"""Model Lifecycle Manager — 资源压力监控（design §6）

每资源（gpu / cpu）一个 PressureMonitor：采样 + 五态状态机。
监控是被动状态机，采样循环由 ModelLifecycleManager 驱动（避免双循环同步问题）。

数据源：默认走常驻采样中枢 `server/resource_probe`（NVML 会话 + psutil，进程内，
不创建子进程）。构造时传 `probe=` 可注入同步探针（单测/手工覆写用），签名保持
`() -> (used_bytes, total_bytes)`，抛异常即视为一次采样失败。

实测约束（design §2）：WDDM 下 per-process VRAM 不可得，gpu 只能读整卡
memory.used；策略语义是"缓解整卡压力"，不是显存归因。

适配性：gpu 资源只有 NVIDIA 卡能采（NVML 由 NVIDIA 驱动提供）。非 NVIDIA 机器上
中枢把探针判为 `absent`，本监控进入 `probe_absent` 姿态——保持 NORMAL 且放行准入，
不再像旧的 nvidia-smi 子进程路线那样永久卡在 `awaiting_first_sample`。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.model_manager.types import PressureState

if TYPE_CHECKING:
    from server.resource_probe.hub import SampleHub

logger = logging.getLogger("parcae.model_manager.monitor")

# 连续探测失败达到该次数后进入 PROBE_DEGRADED（保持最后保护姿态）
_PROBE_FAIL_THRESHOLD = 3

# 资源名 → 中枢探针 id
RESOURCE_PROBE_IDS = {"gpu": "gpu_mem", "cpu": "mem_ram"}

# sample() 最近一次取不到数据的原因
FAIL_ABSENT = "absent"          # 本机没有该数据源（非 NVIDIA 卡 / psutil 缺失）
FAIL_TRANSIENT = "transient"    # 有数据源但这次采样失败
FAIL_NO_NEW = "no_new_sample"   # 中枢还没产出比游标更新的样本


@dataclass
class ResourceMonitorConfig:
    """单资源监控参数（默认值同 design §9）"""

    resource: str                      # "gpu" | "cpu"
    enabled: bool = True
    poll_interval_sec: float = 5.0
    high_watermark: float = 0.90       # 高压阈值（占用比例）
    high_sustain: int = 3              # 连续 N 个样本越限 → REFUSING
    critical_watermark: float = 0.97   # 单样本逃生门（立即 REFUSING）
    low_watermark: float = 0.70        # 低压阈值
    low_sustain: int = 6               # 连续 N 个样本低于阈值 → ARMED



class PressureMonitor:
    """单资源压力状态机

    状态迁移（design §6）：
      NORMAL/ARMED → REFUSING  高压 ×high_sustain，或 ≥critical 单样本
      REFUSING     → ARMED     低压 ×low_sustain
      ARMED        → NORMAL    下次加载成功（管理器调 mark_recovered）
      逐出完成     → NORMAL    （管理器调 force_normal_after_eviction）
      PAUSED（手动）：采样继续、保护态冻结；resume 后按最新样本重评估
      PROBE_DEGRADED：连续探测失败 ≥3，保持降级前保护姿态；探测恢复后还原

    `probe_absent`（不是状态，是姿态标记）：数据源在本机不存在（非 NVIDIA 卡）。
    此时状态停在 NORMAL、准入放行——旧的 nvidia-smi 路线把这种情况算成"永远采不到
    首样本"，导致无卡机器上模型准入被永久卡死。
    """

    def __init__(
        self,
        cfg: ResourceMonitorConfig,
        probe: Callable[[], tuple[int, int]] | None = None,
        *,
        hub: SampleHub | None = None,
    ):
        self.cfg = cfg
        # probe 可注入（单测/手工覆写用）；为 None 时走常驻采样中枢
        self._probe: Callable[[], tuple[int, int]] | None = probe
        self._hub: SampleHub | None = None if probe is not None else hub
        self.probe_id: str | None = RESOURCE_PROBE_IDS.get(cfg.resource) if probe is None else None

        self.state: PressureState = PressureState.NORMAL
        self.last_used: int | None = None
        self.last_total: int | None = None
        self.samples_taken: int = 0
        self.last_transition_ts: float = time.time()
        self.events: deque[dict] = deque(maxlen=50)

        self.probe_absent: bool = False
        self.last_failure: str | None = None

        self._cursor = 0
        self._high_streak = 0
        self._low_streak = 0
        self._probe_fail_streak = 0
        self._degraded_posture: PressureState | None = None  # 降级前的状态
        self._pre_pause_state: PressureState | None = None

    # ---------- 采样 ----------

    def _resolve_hub(self) -> SampleHub:
        if self._hub is None:
            from server.resource_probe import get_resource_hub

            self._hub = get_resource_hub()
        return self._hub

    async def sample(self) -> tuple[int, int] | None:
        """取一个新样本并记账；取不到返回 None，原因写在 `last_failure`"""
        probe = self._probe
        if probe is not None:
            return await self._sample_injected(probe)
        return await self._sample_from_hub()

    async def _sample_injected(
        self, probe: Callable[[], tuple[int, int]]
    ) -> tuple[int, int] | None:
        try:
            used, total = await asyncio.to_thread(probe)
        except Exception as e:
            self.last_failure = FAIL_TRANSIENT
            logger.debug(f"[{self.cfg.resource}] 压力探测失败: {e}")
            return None
        self.last_failure = None
        self.probe_absent = False
        return self._accept(used, total)

    async def _sample_from_hub(self) -> tuple[int, int] | None:
        hub = self._resolve_hub()
        if hub is None or self.probe_id is None or self.probe_id not in hub.probes:
            self._note_absent("该资源在中枢里没有注册探针")
            return None
        if not hub.running:
            # 自愈：lifecycle 里起失败时，第一个 tick 把中枢拉起来。
            # 仍起不来（[resource_probe] enabled=false）→ 判 absent：宁可"监控失效放行"，
            # 也不能回到旧的"永远采不到首样本 → 准入永久 503"
            await hub.ensure_started()
            if not hub.running:
                self._note_absent("采样中枢未运行（[resource_probe] enabled=false 或启动失败）")
                return None
        if hub.is_absent(self.probe_id):
            self._note_absent(f"探针 {self.probe_id} 在本机不可用")
            return None

        sample, self._cursor = hub.consume(self.probe_id, self._cursor)
        if sample is None:
            status = hub.status(self.probe_id)
            if status == "failed":
                self.last_failure = FAIL_TRANSIENT
                logger.debug(f"[{self.cfg.resource}] 中枢采样暂不可用: {self.probe_id}")
            else:
                # 还没产出比游标更新的样本（中枢与管理器周期未对齐时正常发生），
                # 不算失败，避免误判 PROBE_DEGRADED
                self.last_failure = FAIL_NO_NEW
            return None

        self.last_failure = None
        self.probe_absent = False
        return self._accept(sample.value, sample.total or 0)

    def _accept(self, used: float, total: float) -> tuple[int, int]:
        values = (int(used), int(total))
        self.last_used, self.last_total = values
        self.samples_taken += 1
        self._probe_fail_streak = 0
        return values

    def _note_absent(self, detail: str) -> None:
        """数据源不存在：标记姿态、从 PROBE_DEGRADED 复位，不计入探测失败连击"""
        first_time = not self.probe_absent
        self.probe_absent = True
        self.last_failure = FAIL_ABSENT
        if self.state is PressureState.PROBE_DEGRADED:
            restored = self._degraded_posture or PressureState.NORMAL
            self._degraded_posture = None
            self._high_streak = 0
            self._low_streak = 0
            self._transition(restored, "probe_absent_reset")
        if first_time:
            logger.info(f"[{self.cfg.resource}] 无可用数据源，压力监控失效（放行准入）: {detail}")

    # ---------- 状态机 ----------

    @property
    def ratio(self) -> float | None:
        if not self.last_total:
            return None
        return (self.last_used or 0) / self.last_total

    def observe(self, ratio: float) -> dict | None:
        """喂入一个新样本，推进状态机；返回迁移事件（无迁移返回 None）"""
        # 探测恢复：从 PROBE_DEGRADED 还原降级前状态，再继续常规评估
        if self.state is PressureState.PROBE_DEGRADED:
            restored = self._degraded_posture or PressureState.NORMAL
            self._degraded_posture = None
            self._high_streak = 0
            self._low_streak = 0
            event = self._transition(restored, "probe_recovered")
            # 还原后继续评估本样本（可能立刻再迁移）
            self._evaluate(ratio)
            return event

        if self.state is PressureState.PAUSED:
            return None  # 保护态冻结，只记录样本

        return self._evaluate(ratio)

    def _evaluate(self, ratio: float) -> dict | None:
        cfg = self.cfg
        if self.state in (PressureState.NORMAL, PressureState.ARMED):
            if ratio >= cfg.critical_watermark:
                self._high_streak = 0
                self._low_streak = 0
                return self._transition(PressureState.REFUSING, "critical_sample")
            if ratio >= cfg.high_watermark:
                self._high_streak += 1
                self._low_streak = 0
                if self._high_streak >= cfg.high_sustain:
                    self._high_streak = 0
                    return self._transition(PressureState.REFUSING, "high_sustain")
            else:
                self._high_streak = 0
            return None

        if self.state is PressureState.REFUSING:
            if ratio <= cfg.low_watermark:
                self._low_streak += 1
                if self._low_streak >= cfg.low_sustain:
                    self._low_streak = 0
                    return self._transition(PressureState.ARMED, "low_sustain")
            else:
                self._low_streak = 0
            return None

        return None  # NORMAL after restore 等：无动作

    def on_probe_failure(self) -> dict | None:
        """探测失败一次；连续达到阈值进入 PROBE_DEGRADED（保持最后保护姿态）"""
        self._probe_fail_streak += 1
        if self.state is PressureState.PROBE_DEGRADED:
            return None
        if self._probe_fail_streak >= _PROBE_FAIL_THRESHOLD:
            self._degraded_posture = self.state
            return self._transition(PressureState.PROBE_DEGRADED, "probe_degraded")
        return None

    def mark_recovered(self) -> dict | None:
        """ARMED → NORMAL：懒恢复加载成功（管理器在加载成功后调用）"""
        if self.state is PressureState.ARMED:
            return self._transition(PressureState.NORMAL, "reload_success")
        return None

    def force_normal_after_eviction(self) -> dict | None:
        """逐出完成后回 NORMAL（信任卸载已释放压力；仍高会由后续采样重新触发）"""
        if self.state is PressureState.REFUSING:
            self._high_streak = 0
            self._low_streak = 0
            return self._transition(PressureState.NORMAL, "eviction_complete")
        return None

    def pause(self) -> dict | None:
        if self.state is PressureState.PAUSED:
            return None
        self._pre_pause_state = self.state if self.state is not PressureState.PROBE_DEGRADED else (
            self._degraded_posture or PressureState.NORMAL
        )
        return self._transition(PressureState.PAUSED, "manual_pause")

    def resume(self) -> dict | None:
        if self.state is not PressureState.PAUSED:
            return None
        resume_to = self._pre_pause_state or PressureState.NORMAL
        self._pre_pause_state = None
        self._high_streak = 0
        self._low_streak = 0
        event = self._transition(resume_to, "manual_resume")
        # 安全：暂停期间压力可能已变化，若有最新样本立即按逃生门重评估
        ratio = self.ratio
        if ratio is not None and ratio >= self.cfg.critical_watermark:
            self._transition(PressureState.REFUSING, "critical_sample")
        return event

    def _transition(self, new_state: PressureState, cause: str) -> dict:
        old = self.state
        self.state = new_state
        self.last_transition_ts = time.time()
        event = {
            "ts": self.last_transition_ts,
            "resource": self.cfg.resource,
            "from": old.value,
            "to": new_state.value,
            "cause": cause,
            "ratio": self.ratio,
        }
        self.events.append(event)
        logger.info(
            f"[{self.cfg.resource}] 压力状态迁移 {old.value} → {new_state.value} "
            f"({cause}, ratio={self.ratio if self.ratio is None else round(self.ratio, 3)})"
        )
        return event

    # ---------- 姿态判定 ----------

    @property
    def has_first_sample(self) -> bool:
        return self.samples_taken > 0

    def admission_allowed(self) -> bool:
        """当前姿态是否放行新加载（design §7 准入）"""
        if self.state is PressureState.PAUSED:
            return True
        if self.state is PressureState.REFUSING:
            return False
        if self.state is PressureState.PROBE_DEGRADED:
            # 保持降级前的最后保护姿态
            return self._degraded_posture not in (PressureState.REFUSING,)
        return True  # NORMAL / ARMED

    def eviction_active(self) -> bool:
        """是否应电平式评估逐出：仅 REFUSING（有实时数据）；降级/暂停不主动逐出"""
        return self.state is PressureState.REFUSING

    # ---------- 报告 ----------

    def summary(self) -> dict:
        out = {
            "resource": self.cfg.resource,
            "enabled": self.cfg.enabled,
            "state": self.state.value,
            "probe_source": "hub" if self._probe is None else "injected",
            "probe_id": self.probe_id,
            "probe_absent": self.probe_absent,
            "last_failure": self.last_failure,
            "degraded_posture": self._degraded_posture.value if self._degraded_posture else None,
            "used_mb": round(self.last_used / (1024 * 1024)) if self.last_used is not None else None,
            "total_mb": round(self.last_total / (1024 * 1024)) if self.last_total is not None else None,
            "ratio": round(self.ratio, 4) if self.ratio is not None else None,
            "samples_taken": self.samples_taken,
            "last_transition_ts": self.last_transition_ts,
        }
        # 中枢侧诊断（只在已经拿到中枢时读，避免报告路径产生副作用）
        if self._probe is None and self._hub is not None and self.probe_id:
            sample = self._hub.latest(self.probe_id)
            out.update(
                {
                    "probe_status": self._hub.status(self.probe_id),
                    "probe_fail_streak": self._hub.fail_streak(self.probe_id),
                    "probe_age_sec": round(sample.age(), 1) if sample else None,
                }
            )
        return out

"""资源采样中枢：进程内唯一的采样 owner

一个 ticker 任务按各自周期采样、缓存最近样本；消费方只读缓存或走 `collect()`，
**任何消费路径都不再创建进程**。这取代了过去两处各自 fork nvidia-smi 的形态
（`server/model_manager/monitor.py` 每 5s 一次 + 活动采集的高负载门每次一次）。

三类调用面：
- `read_cached(id)`：纯缓存读，永不阻塞，可在事件循环上调
- `collect(id)`：async；缓存新鲜则直接返回，否则放线程池采一次
- `collect_sync(id)`：给本来就跑在线程里的调用方（如活动采集的 to_thread 路径）

看门狗/新消费方的接入点：`status()` + `health()` + `events`（样本与状态都已在这里
集中，规则引擎只需订阅这些，不必自己起采样）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from server.resource_probe.probe import Probe, ProbeAbsent, Sample

logger = logging.getLogger("parcae.resource_probe.hub")

STATUS_UNKNOWN = "unknown"
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_ABSENT = "absent"


class SampleHub:
    """按 probe_id 缓存样本的常驻采样器（进程内单例，经 get_resource_hub() 获取）"""

    def __init__(
        self,
        probes: dict[str, Probe],
        *,
        poll_interval_sec: float = 5.0,
        stale_multiplier: float = 3.0,
        absent_retry_sec: float = 300.0,
        enabled: bool = True,
        clock=time.time,
    ) -> None:
        self.probes = probes
        self.poll_interval_sec = poll_interval_sec
        self.stale_multiplier = stale_multiplier
        self.absent_retry_sec = absent_retry_sec
        self.enabled = enabled
        self._clock = clock

        self._samples: dict[str, Sample] = {}
        self._status: dict[str, str] = dict.fromkeys(probes, STATUS_UNKNOWN)
        self._fail_streak: dict[str, int] = dict.fromkeys(probes, 0)
        self._counts: dict[str, int] = dict.fromkeys(probes, 0)
        self._next_due: dict[str, float] = dict.fromkeys(probes, 0.0)
        self._retry_after: dict[str, float] = dict.fromkeys(probes, 0.0)
        self._intervals: dict[str, float] = {
            pid: float(getattr(probe, "interval_sec", 0) or poll_interval_sec)
            for pid, probe in probes.items()
        }

        self.events: deque[dict] = deque(maxlen=50)
        self._task: asyncio.Task | None = None
        self._stopped = True

    # ---------- 生命周期 ----------

    @property
    def running(self) -> bool:
        return self._task is not None

    async def ensure_started(self) -> None:
        """消费方启动前确保 ticker 已在跑（幂等；中枢已启动则直接返回）"""
        await self.start()

    async def start(self) -> None:
        """启动 ticker；先同步采一轮首样本，避免启动即失明"""
        if not self.enabled or self._task is not None:
            return
        await self.prime()
        self._stopped = False
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "资源采样中枢已启动: %d 个探针, interval=%.1fs",
            len(self.probes),
            self.poll_interval_sec,
        )

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for probe in self.probes.values():
            try:
                probe.reset()
            except Exception:
                pass

    async def prime(self) -> None:
        """强制立即采一轮（管理器用它堵"首样本先于任何准入判定"的窗口）"""
        for pid in self.probes:
            await asyncio.to_thread(self._collect, pid, force=True)

    def _now(self) -> float:
        return self._clock()

    async def _run_loop(self) -> None:
        tick = min(self._intervals.values() or [self.poll_interval_sec])
        while not self._stopped:
            await asyncio.sleep(tick)
            now = self._now()
            for pid in self.probes:
                if now < self._next_due[pid]:
                    continue
                await asyncio.to_thread(self._collect, pid)

    # ---------- 读取面 ----------

    def read_cached(self, probe_id: str) -> Sample | None:
        """纯缓存读，永不阻塞；无样本或已过期返回 None"""
        sample = self._samples.get(probe_id)
        if sample is None:
            return None
        if self.is_stale(probe_id):
            return None
        return sample

    async def collect(self, probe_id: str) -> Sample | None:
        """async 读：缓存新鲜直接用，否则放线程池采一次"""
        cached = self.read_cached(probe_id)
        if cached is not None:
            return cached
        return await asyncio.to_thread(self._collect, probe_id)

    def collect_sync(self, probe_id: str) -> Sample | None:
        """供本就在线程里的调用方使用（会阻塞调用线程）"""
        cached = self.read_cached(probe_id)
        if cached is not None:
            return cached
        return self._collect(probe_id)

    def latest(self, probe_id: str) -> Sample | None:
        """忽略新鲜度返回最近样本（诊断/看门狗判断陈旧度用）"""
        return self._samples.get(probe_id)

    def consume(self, probe_id: str, cursor: int) -> tuple[Sample | None, int]:
        """按游标取"自上次以来是否有了新样本"

        状态机类消费方（压力监控）用它推进，避免同一个样本被计两次、
        也避免消费方自己的周期与中枢周期不一致时误判为采样失败。
        """
        count = self._counts.get(probe_id, 0)
        if count > cursor:
            return self._samples.get(probe_id), count
        return None, cursor

    def fail_streak(self, probe_id: str) -> int:
        return self._fail_streak.get(probe_id, 0)

    def status(self, probe_id: str) -> str:
        return self._status.get(probe_id, STATUS_UNKNOWN)

    def is_absent(self, probe_id: str) -> bool:
        """数据源在本机不存在（如非 NVIDIA 卡）——消费方应判"该资源不可监控"而非降级"""
        return self.status(probe_id) == STATUS_ABSENT

    def is_stale(self, probe_id: str, now: float | None = None) -> bool:
        sample = self._samples.get(probe_id)
        if sample is None:
            return True
        limit = self._intervals.get(probe_id, self.poll_interval_sec) * self.stale_multiplier
        return sample.age(now or self._now()) > limit

    # ---------- 采样实现（阻塞，可在线程中调用） ----------

    def _collect(self, probe_id: str, *, force: bool = False) -> Sample | None:
        probe = self.probes.get(probe_id)
        if probe is None:
            return None
        now = self._now()
        interval = self._intervals.get(probe_id, self.poll_interval_sec)
        self._next_due[probe_id] = now + interval
        if not force and now < self._retry_after[probe_id]:
            return None  # absent：等慢速重试窗口，不每周期空转
        try:
            sample = probe.sample()
        except ProbeAbsent as e:
            self._mark_absent(probe_id, str(e), now)
            return None
        except Exception as e:
            self._mark_failed(probe_id, str(e), now)
            return None
        self._samples[probe_id] = sample
        self._counts[probe_id] += 1
        self._fail_streak[probe_id] = 0
        self._retry_after[probe_id] = 0.0
        self._set_status(probe_id, STATUS_OK, now, reason="sampling")
        return sample

    def _mark_absent(self, probe_id: str, detail: str, now: float) -> None:
        self._retry_after[probe_id] = now + self.absent_retry_sec
        logger.info(
            "[%s] 数据源在本机不存在，判为不可用（%.0fs 后重试）: %s",
            probe_id,
            self.absent_retry_sec,
            detail,
        )
        self._set_status(probe_id, STATUS_ABSENT, now, reason=detail)

    def _mark_failed(self, probe_id: str, detail: str, now: float) -> None:
        self._fail_streak[probe_id] += 1
        streak = self._fail_streak[probe_id]
        if streak == 3:
            logger.warning("[%s] 连续 %d 次采样失败: %s", probe_id, streak, detail)
        else:
            logger.debug("[%s] 采样失败: %s", probe_id, detail)
        self._set_status(probe_id, STATUS_FAILED, now, reason=detail)

    def _set_status(self, probe_id: str, new_status: str, now: float, *, reason: str) -> None:
        old = self._status.get(probe_id, STATUS_UNKNOWN)
        if old == new_status:
            return
        self._status[probe_id] = new_status
        self.events.append(
            {
                "ts": now,
                "probe": probe_id,
                "from": old,
                "to": new_status,
                "reason": reason[:200],
            }
        )

    # ---------- 报告 ----------

    def health(self) -> dict:
        """`/health.resource_probe` 用的高频计数最小集"""
        return {
            "running": self._task is not None,
            "poll_interval_sec": self.poll_interval_sec,
            "probes": {
                pid: {
                    "source": probe.source,
                    "status": self._status.get(pid, STATUS_UNKNOWN),
                    "interval_sec": self._intervals.get(pid, self.poll_interval_sec),
                    "samples_taken": self._counts[pid],
                    "fail_streak": self._fail_streak[pid],
                    "age_sec": round(sample.age(self._now()), 1) if (sample := self._samples.get(pid)) else None,
                    "value": round(sample.value, 2) if (sample := self._samples.get(pid)) else None,
                    "total": round(sample.total, 2) if (sample := self._samples.get(pid)) and sample.total else None,
                    "stale": self.is_stale(pid),
                }
                for pid, probe in self.probes.items()
            },
            "recent_events": list(self.events)[-8:],
        }

    def summary_line(self) -> str:
        """启动/日志用的一行摘要"""
        parts = []
        for pid in self.probes:
            sample = self._samples.get(pid)
            value = "-" if sample is None else f"{round(sample.value)}"
            parts.append(f"{pid}={self._status.get(pid)}({value})")
        return ", ".join(parts)

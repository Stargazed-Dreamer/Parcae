"""记忆维护器 — 垃圾预过滤 + 老化评分 + 指标日聚合

定期检查 facts 表中超过 stale_days 未更新的记忆，算出 stale_score 供 staleness
提示与排序使用；并在三张明细表被 retention 删掉之前，把按日指标固化进
`memory_metrics_daily`（先聚合后清理，否则趋势永远是零）。

2026-10-31 移除 LLM 验证器（用户拍板，见 `30_架构与设计/28` §2.6 与 `R-VALIDATE`）：
原实现每 6 小时让 cheap 模型逐条判 keep/update/archive，实测 162 次判定里 157 次
keep，唯一能执行的 archive 判决又从不执行——产出只有日志，成本却是每天最多
20 万输入 token。形态类问题（超长/超版/频繁改/坏 scope/死链/孤儿）本来就是确定性
查询，已移交中枢 L7 巡检与面板；内容真伪没有程序判得了，交给人看。
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from server.memory.config import MemoryConfig
from server.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class MemoryMaintainer:
    """记忆维护器：垃圾预过滤 + 老化评分 + 指标日聚合（无 LLM 调用）"""

    def __init__(self, store: MemoryStore, config: MemoryConfig,
                 search_tracer: Any | None = None,
                 evidence_ledger: Any | None = None):
        """初始化维护器。

        5-14: search_tracer / evidence_ledger 由 MemoryManager 正常传参注入
        （此前靠 manager 侧 Any 动态挂属性 + getattr 字符串约定取回）。
        用 Any 承载：二者与 maintainer 分属不同模块，无静态类型契约。
        """
        self.store = store
        self.config = config
        self._search_tracer: Any | None = search_tracer
        self._evidence_ledger: Any | None = evidence_ledger

    def should_run(self) -> bool:
        """检查是否应该运行维护（距上次运行 > interval_hours）"""
        cur = self.store.conn.execute(
            "SELECT value FROM schema_info WHERE key = 'maintainer_last_run'"
        )
        row = cur.fetchone()
        if not row:
            return True
        try:
            last_run = datetime.fromisoformat(row[0])
            elapsed = datetime.now() - last_run
            return elapsed > timedelta(hours=self.config.maintain_interval_hours)
        except (ValueError, TypeError):
            return True

    async def run(self) -> dict:
        """执行完整维护流程：垃圾预过滤 → 老化评分 → **指标日聚合** → 清理过期明细

        2026-10-31 起没有「验证」这一段：LLM 验证器已删除（见模块 docstring）。

        指标聚合必须排在 retention 清理之前：三张明细表（search_traces /
        evidence_ledger / context_projections）按 30 天硬删，若先删后算，
        每天看到的都是空表，趋势曲线永远是零（2026-10-03 体检实测：累计写过
        1094 条 trace、10165 行 evidence，当前均为 0 行，且删除前无任何聚合）。
        """
        prefilter_result = self._prefilter_garbage()
        aging_result = self.run_aging()

        # v5: 明细被删之前先把按日指标固化下来（失败不影响后续清理）
        metrics_result: dict[str, Any] = {"written": 0, "errors": []}
        try:
            from server.memory import metrics as _metrics

            metrics_result = _metrics.record_daily(
                self.store.conn,
                db_path=getattr(self.store, "db_path", None),
                write_lock=self.store._write_lock,
            )
            if metrics_result.get("errors"):
                logger.warning(
                    f"记忆指标聚合有 {len(metrics_result['errors'])} 项失败: "
                    f"{metrics_result['errors']}"
                )
        except Exception as e:  # noqa: BLE001 - 指标是观察者，不能拖垮维护本身
            logger.warning(f"记忆指标日聚合失败: {e}")
            metrics_result = {"written": 0, "errors": [f"record_daily:{type(e).__name__}"]}

        # v3: 清理过期 search_traces（如果 SearchTracer 已注入）
        trace_cleanup = {"deleted": 0, "cutoff": None, "retention_days": 0}
        # 5-14: tracer/ledger 经 __init__ 正常注入，直接读属性
        try:
            if self._search_tracer is not None:
                trace_cleanup = self._search_tracer.cleanup_old()
        except Exception as e:
            logger.warning(f"清理 search_traces 失败: {e}")

        # v3: 清理过期 evidence_ledger（如果 EvidenceLedger 已注入）
        evidence_cleanup = {"deleted": 0, "cutoff": None, "retention_days": 0}
        try:
            if self._evidence_ledger is not None:
                evidence_cleanup = self._evidence_ledger.cleanup_old()
        except Exception as e:
            logger.warning(f"清理 evidence_ledger 失败: {e}")

        # v4 T15: 清理过期 context_projections（与 traces/evidence 同一套保留纪律）
        projection_deleted = 0
        try:
            from server.memory import projection as _proj

            projection_deleted = _proj.cleanup_old(
                self.store.conn, self.store._write_lock,
                self.config.projection_retention_days,
            )
        except Exception as e:
            logger.warning(f"清理 context_projections 失败: {e}")

        now_str = datetime.now().isoformat()
        # T06：INSERT + commit 包入 _write_lock
        with self.store._write_lock:
            self.store.conn.execute(
                "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
                ("maintainer_last_run", now_str),
            )
            self.store.conn.commit()
        return {
            "prefilter": prefilter_result,
            "aging": aging_result,
            "metrics": metrics_result,
            "trace_cleanup": trace_cleanup,
            "evidence_cleanup": evidence_cleanup,
            "projection_cleanup_deleted": projection_deleted,
            "last_run": now_str,
        }


    def _prefilter_garbage(self) -> dict:
        """预过滤明显垃圾，直接删除，不浪费 LLM 调用。

        规则（确定性，无 LLM 参与）：
        1. test_ 前缀：测试数据泄漏到生产库
        2. 孤儿子字段：key 含 "." 且父 key 不存在于 facts 表（父 key 已被删除的残留）
        3. 保留带 "." 的合法主体 key（如 reference.vision_503_cooldown）
        """
        stats = {"deleted_test": 0, "deleted_orphan_subfield": 0, "skipped": 0}

        cur = self.store.conn.execute("SELECT key FROM facts")
        all_keys = {row[0] for row in cur.fetchall()}

        to_delete: list[str] = []
        for key in all_keys:
            # 规则 1：test_ 前缀
            if key.startswith("test_"):
                to_delete.append(key)
                stats["deleted_test"] += 1
                continue
            # 规则 2：孤儿子字段（含 "." 且父 key 不存在）
            # 但跳过合法的带点主体：父 key 本身不存在但 key 是通过 manual 写入的主体
            # 判断方式：带 "." 的 key，如果去掉最后一段后的父 key 不在 facts 表中，
            # 且该 key 自身不是 manual source 的主体（即 value 不是完整 JSON dict），则视为孤儿
            if "." in key:
                parent = key.rsplit(".", 1)[0]
                if parent not in all_keys:
                    # 父 key 不在 facts 表，检查是否是 manual source 主体
                    cur2 = self.store.conn.execute(
                        "SELECT source FROM facts WHERE key = ?", (key,)
                    )
                    row = cur2.fetchone()
                    is_manual_main = row and row[0] == "manual"
                    if not is_manual_main:
                        to_delete.append(key)
                        stats["deleted_orphan_subfield"] += 1

        # T06：循环 DELETE (facts + memory_meta) + commit 整体包入 _write_lock
        # 不调 self.store.delete_fact（它内部也获取 _write_lock，会死锁），直接 execute
        if to_delete:
            with self.store._write_lock:
                for key in to_delete:
                    self.store.conn.execute(
                        "DELETE FROM facts WHERE key = ?", (key,)
                    )
                    self.store.conn.execute(
                        "DELETE FROM memory_meta WHERE key = ?", (key,)
                    )
                self.store.conn.commit()
            logger.info(
                f"memory prefilter: 删除 {len(to_delete)} 条垃圾 "
                f"(test={stats['deleted_test']}, orphan_subfield={stats['deleted_orphan_subfield']})"
            )

        return stats

    def run_aging(self) -> dict:
        """扫描所有 facts，计算 stale_score（staleness 提示与排序都用它）。

        2026-10-31：验证器删除后本函数不再翻转 `validation_status`。
        `memory_meta.validation_status / last_validated_at / validate_attempts /
        validation_note` 四列**保留但冻结**（历史标签，面板显示为「历史字段」），
        不再有生产者；不删列是因为 SQLite 删列代价大、且这批标签仍是可查的历史。

        `stale_score` 每轮照常更新。

        T06：循环 INSERT + commit 整体包入 _write_lock，避免并发写交叉。
        """
        stale_days = self.config.maintain_stale_days
        cutoff_dt = datetime.now() - timedelta(days=stale_days)
        cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

        cur = self.store.conn.execute(
            "SELECT key, updated_at FROM facts "
            "WHERE updated_at < ? AND (kind IS NULL OR kind <> 'norm') "
            "ORDER BY updated_at ASC",
            (cutoff_str,),
        )
        rows = cur.fetchall()
        stale_keys: list[dict[str, Any]] = []
        # T06：循环 INSERT + commit 整体包入 _write_lock
        with self.store._write_lock:
            for row in rows:
                key, updated_at = row
                try:
                    updated_dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
                    days_since = (datetime.now() - updated_dt).days
                except (ValueError, TypeError):
                    days_since = stale_days

                self.store.conn.execute(
                    """INSERT INTO memory_meta (key, stale_score)
                       VALUES (?, ?)
                       ON CONFLICT(key) DO UPDATE SET stale_score = excluded.stale_score""",
                    (key, float(days_since)),
                )
                stale_keys.append({"key": key, "days_since_update": days_since})
            self.store.conn.commit()
        return {
            "stale_count": len(stale_keys),
            "stale_threshold_days": stale_days,
            "stale_keys": [k["key"] for k in stale_keys[:20]],
        }

    def get_status(self) -> dict:
        """返回维护器状态。

        验证器已删除：不再报告 `validate_enabled` / `model_tier`（那两个字段会让人
        以为还有开关可拨）。`validator` 字段显式写 removed，避免面板把「标签不再
        变化」误读成「验证器卡住了」。
        """
        cur = self.store.conn.execute(
            "SELECT value FROM schema_info WHERE key = 'maintainer_last_run'"
        )
        row = cur.fetchone()
        last_run = row[0] if row else None

        stale_total = self.store.conn.execute(
            "SELECT COUNT(*) FROM memory_meta WHERE stale_score > ?",
            (float(self.config.maintain_stale_days),),
        ).fetchone()[0]
        archived_total = self.store.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE archived_at IS NOT NULL"
        ).fetchone()[0]

        return {
            "last_run": last_run,
            "stale_threshold_days": self.config.maintain_stale_days,
            "interval_hours": self.config.maintain_interval_hours,
            "stale_total": int(stale_total or 0),
            "archived_total": int(archived_total or 0),
            "validator": "removed_2026-10-31",
        }

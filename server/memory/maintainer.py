"""记忆维护器 — 自动老化 + LLM 验证

定期检查 facts 表中超过 stale_days 未更新的记忆，标记为 stale，
然后用 cheap 模型验证是否仍准确，返回建议动作 (keep/update/archive)。

设计原则（来自 Claude Code memory docs）：
  - 结构化而非自由文本：验证结果强制 JSON
  - cheap 模型做选择题，不做摘要：只返回 is_accurate + suggested_action
  - 时间感知注入：>7天 = stale
  - 不确定时保留 (when in doubt, keep)
"""

import asyncio
import functools
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from server.memory.config import MemoryConfig
from server.memory.store import MemoryStore

logger = logging.getLogger(__name__)

# value 里这些键是 memory_set 旧 merge 路径抄进正文的元数据信封（v6 已封住新增来源，
# 存量脏 value 未洗），对"这条记忆还成立吗"没有信息量，却占据了旧实现里唯一可见的窗口。
_METADATA_KEYS = {
    "source", "updated_at", "created_at", "access_count", "confidence",
    "mentioned_at", "fact_type", "consumption_contexts", "trigger_keywords",
    "source_task", "type", "date",
}
# 有信息量的正文键，按此顺序优先呈现（不同 skill 的 fact 结构不同）
_CONTENT_KEYS = [
    "summary", "content", "name", "description", "lesson", "decision",
    "why", "how_to_apply", "problem", "solution", "rules",
]
# 正文超窗时，archive 判决最多被采纳前不采纳的延判次数
_ARCHIVE_CONFIRM_ATTEMPTS = 3

# 无效判据：cheap 模型把自己**知识截止日期之后**的时间戳读成"未来未发生 ⇒ 内容无效"。
# 2026-10-03 重判存量时实测到两例：
#   project_ruanxie_recruit_2026   "时间戳显示为2026年9月，已超出模型知识库截止日期（2026-07），
#                                   属于未来未发生的事件，内容无效"
#   reference_opencode_zen_free_tier（同期上屏 283 次的第 2 高频记忆）同样理由挂 archive
# 这条偏见的杀伤面很专一：**优先杀最新写入的记忆**——而最新写入的恰恰是最该活的那批。
# 凡 archive 理由命中这里的模式，本次判决不采纳（与"超窗不判死"同源：依据不成立就不能动手）。
_REJECTED_ARCHIVE_REASON_RE = re.compile(
    r"知识(库)?截止|(超出|早于|晚于).{0,12}(训练|知识).{0,8}截止|"
    r"未来(未|尚)?发生|尚未发生|不可能(存在|发生)|属于预测"
)


def is_invalid_archive_reason(reason: str | None) -> bool:
    """archive 的理由是否属于"时间观缺陷"（把晚于模型知识截止的日期当虚构）。"""
    return bool(reason) and bool(_REJECTED_ARCHIVE_REASON_RE.search(reason or ""))

# 分类型的失效判据。旧 prompt 对所有 fact 用同一把尺子，而"多久算失效"
# 在五类记忆上是五个不同的问题：状态类几天就变，偏好类几年不变。
_FACT_TYPE_GUIDANCE: dict[str, str] = {
    "user": "用户偏好/身份类：除非有明确证据表明用户改过口径，否则视为长期成立；"
            "\"很久没被提到\"不是失效理由，判 keep。\n",
    "feedback": "用户反馈/纠正类：它记录的是\"用户表过一次态\"这件事本身，"
                "这条历史不会因为时间推移而变假。只有当后来用户明确改口才判 update，"
                "不要因为\"看起来像一次性抱怨\"判 archive。\n",
    "project": "项目状态/进度类：这类确实最容易变。但失效表现是\"进度已经往前了\"，"
               "不是\"这条没价值了\"——若里面含决策、边界或踩坑记录，判 update 保留，"
               "只有纯进度快照且工作已收尾才判 archive。\n",
    "reference": "外部机制/命令/API 类：若描述的是带明确时间点的实测"
                 "（如\"2026-09-13 实测某平台的免费层规则\"），它作为历史实测记录仍然成立，"
                 "判 keep；只有在你确知该机制后来已经变更时才判 update。"
                 "\"可能变了\"不等于\"变了\"。\n",
    "experience": "踩坑/调试结论/方法论类：可复用性最高。\"当时那个 bug 已经修好了\""
                  "不等于\"这条经验失效了\"——默认判 keep。\n",
    "": "类型未标注：按上面的通用判据判断，默认倾向 keep。\n",
}


def build_validation_preview(value: str | None, max_chars: int) -> tuple[str, bool]:
    """从 fact 的 value 里提出"值得判生死"的正文，剥掉内嵌的元数据信封。

    Returns:
        (preview, truncated)。truncated=True 表示剥完之后正文仍超出验证窗口。
        这个标记不是装饰：旧实现把 `value[:800]` 直接喂给 cheap 模型，而实测
        120/151 条 fact 超过 800 字符、81 条的前 800 字符以 JSON 脚手架开头，
        于是模型给出"内容被截断／乱码／无语义"的 archive 判决——72% 的假归档
        就是这么来的（被误判的包括 ui_design_system、approval_strictness_levels）。
        超窗时宁可延判，不能把"看不全"当成"没价值"。
    """
    raw = value or ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = None

    text = raw
    if isinstance(data, dict):
        leftover = {k: v for k, v in data.items() if k not in _METADATA_KEYS}
        parts: list[str] = []
        for k in _CONTENT_KEYS:
            if k in leftover:
                v = leftover.pop(k)
                parts.append(v if isinstance(v, str) else f"{k}: {v}")
        if leftover:
            # 其余业务键（如 stock_review_output / steps_completed）原样带上，
            # 不静默丢弃——验证器看不到的一律算"看不全"，走延判路径
            parts.append(json.dumps(leftover, ensure_ascii=False))
        joined = "\n".join(p for p in parts if str(p).strip())
        text = joined or raw

    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


class MemoryMaintainer:
    """记忆维护器：老化评分 + LLM 验证"""

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
        """执行完整维护流程：垃圾预过滤 → 老化 → 验证 → **指标日聚合** → 清理过期 trace，返回统计

        指标聚合必须排在 retention 清理之前：三张明细表（search_traces /
        evidence_ledger / context_projections）按 30 天硬删，若先删后算，
        每天看到的都是空表，趋势曲线永远是零（2026-10-03 体检实测：累计写过
        1094 条 trace、10165 行 evidence，当前均为 0 行，且删除前无任何聚合）。
        """
        prefilter_result = self._prefilter_garbage()
        aging_result = self.run_aging()
        validate_result = {"validated": 0, "keep": 0, "update": 0, "archive": 0}
        if self.config.maintain_enable_validate:
            validate_result = await self.run_validate()

        # v5: 明细被删之前先把按日指标固化下来（失败不影响后续清理）
        metrics_result: dict[str, Any] = {"written": 0, "errors": []}
        try:
            from server.memory import metrics as _metrics

            metrics_result = _metrics.record_daily(
                self.store.conn,
                db_path=getattr(self.store, "db_path", None),
                validator_window_chars=self.config.validate_preview_chars,
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
            "validate": validate_result,
            "metrics": metrics_result,
            "trace_cleanup": trace_cleanup,
            "evidence_cleanup": evidence_cleanup,
            "projection_cleanup_deleted": projection_deleted,
            "last_run": now_str,
        }

    def requeue_all_validated(self) -> dict:
        """把所有已验证标签重置为 pending（`POST /memory/maintain` 的 force_revalidate 用）

        为什么需要一个显式入口：archive 标签的默认重验间隔是 30 天，而验证器修好之后，
        存量的误判（实测 109 条 archive，其中 2 条一边被判死一边上屏 ≥20 次）
        按自然轮换要再等一个月才能被重判。但"重判一遍"会按 batch_limit 逐轮消耗
        cheap 池 quota 并改写标签，属于有代价的动作，所以不自动跑、只接受明确调用。
        """
        with self.store._write_lock:
            cur = self.store.conn.execute(
                """UPDATE memory_meta
                   SET validation_status = 'pending',
                       last_validated_at = NULL,
                       validate_attempts = 0
                   WHERE validation_status != 'pending'"""
            )
            requeued = cur.rowcount
            self.store.conn.commit()
        logger.info(f"记忆验证标签全量重置为 pending: {requeued} 条")
        return {"requeued": requeued}

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
        """扫描所有 facts，计算 stale_score，把到期的记忆重置为 pending 等待再验证。

        v5 改动（2026-10-03 体检）：
        - 状态重置改为**按时间门控**：只有 `last_validated_at` 距今 ≥ revalidate_days
          （默认 30 天）或从未验证过，才重置回 pending。此前 keep/update 每 6 小时
          就被重新置回 pending，等于对每条 stale 记忆无限重验（白烧 cheap 池 quota）。
        - `archive` 也纳入可重验范围。此前 archive 是**永久标签**（CASE 只翻转
          keep/update），而实测 109/151 条 archive 多数出自一次"只看前 800 字符"
          的误判——修好验证器之后，必须让这些存量标签能被重新判一遍，否则错误的
          死刑永远留在账上。archive 语义本身（不摘除召回）按用户裁决保持不变。
        - `stale_score` 每轮照常更新（staleness 警告要用它）。

        T06：循环 INSERT + commit 整体包入 _write_lock，避免并发写交叉。
        """
        stale_days = self.config.maintain_stale_days
        revalidate_days = self.config.validate_revalidate_days
        cutoff_dt = datetime.now() - timedelta(days=stale_days)
        cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

        cur = self.store.conn.execute(
            "SELECT key, value, updated_at FROM facts WHERE updated_at < ? ORDER BY updated_at ASC",
            (cutoff_str,),
        )
        rows = cur.fetchall()
        stale_keys = []
        requeued = 0
        # T06：循环 INSERT + commit 整体包入 _write_lock
        with self.store._write_lock:
            for row in rows:
                key, value, updated_at = row
                try:
                    updated_dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
                    days_since = (datetime.now() - updated_dt).days
                except (ValueError, TypeError):
                    days_since = stale_days

                before = self.store.conn.execute(
                    "SELECT validation_status FROM memory_meta WHERE key = ?", (key,)
                ).fetchone()
                self.store.conn.execute(
                    """INSERT INTO memory_meta (key, stale_score, validation_status)
                       VALUES (?, ?, 'pending')
                       ON CONFLICT(key) DO UPDATE SET
                           stale_score = excluded.stale_score,
                           validation_status = CASE
                               WHEN validation_status = 'pending' THEN 'pending'
                               WHEN last_validated_at IS NULL THEN 'pending'
                               WHEN julianday('now', 'localtime') - julianday(last_validated_at) >= ?
                                   THEN 'pending'
                               ELSE validation_status
                           END""",
                    (key, float(days_since), float(revalidate_days)),
                )
                after = self.store.conn.execute(
                    "SELECT validation_status FROM memory_meta WHERE key = ?", (key,)
                ).fetchone()
                if before and after and before[0] != "pending" and after[0] == "pending":
                    requeued += 1
                stale_keys.append({"key": key, "days_since_update": days_since})
            self.store.conn.commit()
        return {
            "stale_count": len(stale_keys),
            "stale_threshold_days": stale_days,
            "revalidate_days": revalidate_days,
            "requeued_for_revalidation": requeued,
            "stale_keys": [k["key"] for k in stale_keys[:20]],
        }

    def _defer_verdict(self, key: str, attempts: int, note: str) -> None:
        """本轮不采纳该判决：留在 pending，只递增计数并写明为什么没采纳。

        单条 UPDATE 包 `_write_lock`（与 5-9 那条约定一致：写语句不包锁会与并发写交叉）。
        """
        with self.store._write_lock:
            self.store.conn.execute(
                "UPDATE memory_meta SET validate_attempts = ?, validation_note = ? WHERE key = ?",
                (attempts, note[:500], key),
            )
            self.store.conn.commit()

    async def run_validate(self) -> dict:
        """对 pending 的记忆调用 LLM 验证，写入建议动作

        v15：统一走 pool.call(use_case="memory_validate")，不再 OpenAI SDK 直连。
        429 熔断：pool 内部已对 429 做 model_cooldown，但 memory_validate 走 retries=1
        单条调用，失败后立即返回。这里仍保留 consecutive_429 计数做批量熔断，
        防止 quota 耗尽时连发数十次失败调用透支 quota。
        跳过的 pending 条目下次 maintain 周期再处理。

        v5 改动（2026-10-03 体检）：
        - 单轮最多验证 `validate_batch_limit` 条（archive 改为可重验后存量积压 109 条，
          不限流会在一轮里打空 cheap 池）。
        - **正文超窗时不采纳 archive**：延判到下一轮，连续 _ARCHIVE_CONFIRM_ATTEMPTS 次
          仍超窗则改判 `update`（"臃肿到无法判读"是重写信号，不是过时信号）。
        - 每次验证递增 `validate_attempts`，供上面那条计数。
        """
        batch_limit = self.config.validate_batch_limit
        preview_chars = self.config.validate_preview_chars
        cur = self.store.conn.execute(
            """SELECT m.key, f.value, m.validate_attempts, f.updated_at, m.stale_score,
                      f.fact_type
               FROM memory_meta m
               JOIN facts f ON m.key = f.key
               WHERE m.validation_status = 'pending'
               ORDER BY m.stale_score DESC
               LIMIT ?""",
            (batch_limit,),
        )
        rows = cur.fetchall()
        stats = {"validated": 0, "keep": 0, "update": 0, "archive": 0,
                 "deferred_truncated": 0, "deferred_invalid_reason": 0,
                 "skipped_429": 0, "broke_on_429": False,
                 "batch_limit": batch_limit, "pending_selected": len(rows)}

        consecutive_429 = 0
        _RATE_LIMIT_BREAK_THRESHOLD = 3
        for key, value, prior_attempts, fact_updated_at, stale_score, fact_type in rows:
            # partial 而不是给 to_thread 传 kwargs：asyncio.to_thread 的重载只在
            # 纯位置参数下匹配得上，传 kwargs 会被 pyright 判成 "No parameter named ..."
            call = functools.partial(
                self._call_llm_validate_with_429_flag, key, value,
                fact_type=fact_type, fact_updated_at=fact_updated_at,
                stale_days=stale_score,
            )
            result, is_rate_limited = await asyncio.to_thread(call)
            if is_rate_limited:
                consecutive_429 += 1
                stats["skipped_429"] += 1
                if consecutive_429 >= _RATE_LIMIT_BREAK_THRESHOLD:
                    remaining = len(rows) - stats["validated"] - stats["skipped_429"]
                    logger.warning(
                        f"memory.maintain 连续 {consecutive_429} 次 429，"
                        f"跳过本次剩余 {remaining} 条 pending 验证（下次 maintain 周期再处理）"
                    )
                    stats["broke_on_429"] = True
                    break
                continue
            # 非 429 的失败（None）或成功都重置计数
            consecutive_429 = 0
            if result is None:
                continue

            action = result.get("suggested_action", "keep")
            if action not in ("keep", "update", "archive"):
                action = "keep"
            reason = str(result.get("reason", ""))[:500]
            attempts = int(prior_attempts or 0) + 1
            now_str = datetime.now().isoformat()

            _preview, truncated = build_validation_preview(value, preview_chars)
            if action == "archive" and is_invalid_archive_reason(reason):
                # 依据本身不成立（模型的"知识截止"不是这条记忆失效的证据），不能动手
                if attempts < _ARCHIVE_CONFIRM_ATTEMPTS:
                    self._defer_verdict(
                        key, attempts,
                        f"〔第 {attempts} 次：判决理由属'超出知识截止/未来未发生'类无效依据，"
                        f"未采纳（原由：{reason}）〕",
                    )
                    stats["deferred_invalid_reason"] += 1
                    continue
                # 反复只给无效依据 ⇒ 等于"没有成立的失效证据"，按 when-in-doubt-keep 保留
                action = "keep"
                reason = (
                    "多次仅以'超出知识截止/未来未发生'类无效依据要求归档，"
                    "按'无有效失效证据'保留"
                )[:500]

            if action == "archive" and truncated:
                if attempts < _ARCHIVE_CONFIRM_ATTEMPTS:
                    # 验证器没看全整条内容，这次不给死刑：留在 pending，下轮再判。
                    self._defer_verdict(
                        key, attempts,
                        f"〔第 {attempts} 次：正文超出 {preview_chars} 字验证窗口，"
                        f"archive 建议未采纳（原由：{reason}）〕",
                    )
                    stats["deferred_truncated"] += 1
                    continue
                # 反复超窗：这条不是"过时"，是"臃肿到没法判读"，交回写它的人压缩重写
                action = "update"
                reason = (
                    f"正文长期超出 {preview_chars} 字验证窗口，无法判读有效性，"
                    f"需压缩重写后重验（原 archive 建议不采纳）"
                )[:500]

            # 5-9: 单条 UPDATE 也包 _write_lock（"无法长时间持锁"仅针对整循环串行化；
            # 单条写语句不包锁会与并发写交叉，违背 store 写锁约定）
            with self.store._write_lock:
                self.store.conn.execute(
                    """UPDATE memory_meta SET
                           last_validated_at = ?,
                           validation_status = ?,
                           validation_note = ?,
                           validate_attempts = ?
                       WHERE key = ?""",
                    (now_str, action, reason, attempts, key),
                )
            stats["validated"] += 1
            stats[action] += 1

        # T06：commit 包入 _write_lock（run_validate 是 async + 循环含 await，
        # 无法长时间持锁，仅 commit 串行化）
        with self.store._write_lock:
            self.store.conn.commit()
        return stats

    def get_status(self) -> dict:
        """返回维护器状态"""
        cur = self.store.conn.execute(
            "SELECT value FROM schema_info WHERE key = 'maintainer_last_run'"
        )
        row = cur.fetchone()
        last_run = row[0] if row else None

        cur2 = self.store.conn.execute(
            "SELECT validation_status, COUNT(*) FROM memory_meta GROUP BY validation_status"
        )
        status_counts = {r[0]: r[1] for r in cur2.fetchall()}

        # 验证用 LLM tier 范围（来自 USE_CASE_REGISTRY.memory_validate.default_tier）
        model_tier = None
        try:
            from server.llm_pool.types import USE_CASE_REGISTRY
            uc = USE_CASE_REGISTRY.get("memory_validate")
            if uc:
                model_tier = list(uc.default_tier)
        except Exception:
            pass

        return {
            "last_run": last_run,
            "stale_threshold_days": self.config.maintain_stale_days,
            "interval_hours": self.config.maintain_interval_hours,
            "validate_enabled": self.config.maintain_enable_validate,
            "model_tier": model_tier,
            "status_counts": status_counts,
            "stale_total": sum(v for k, v in status_counts.items() if k != "keep"),
        }

    def _call_llm_validate_with_429_flag(
        self, key: str, value: str,
        *, fact_type: str | None = None,
        fact_updated_at: str | None = None, stale_days: float | None = None,
    ) -> tuple[dict | None, bool]:
        """通过 pool.call() 调用 LLM 验证单条记忆，返回 (result, is_rate_limited)

        v15：统一走 pool.call(use_case="memory_validate", retries=1)。
        pool 内部已实现 model 级降级 / cooldown / 429 不计 consecutive_fails，
        本函数只需根据 pool 返回的 error 判断是否是限流类型，用于批量熔断。

        is_rate_limited=True 表示 pool 返回限流类错误（429/no available keys/quota），
        调用方（run_validate）据此做熔断 break，避免连发数十次失败调用。
        其他失败仍返回 (None, False)，按原逻辑跳过单条。
        """
        try:
            from server.llm_pool.singleton import get_pool

            system_prompt, user_prompt = self._build_validate_prompt(
                key, value,
                fact_type=fact_type, fact_updated_at=fact_updated_at,
                stale_days=stale_days,
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            result = get_pool().call(
                messages=messages,
                temperature=0.1,
                max_tokens=16384,
                retries=1,
                use_case="memory_validate",
            )
            if not result.get("ok"):
                err = result.get("error", "") or ""
                err_lower = err.lower()
                # 识别限流类错误（pool 内部 429 / 全池不可用 / quota 耗尽）
                is_rate_limited = (
                    "429" in err
                    or "rate limit" in err_lower
                    or "quota" in err_lower
                    or "no available keys" in err_lower
                )
                if is_rate_limited:
                    logger.info(f"LLM 验证限流 (key={key}): {err[:120]}")
                else:
                    logger.warning(f"LLM 验证调用失败 (key={key}): {err[:150]}")
                return None, is_rate_limited

            raw = (result.get("content") or "").strip()
            return self._parse_validate_response(raw), False
        except Exception as e:
            err_str = f"{type(e).__name__}: {str(e)[:200]}"
            logger.warning(f"LLM 验证调用异常 (key={key}): {err_str[:150]}")
            return None, False

    def _build_validate_prompt(
        self, key: str, value: str,
        *, fact_type: str | None = None, fact_updated_at: str | None = None,
        stale_days: float | None = None,
    ) -> tuple[str, str]:
        """构建验证 prompt（system + user）

        v5 重写（2026-10-03 记忆库体检）——旧 prompt 有两处会把好记忆判死：

        1. `value[:800]`：实测 120/151 条 fact 超 800 字符，81 条的前 800 字符
           以 JSON 元数据脚手架开头（`{"source": "manual", "updated_at": ...}`，
           66 条 fact 把整套元数据抄进了正文）。模型看到的全是信封不是信，
           于是给出"内容被截断／乱码／无实际语义／技术细节"的 archive 判决。
           现在改为 `build_validation_preview`：剥元数据、优先取正文键、
           窗口放大到 `validate_preview_chars`，超窗则显式标注并触发延判。
        2. "与当前项目无关的个人数据（如游戏抽卡记录）应判 archive"：这是把
           工程域当成唯一合法记忆域的项目中心偏见。对个人管家来说，生活事务
           恰恰是最需要长期记忆的部分，这条指令等于主动销毁未来的核心资产。
           现在明文禁止"因为属于个人生活域"而判 archive。
        """
        type_guidance = _FACT_TYPE_GUIDANCE.get(fact_type or "", _FACT_TYPE_GUIDANCE[""])
        today_str = datetime.now().strftime("%Y-%m-%d")
        age_note = f"\n今天: {today_str}"
        if fact_updated_at:
            age_note += f"｜记忆最后更新: {fact_updated_at}"
        if stale_days is not None:
            age_note += f"（已 {int(float(stale_days))} 天未更新）"

        system_prompt = (
            "你是记忆维护助手。要判断的问题只有一个：**这条记忆所说的内容现在是否仍然成立**。\n"
            "不要判断它\"对某个任务有没有用\"——记忆的价值不由单次任务决定，也没人给它发过任务清单。\n"
            "个人生活事务（抽卡、账单、游戏、聊天、爱好、健康）与工程事务在这一点上完全平等，"
            "**禁止因为内容属于个人生活域或与项目无关而判 archive**。\n"
            "时间线以 user 消息里给出的「今天」为准。**你的知识截止日期与本库无关**："
            "记忆里的日期晚于你的训练截止、或你根本没见过那件事，"
            "都**不构成内容无效的证据**，绝不得以\"超出知识截止/属于未来未发生的事件\"判 archive"
            "（这类记忆是本库最新、最该保留的一批）。\n"
            "你必须只返回 JSON，不要输出其他任何内容。\n"
            "JSON 格式：\n"
            '{"is_accurate": true/false, "reason": "简短原因(中文,50字内)", "suggested_action": "keep|update|archive"}\n'
            "\n"
            "通用判据：\n"
            "- keep：仍然成立；或者它作为历史记录本身仍然可信"
            "（\"某次实验得出的实测结论\"不会因为实验结束而失效）\n"
            "- update：主体仍然可用，但部分表述已变化、写法需要修正，或内容过于臃肿需要重写\n"
            "- archive：仅限以下三种情况之一："
            "(a) 说的是一次性动作已经完成、且不含任何可复用信息（如\"某次下载已完成\"）；"
            "(b) 已被更晚的事实明确取代；"
            "(c) 空壳、乱码或纯占位符，读不出任何可判断的命题\n"
            "\n"
            "本条记忆的类型判据：" + type_guidance + "\n"
            "原则：不确定时选 keep（保留优于误删）。\n"
            "如果正文末尾出现\"（正文过长，已截断）\"，说明你没有看全整条内容——"
            "此时不得以\"看不全/内容不完整\"为理由判 archive。"
        )
        preview, truncated = build_validation_preview(
            value, self.config.validate_preview_chars
        )
        if truncated:
            preview += "\n（正文过长，已截断）"

        user_prompt = (
            f"记忆 key: {key}\n"
            f"fact_type: {fact_type or '未标注'}{age_note}\n"
            f"\n记忆正文:\n{preview}\n"
            f"\n请判断这条记忆所说的内容现在是否仍然成立，返回 JSON。"
        )
        return system_prompt, user_prompt

    @staticmethod
    def _parse_validate_response(raw: str) -> dict | None:
        """解析 LLM 返回的 JSON，容错处理 markdown 包裹和文本包裹"""
        if not raw:
            return None

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [line for line in lines if not line.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()

        try:
            data = json.loads(cleaned)
            if not isinstance(data, dict):
                return None
            if "is_accurate" not in data or "suggested_action" not in data:
                return None
            if data["suggested_action"] not in ("keep", "update", "archive"):
                data["suggested_action"] = "keep"
            return data
        except json.JSONDecodeError:
            match = re.search(r"\{[^}]+\}", raw)
            if match:
                try:
                    data = json.loads(match.group())
                    if isinstance(data, dict) and "suggested_action" in data:
                        if data["suggested_action"] not in ("keep", "update", "archive"):
                            data["suggested_action"] = "keep"
                        return data
                except json.JSONDecodeError:
                    pass
            return None

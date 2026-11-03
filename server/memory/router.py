"""三层记忆系统 FastAPI 路由

完全向后兼容 /memory/* 端点，同时新增语义搜索等接口。
"""

import asyncio
import logging
import time

from fastapi import APIRouter, Body, HTTPException, Path, Query
from pydantic import Field, field_validator

from lib.schema import BaseSchema
from server.memory.manager import get_memory_manager
from server.memory.norms import NORMS_BUDGET_CHARS, build_norms_bundle
from server.memory.wrapup import list_pending_candidates, submit_wrapup
from server.memory.write import MemoryWriteError, review_candidate

logger = logging.getLogger("parcae.memory.router")

router = APIRouter(prefix="/memory", tags=["memory"])

_KEY_PATTERN = r'^[a-zA-Z0-9_-]{1,128}$'

# v6.1 T29：5 类 closed taxonomy（spec D7）
# 君子协议：未知类型落 NULL + log warning，不强制拒绝
VALID_FACT_TYPES = ("user", "feedback", "project", "reference", "experience")

# v6.1 T31：staleness 阈值表（spec D14，按 fact_type 分类）
STALENESS_THRESHOLDS_DAYS = {
    "user": 90,        # 用户偏好/身份极稳定
    "feedback": 30,    # 反馈会随偏好演进而变化
    "project": 7,      # 项目迭代快，架构/约定易改
    "reference": 30,   # 链接/命令可能失效但不一定过时
    "experience": 14,  # 库版本更新会修复坑点，经验易过时
}


# ─── Request/Response 模型 ──────────────────────────────────

class MemorySetRequest(BaseSchema):
    """写入记忆请求（兼容旧接口，支持深度合并 + v3 结构化字段）

    v3 新增：fact_type / occurred_at / consumption_contexts / trigger_keywords
    这些字段作为 fact 的元数据写入 facts 表的独立列，不属于 data。
    若同时通过 data 内部传入同名键，请求顶层字段优先（覆盖）。

    v6.1 T29：fact_type 收敛为 5 类 closed（user/feedback/project/reference/experience）。
    君子协议：未知类型不强制拒绝，validator 落 None + log warning。
    """
    data: dict = Field(..., description="记忆数据")
    merge: bool = Field(True, description="是否深度合并（旧接口兼容）")
    # v3 结构化字段（可选）
    fact_type: str | None = Field(
        None,
        description="事实类型（v6.1 closed）: user|feedback|project|reference|experience",
    )
    occurred_at: str | None = Field(
        None, description="事实发生时间 ISO 字符串（区别于写入时间）"
    )
    consumption_contexts: list[str] | None = Field(
        None,
        description=(
            "消费场景列表，哪些 task_type 应读取此记忆。"
            '支持通配 "*"（全场景）和 "scope.*"（如 "recurring.*"）'
        ),
    )
    trigger_keywords: list[str] | None = Field(
        None,
        description="触发关键词列表，任务描述/用户消息出现这些词时应读取此记忆",
    )

    @field_validator("fact_type")
    @classmethod
    def _validate_fact_type(cls, v: str | None) -> str | None:
        """v6.1 T29：closed taxonomy 君子协议。

        - None / 空 → 透传 None（不改）
        - 5 类之内 → 透传
        - 5 类之外（含旧值 preference/transaction）→ 落 None + log warning
          （后端不强制拒绝，但 prompt 已强约束；旧值由 migrate_fact_type_v6_1.py 处理）
        """
        if v is None or v == "":
            return None
        if v in VALID_FACT_TYPES:
            return v
        logger.warning(
            "fact_type=%r 不在 closed taxonomy %r 内（君子协议：落 NULL，不强制拒绝）",
            v, VALID_FACT_TYPES,
        )
        return None


class WrapupQuote(BaseSchema):
    """收尾提交里的一条用户原话（不可改写）。"""
    text: str = Field(..., description="原文；落库后永不 UPDATE")
    speaker: str = Field("user", description="user / agent_relay / system")
    project_id: str | None = Field(None, description="归属项目；缺省用请求级 project_id")
    sensitivity: str = Field("personal", description="public / private-eng / personal")
    captured_at: str | None = Field(None, description="这句话发生的时间（ISO）")


class WrapupCandidate(BaseSchema):
    """一条记忆候选。

    **故意不把 kind / sensitivity 写成 Literal**：非法值要以
    ``status='rejected'`` + 原因码回给 agent（不静默丢），而不是 422 整单打回。
    """
    content: str = Field(..., description="正文（必填）")
    key: str | None = Field(None, description="稳定键；不传则按内容生成")
    title: str | None = Field(None, description="短标题")
    summary: str | None = Field(None, description="1-2 句摘要，注入只看这个")
    kind: str | None = Field(
        None, description="norm/fact/pitfall/preference/procedure/reference/current_state"
    )
    scope: str | None = Field(None, description="global / project:<id> / topic:<id>")
    origin: str | None = Field(
        None, description="user_quote / agent_extracted / system / tool；缺省按有无 quote 推断"
    )
    sensitivity: str | None = Field(None, description="public / private-eng / personal")
    evidence_refs: list[str] | None = Field(None, description="可机器核对的证据")
    source_quote_ids: list[int] | None = Field(None, description="指向 source_quotes.id")
    quote_text: str | None = Field(None, description="内联原话，服务端先落 source_quotes 再引用")
    quote_speaker: str | None = Field(None, description="内联原话的说话人")
    must_read: int | None = Field(None, description="1 = 必读，只允许 kind='norm'")
    expires_at: str | None = Field(None, description="kind='current_state' 必填")
    confidence: float = Field(0.8, ge=0.0, le=1.0)
    fact_type: str | None = Field(None, description="旧分类，兼容用（不传则 NULL）")
    occurred_at: str | None = Field(None, description="事实发生时间（ISO）")
    consumption_contexts: list[str] | None = None
    trigger_keywords: list[str] | None = None


class MemoryWrapupRequest(BaseSchema):
    """收尾提交：原话 + 记忆候选，一次交。"""
    run_id: str | None = Field(None, description="run/会话码标识")
    project_id: str | None = Field(None, description="项目归属，给 quotes 兜底")
    default_scope: str = Field("global", description="候选没给 scope 时用它")
    quotes: list[WrapupQuote] = Field(default_factory=list)
    candidates: list[WrapupCandidate] = Field(default_factory=list)


class CandidateReviewRequest(BaseSchema):
    """DL5 待审候选处置：accept 走写入漏斗，reject 只改状态。"""
    action: str = Field(..., description="accept / reject")
    overrides: dict = Field(default_factory=dict, description="采纳前人工修正的字段")
    reason: str | None = Field(None, description="退回原因，默认 manual_reject")


class MemorySearchRequest(BaseSchema):
    """语义搜索请求"""
    query: str = Field(..., min_length=1, max_length=1000, description="搜索查询")
    top_k: int = Field(10, ge=1, le=100, description="返回结果数")
    since: float | None = Field(None, description="起始时间戳")
    until: float | None = Field(None, description="结束时间戳")
    source: str | None = Field(None, description="消息来源过滤")


class MemoryRecordRequest(BaseSchema):
    """手动记录请求"""
    content: str = Field(..., min_length=1, description="内容")
    source: str = Field("manual", description="来源: agent/user/tool/system/manual")
    role: str = Field("assistant", description="角色: user/assistant/tool/system")
    key: str | None = Field(None, pattern=_KEY_PATTERN, description="可选 key（兼容 KV 接口，仅字母/数字/下划线/连字符，1-128 字符）")
    session_id: str | None = Field(None, description="会话 ID")


# ─── 向后兼容端点 ───────────────────────────────────────────

@router.get("/status", operation_id="memory_status",
            summary="记忆系统状态")
async def memory_status():
    """获取记忆系统状态（增强版）"""
    mgr = get_memory_manager()
    return await asyncio.to_thread(mgr.get_status)


@router.get("/list", operation_id="memory_list",
            summary="列出所有记忆 key")
async def memory_list():
    """列出所有记忆 key 及摘要"""
    mgr = get_memory_manager()
    keys = await asyncio.to_thread(mgr.list_keys)
    return {"keys": keys, "total": len(keys)}


@router.post("/cleanup/orphaned", operation_id="memory_cleanup_orphaned",
             summary="清理孤立记忆")
async def memory_cleanup_orphaned():
    """清理超过 orphan_cleanup_days 天的已压缩消息和超过 summary_cleanup_days 天的旧摘要"""
    mgr = get_memory_manager()

    # 5-6: 循环逐条删除 + 摘要清理为同步重活，包 to_thread（与同文件 search/list 一致）
    def _do_cleanup() -> dict:
        cutoff = time.time() - mgr.config.orphan_cleanup_days * 86400

        # 清理旧消息
        old_messages = mgr.store.query_messages(until=cutoff, compressed=1, limit=1000)
        deleted_msgs = 0
        for msg in old_messages:
            # 只删除已压缩的旧消息
            mgr.store.delete_message(msg["id"])
            deleted_msgs += 1

        # 清理旧摘要（5-9: 改走 store 方法，DELETE+commit 包入 _write_lock）
        summary_cutoff = time.time() - mgr.config.summary_cleanup_days * 86400
        deleted_summaries = mgr.store.delete_summaries_before(summary_cutoff)

        return {
            "deleted_messages": deleted_msgs,
            "deleted_summaries": deleted_summaries,
            "message_cutoff_days": mgr.config.orphan_cleanup_days,
            "summary_cutoff_days": mgr.config.summary_cleanup_days,
            "status": "ok",
        }

    return await asyncio.to_thread(_do_cleanup)


# ─── 新增端点 ───────────────────────────────────────────────

@router.post("/search", operation_id="memory_search",
             summary="语义搜索记忆")
async def memory_search(req: MemorySearchRequest):
    """混合语义搜索（向量 + BM25）

    支持降级模式：嵌入模型不可用时自动降级为 BM25-only。

    v6.1 T31：返回结果每条追加 staleness 字段（memory_age / staleness_warning / trust_recall_hint）。
    """
    mgr = get_memory_manager()
    results = await asyncio.to_thread(
        mgr.search,
        query=req.query,
        top_k=req.top_k,
        since=req.since,
        until=req.until,
        source=req.source,
    )
    # T31: 追加 staleness 字段（results 是 list[dict]，原地修改）
    _enrich_with_staleness(results)
    return {"results": results, "total": len(results), "query": req.query}


@router.get("/timeline", operation_id="memory_timeline",
            summary="时间线查询")
async def memory_timeline(
    since: float | None = Query(None, description="起始时间戳"),
    until: float | None = Query(None, description="结束时间戳"),
    source: str | None = Query(None, description="消息来源过滤"),
    limit: int = Query(50, ge=1, le=500, description="返回数量"),
):
    """按时间范围查询消息"""
    mgr = get_memory_manager()
    messages = await asyncio.to_thread(
        mgr.timeline, since=since, until=until, source=source, limit=limit
    )
    return {"messages": messages, "total": len(messages)}


@router.post("/compress", operation_id="memory_compress",
             summary="手动触发记忆压缩")
async def memory_compress(force: bool = Query(False, description="是否强制压缩")):
    """手动触发记忆压缩

    将旧消息压缩为摘要，节省存储空间和上下文窗口。
    """
    mgr = get_memory_manager()
    result = await mgr.compress.compress(force=force)
    return result


@router.post("/record", operation_id="memory_record",
             summary="手动记录交互")
async def memory_record(req: MemoryRecordRequest):
    """手动记录一条交互（也可由自动记录器调用）"""
    mgr = get_memory_manager()

    # 5-6: insert + ONNX 语义索引（数十至数百 ms）+ Recent 写入为同步重活，包 to_thread
    def _do_record() -> dict:
        msg_id = mgr.store.insert_message(
            content=req.content,
            source=req.source,
            role=req.role,
            key=req.key,
            session_id=req.session_id,
        )
        # 建立语义索引
        mgr.semantic.index_message(msg_id, req.content)
        # 写入 Recent 层
        mgr.recent.add(req.content, source=req.source, role=req.role, key=req.key)
        return {"id": msg_id, "status": "ok"}

    return await asyncio.to_thread(_do_record)


@router.post("/wrapup", operation_id="memory_wrapup",
             summary="收尾提交：原话 + 记忆候选 → 去重 → 落库")
async def memory_wrapup(req: MemoryWrapupRequest):
    """DL3 的唯一收尾落库入口。

    返回逐条去向（accepted / merged / duplicate / rejected + 原因码），
    **被拒的也会留在 memory_candidates 里**，不会静默丢。
    """
    mgr = get_memory_manager()
    payload = req.model_dump()
    return await asyncio.to_thread(
        submit_wrapup, mgr.store, payload, default_scope=req.default_scope
    )


@router.get("/candidates", operation_id="memory_candidates",
            summary="待审候选（被拒的写入，供人工改后重提）")
async def memory_candidates(
    limit: int = Query(50, ge=1, le=200),
    run_id: str | None = Query(None, max_length=64, description="只看某个 run 的候选"),
):
    mgr = get_memory_manager()
    items = await asyncio.to_thread(list_pending_candidates, mgr.store, limit, run_id)
    return {"items": items, "count": len(items)}


@router.post("/candidates/{candidate_id}/review", operation_id="memory_candidate_review",
             summary="采纳 / 退回一条待审候选（DL5）")
async def memory_candidate_review(candidate_id: int, req: CandidateReviewRequest):
    mgr = get_memory_manager()
    try:
        result = await asyncio.to_thread(
            review_candidate, mgr.store, candidate_id, req.action,
            overrides=req.overrides, reason=req.reason,
        )
    except MemoryWriteError as err:
        raise HTTPException(
            status_code=err.http_status,
            detail={"code": err.code, "message": err.message},
        ) from err
    return {"ok": True, **result}


@router.get("/stats", operation_id="memory_stats",
            summary="详细记忆统计")
async def memory_stats():
    """获取详细的记忆系统统计信息"""
    mgr = get_memory_manager()

    # 5-6: get_status + BM25/摘要统计为同步 SQLite 读，包 to_thread
    def _do_stats() -> dict:
        stats = mgr.get_status()
        # 添加 BM25 统计
        stats["bm25"] = mgr.store.get_bm25_stats()
        # 添加摘要统计
        summaries = mgr.store.query_summaries(limit=5)
        stats["recent_summaries"] = len(summaries)
        return stats

    return await asyncio.to_thread(_do_stats)


@router.post("/reindex", operation_id="memory_reindex",
             summary="重建语义索引")
async def memory_reindex():
    """重建向量索引和 BM25 统计（维护操作）"""
    mgr = get_memory_manager()

    # 5-6: 全量向量索引重建在消息多时分钟级，包 to_thread（参照 compress 的包装模式）
    def _do_reindex() -> dict:
        # 重建向量索引
        vector_result = mgr.semantic.rebuild_vector_index()
        # 重建 BM25 统计
        bm25_result = mgr.semantic.bm25.rebuild_stats()
        return {"vectors": vector_result, "bm25": bm25_result}

    result = await asyncio.to_thread(_do_reindex)
    return {**result, "status": "ok"}


# ─── 维护器端点（必须在 /{key} 动态路由之前） ────────────────

class MaintainRequest(BaseSchema):
    """手动触发维护请求"""
    force: bool = Field(False, description="是否强制运行（忽略 should_run 检查）")


@router.post("/maintain", operation_id="memory_maintain",
             summary="手动触发记忆维护（垃圾预过滤 + 老化 + 指标聚合）")
async def memory_maintain(req: MaintainRequest):
    """手动触发记忆维护器：预过滤垃圾 + 重算 stale_score + 指标日聚合。

    通常自动触发（每 maintain_interval_hours 小时），也可手动调用。
    force=True 时忽略 should_run 检查立即执行。

    2026-10-31 起没有 `force_revalidate`：LLM 验证器已删除，没有可重跑的判决。
    """
    mgr = get_memory_manager()
    if not req.force and not mgr.maintainer.should_run():
        return {"status": "skipped", "reason": "not_due_yet",
                "last_run": mgr.maintainer.get_status().get("last_run")}
    result = await mgr.maintainer.run()
    return {"status": "ok", "result": result}


@router.get("/maintain/status", operation_id="memory_maintain_status",
            summary="记忆维护器状态")
async def memory_maintain_status():
    """获取维护器状态：上次运行时间、stale 数量、已归档数量（验证器已删除）"""
    mgr = get_memory_manager()
    return mgr.maintainer.get_status()


# ─── Search Tracer 端点 (v3 C) ────────────────────────────────────

@router.get("/search/traces", operation_id="memory_search_traces_list",
            summary="最近检索 trace 列表")
async def memory_search_traces_list(
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    since_id: int | None = Query(None, description="游标分页（仅返回 id > since_id 的记录）"),
):
    """列出最近 search_traces 记录（不含 candidates/final_results 详情）"""
    mgr = get_memory_manager()
    traces = await asyncio.to_thread(
        mgr.search_tracer.get_recent, limit=limit, since_id=since_id
    )
    return {"traces": traces, "total": len(traces)}


# ─── Context Projection 端点 (v4, unknown-outcome-evidence T15) ───────
# 回答"召回之后到底上了什么"：入选/丢弃/前后两序/内容指纹/有效性标记。
# 纯观测——这些端点不改变任何检索或注入行为。

@router.get("/projections", operation_id="memory_projections_list",
            summary="上下文投影留痕列表")
async def memory_projections_list(
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    since_id: int | None = Query(None, description="游标分页（仅返回 id > since_id）"),
    task_type: str | None = Query(None, description="按消费场景过滤"),
    surface: str | None = Query(None, description="按注入面过滤，如 agent_guide.must_read"),
):
    """列出最近的上文投影记录（轻量列，详情走单条端点）"""
    from server.memory import projection as _proj

    mgr = get_memory_manager()
    items = await asyncio.to_thread(
        _proj.list_projections,
        mgr.store.conn,
        limit=limit,
        since_id=since_id,
        task_type=task_type,
        surface=surface,
    )
    return {"projections": items, "total": len(items)}


@router.get("/projections/{projection_id}", operation_id="memory_projections_detail",
            summary="单条上下文投影详情")
async def memory_projections_detail(projection_id: int):
    """取一条投影：selected/dropped/rank_before/rank_after/validation_flags/digest"""
    from server.memory import projection as _proj

    mgr = get_memory_manager()
    detail = await asyncio.to_thread(_proj.get_projection, mgr.store.conn, projection_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"projection_id={projection_id} 不存在")
    return detail


@router.get("/search/traces/{trace_id}", operation_id="memory_search_traces_detail",
            summary="单条检索 trace 详情")
async def memory_search_traces_detail(trace_id: int):
    """获取单条 trace 详情（含 candidates、final_results 完整 JSON）"""
    mgr = get_memory_manager()
    detail = await asyncio.to_thread(mgr.search_tracer.get_detail, trace_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"trace_id={trace_id} 不存在")
    return detail


@router.delete("/search/traces/cleanup", operation_id="memory_search_traces_cleanup",
               summary="清理过期检索 trace")
async def memory_search_traces_cleanup(
    days: int | None = Query(None, ge=1, le=365,
                              description="覆盖 retention_days 配置（可选）"),
):
    """清理超过 retention_days 的旧 trace

    days 参数可选，未传时使用配置中的 search_tracer_retention_days。
    """
    mgr = get_memory_manager()
    if days is not None:
        # 临时覆盖 retention_days
        original = mgr.search_tracer.retention_days
        mgr.search_tracer.retention_days = days
        try:
            result = await asyncio.to_thread(mgr.search_tracer.cleanup_old)
        finally:
            mgr.search_tracer.retention_days = original
    else:
        result = await asyncio.to_thread(mgr.search_tracer.cleanup_old)
    return {"status": "ok", "result": result}


# ─── Evidence Ledger 端点 (v3 A) ────────────────────────────────────

@router.get("/evidence/recent", operation_id="memory_evidence_recent",
            summary="最近证据集列表")
async def memory_evidence_recent(
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
):
    """列出最近 evidence_ledger 记录（按 id DESC）"""
    mgr = get_memory_manager()
    evidences = await asyncio.to_thread(mgr.evidence_ledger.get_recent, limit=limit)
    return {"evidences": evidences, "total": len(evidences)}


@router.get("/evidence/{evidence_id}", operation_id="memory_evidence_detail",
            summary="单条证据详情")
async def memory_evidence_detail(evidence_id: int):
    """获取单条证据详情（含 conflicting_ids 解析）"""
    mgr = get_memory_manager()
    detail = await asyncio.to_thread(mgr.evidence_ledger.get_detail, evidence_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"evidence_id={evidence_id} 不存在")
    return detail


@router.delete("/evidence/cleanup", operation_id="memory_evidence_cleanup",
               summary="清理过期证据")
async def memory_evidence_cleanup(
    days: int | None = Query(None, ge=1, le=365,
                              description="覆盖 evidence_audit_keep_days 配置（可选）"),
):
    """清理超过 evidence_audit_keep_days 的旧证据

    days 参数可选，未传时使用配置中的 evidence_audit_keep_days。
    """
    mgr = get_memory_manager()
    result = await asyncio.to_thread(mgr.evidence_ledger.cleanup_old, days)
    return {"status": "ok", "result": result}


# ─── 动态 key 端点必须放在固定路径之后 ───────────────────────

def _normalize_as_of(as_of: str) -> str:
    """把 ``as_of`` 规范化成与 ``fact_revisions`` 时间列可比的定长文本。

    只给日期（10 字符）时按 **当日 23:59:59** 解释：用户问"9 月 30 日是什么值"，
    要的是那一天**结束时**的状态；按零点解释会把当天发生的所有变化都排除掉，
    是这类查询最常见的错解。
    """
    s = as_of.strip()
    if len(s) == 10:
        return s + " 23:59:59"
    if len(s) == 16 and " " in s:
        return s + ":00"
    return s


def _memory_get_as_of(mgr, key: str, as_of: str) -> dict:
    """查 key 在 ``as_of`` 时刻"当时为真"的版本（v6 P2 版本模型）。

    返回的是**历史视图**，因此形状与默认路径不同：默认路径把 value 的字段平铺在
    顶层（客户端详情页依赖），而这里显式区分 ``value``（原文）与 ``data``（解析后的
    dict），并附 ``origin`` 说明这条是从历史表取的、还是"当前值在当时就已经为真"。
    """
    import json

    norm = _normalize_as_of(as_of)
    hit = mgr.store.resolve_fact_as_of(key, norm)
    if hit is None:
        raise HTTPException(
            status_code=404,
            detail=f"记忆 '{key}' 在 {norm} 为止没有任何版本（as_of 早于本 key 的全部历史）",
        )
    value, origin, rev = hit
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    return {
        "key": key,
        "as_of": norm,
        "origin": origin,   # 'revision' = 取自历史表；'current' = 当前值当时已为真
        "value": value,
        "data": parsed if isinstance(parsed, dict) else None,
        "revision": (
            {"id": rev["id"], "valid_at": rev["valid_at"], "invalid_at": rev["invalid_at"]}
            if rev else None
        ),
    }


@router.get("/norms", operation_id="memory_norms",
            summary="DL1 上下文红线 bundle（norms）")
async def memory_norms(
    project_id: str | None = Query(
        None, max_length=128,
        description="项目标识（不含 'project:' 前缀）；不传只取 global",
    ),
    budget: int = Query(
        NORMS_BUDGET_CHARS, ge=500, le=20_000,
        description="字符预算；默认 5,000（2026-10-27 由 2,000 上调）",
    ),
):
    """取 norms bundle：安全类优先、超预算截断、去向全回显。

    DL4 的 `context.next` 第一页直接调它；当前先以 REST 暴露，方便单独验收。
    """
    mgr = get_memory_manager()
    return await asyncio.to_thread(
        build_norms_bundle, mgr.store, project_id=project_id, budget=budget
    )


@router.get("/{key}", operation_id="memory_get",
            summary="读取记忆")
async def memory_get(
    key: str = Path(..., pattern=_KEY_PATTERN, description="记忆 key（仅字母、数字、下划线、连字符）"),
    as_of: str | None = Query(
        None,
        description="查'当时为真'的版本（v6）：'YYYY-MM-DD' 按当日 23:59:59 解释，"
                    "也接受 'YYYY-MM-DD HH:MM:SS[.ffffff]'。不传则返回当前值，默认路径不变",
    ),
):
    """按 key 读取记忆（兼容旧 KV 接口，同时搜索 facts 和 messages）

    v6.1 T31：返回结果追加 staleness 字段（memory_age / staleness_warning / trust_recall_hint）。
    仅当 fact_type 在 5 类 closed 内且超阈值时填 staleness_warning。

    v6（P2 版本模型）：传 ``as_of`` 时改查 ``fact_revisions``，返回**当时为真**的版本；
    不传时行为与改动前逐字一致（走 ``mgr.get``），以免动到按 key 读 facts 的所有既有代码。
    """
    mgr = get_memory_manager()
    if as_of is not None:
        return await asyncio.to_thread(_memory_get_as_of, mgr, key, as_of)

    data = await asyncio.to_thread(mgr.get, key)
    if data is None:
        raise HTTPException(status_code=404, detail=f"记忆 '{key}' 不存在")
    # P2-3：路由层注入 key 字段——mgr.get 不返回 key，客户端详情页读 data["key"] 拿不到导致标题恒空白
    data["key"] = key
    _enrich_with_staleness(data)
    return data


@router.post("/{key}", operation_id="memory_set",
             summary="写入记忆")
async def memory_set(key: str = Path(..., pattern=_KEY_PATTERN), req: MemorySetRequest = Body(...)):  # noqa: B008
    """写入记忆（兼容旧 KV 接口，同时写入 facts 和 messages）

    支持深度合并：如果 key 已存在，新数据会与旧数据深度合并。

    v3 新增：支持 fact_type / occurred_at / consumption_contexts / trigger_keywords
    顶层字段，这些字段会作为 fact 元数据写入 facts 表的独立列，
    供 find_consumable_memories 用 SQL 索引快速过滤。
    若 data 内部也含同名键，顶层字段优先（覆盖）。
    """
    mgr = get_memory_manager()

    # 5-6: merge 读 + ONNX 侧写入为同步重活，包 to_thread（与 memory_status/search 一致）
    def _do_set() -> dict:
        if req.merge:
            # 深度合并模式。
            # ⚠️ 不能用 mgr.get() 当"现有值"：它把 facts.value 的 dict **平铺在顶层**，
            # 再挂上 source / updated_at / access_count / confidence / created_at /
            # fact_type 等一整套**信封键**。拿它去深合并，这些元数据会被抄进 value
            # 并落库（实测 66/151 条受害，value 里混着 "source":"manual" 这类与内容
            # 无关的字段）。这里直接取 facts.value **原文**来合并，从源头避开信封。
            # 用 get_fact_meta 而非 get_fact：后者会顺带把 access_count +1，
            # 而"写一次"不应该被算作"读一次"。
            raw = mgr.store.get_fact_meta(key)
            existing = None
            if raw and raw.get("value"):
                try:
                    import json as _json
                    parsed = _json.loads(raw["value"])
                    existing = parsed if isinstance(parsed, dict) else None
                except (ValueError, TypeError):
                    existing = None
            merged = _deep_merge(existing, req.data) if existing else req.data
        else:
            merged = req.data

        # v3 结构化字段（仅传非 None 的，None 表示未指定，保留现有值）
        structured: dict = {}
        if req.fact_type is not None:
            structured["fact_type"] = req.fact_type
        if req.occurred_at is not None:
            structured["occurred_at"] = req.occurred_at
        if req.consumption_contexts is not None:
            structured["consumption_contexts"] = req.consumption_contexts
        if req.trigger_keywords is not None:
            structured["trigger_keywords"] = req.trigger_keywords

        result = mgr.set(key, merged, structured=structured or None)

        # 可见性（不是拦截）：写入方没填 fact_type 时，把"这条因此过滤不到"回显给他自己看。
        # NULL 的成因不是校验漏了，而是调用方压根没传（`_validate_fact_type` 只在收到非法值时才落
        # NULL）。存量已于 2026-10-05 收口：key 前缀即类型命名（reference_/preference_/project_/
        # feedback_）或 value 自证过类型的都补标了，剩 4 条交用户判断。实际数字用
        # `SELECT COALESCE(fact_type,'<NULL>'), COUNT(*) FROM facts GROUP BY 1` 现查。
        # 判据按落库后的行读，不按请求参数猜：merge 更新时旧行可能本来就有类型。
        meta = mgr.store.get_fact_meta(key) or {}
        if not meta.get("fact_type"):
            result["guidance"] = (
                "这条记忆的 fact_type 为空：按类型过滤（find_consumable_memories 的 SQL、"
                "staleness 阈值表、中枢 L7 的形态巡检）都不会命中它。"
                f"候选取值 {VALID_FACT_TYPES}，下次写入带上 fact_type=…；"
                "留空不会导致写入失败。"
            )
        return result

    return await asyncio.to_thread(_do_set)


@router.delete("/{key}", operation_id="memory_delete",
               summary="删除记忆")
async def memory_delete(key: str = Path(..., pattern=_KEY_PATTERN)):
    """删除指定 key 的记忆（C1：改为**失效**，可用 POST /{key}/restore 还原）"""
    mgr = get_memory_manager()
    deleted = mgr.delete(key)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"记忆 '{key}' 不存在")
    return {"status": "deleted", "key": key, "recoverable": True, "archived": True}


@router.post("/{key}/restore", operation_id="memory_restore",
             summary="恢复已失效的记忆")
async def memory_restore(key: str = Path(..., pattern=_KEY_PATTERN)):
    """把一条已失效的记忆恢复为活跃（C1 配套）。

    与 DELETE 成对：DELETE 不再物理删，误删可以撤回。
    """
    mgr = get_memory_manager()
    if not mgr.store.restore_fact(key):
        exists = mgr.store.get_fact_raw(key) is not None
        if not exists:
            raise HTTPException(status_code=404, detail=f"记忆 '{key}' 不存在")
        raise HTTPException(status_code=409, detail=f"记忆 '{key}' 未处于失效状态")
    return {"status": "restored", "key": key}


# ─── 内部工具 ───────────────────────────────────────────────

def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并字典"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# v6.1 T31：staleness 警告计算（spec D14 + D9）
_TRUST_RECALL_HINT = "引用文件/函数前请用 file_read/file_grep 验证仍存在"


def _format_memory_age(days: int) -> str:
    """相对时间格式（回收 v6-07:260-271）。

    0 天 → "today"，1 天 → "yesterday"，其他 → "N days ago"。
    模型对相对时间推理优于 ISO 时间戳。
    """
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def _compute_staleness(fact: dict) -> dict:
    """计算单条 fact 的 staleness 字段（spec D14）。

    Args:
        fact: 含 fact_type / updated_at 字段的 dict（来自 facts 表或 memory_index 项）

    Returns:
        dict 含三个字段（仅追加，不修改原 dict）：
        - memory_age: str — 相对时间格式（today/yesterday/N days ago）
        - staleness_warning: str | None — 超阈值时的警告文案；未超阈值或 fact_type 未知时为 None
        - trust_recall_hint: str — 常驻提示"引用前请验证"

    阈值表（STALENESS_THRESHOLDS_DAYS）：
    - user 90 / feedback 30 / project 7 / reference 30 / experience 14
    - fact_type=NULL 或未知 → 不算 staleness（staleness_warning=None）
    """
    from datetime import datetime as _dt

    fact_type = fact.get("fact_type")
    updated_at = fact.get("updated_at")

    # 默认值
    memory_age: str | None = None
    staleness_warning: str | None = None

    if updated_at:
        try:
            # facts 表的 updated_at 格式：'YYYY-MM-DD HH:MM:SS'（sqlite datetime('now', 'localtime')）
            updated_dt = _dt.strptime(str(updated_at)[:19], "%Y-%m-%d %H:%M:%S")
            days_since = (_dt.now() - updated_dt).days
            memory_age = _format_memory_age(days_since)

            # 仅当 fact_type 在 closed taxonomy 内时算 staleness
            if fact_type and fact_type in STALENESS_THRESHOLDS_DAYS:
                threshold = STALENESS_THRESHOLDS_DAYS[fact_type]
                if days_since > threshold:
                    staleness_warning = (
                        f"⚠️ 此记忆最后更新于 {memory_age}"
                        f"（fact_type={fact_type} 类型阈值 {threshold} 天），可能过时。"
                        f"{_TRUST_RECALL_HINT}。"
                    )
        except (ValueError, TypeError):
            # updated_at 解析失败 → memory_age=None, staleness_warning=None
            pass

    return {
        "memory_age": memory_age,
        "staleness_warning": staleness_warning,
        "trust_recall_hint": _TRUST_RECALL_HINT,
    }


def _enrich_with_staleness(data: dict | list) -> dict | list:
    """对 memory_get / memory_search 返回结果追加 staleness 字段。

    - dict：单条 fact，直接追加
    - list：多条结果，逐条追加
    - 其他类型：透传不动

    幂等：已有 staleness_warning 字段时跳过（防重复计算）。
    """
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "staleness_warning" not in item:
                item.update(_compute_staleness(item))
        return data
    if isinstance(data, dict) and "staleness_warning" not in data:
        data.update(_compute_staleness(data))
    return data

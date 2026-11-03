"""headless-agent-session REST 端点（Ticket 07 + 直连创建）

spec: temp/sdd/headless-agent-session/spec.md
ticket: Ticket 07

端点：
- POST /headless/sessions：直接创建一个 headless 会话（不必绕 LoopManager cron）
- POST /headless/sessions/{session_id}/interrupt：中断 headless 会话（主会话或 judge 会话）

中断行为（butler-runtime T6 起）：
- 会话跑在 butler runtime 里，本端点只把中断转发给 runtime 的
  `POST /sessions/{session_id}/interrupt`；后端不再维护 interrupt registry。
- 主会话中断 → runtime 中断运行 → 本端 run_main_agent 拿到 interrupted 终态 →
  HeadlessSessionAction 不调 judge → 整体 failed
- judge 会话中断 → judge 终态非 completed → 降级 uncertain → 整体 failed

设计要点：
- runtime 未运行时返回 503；session 不在跑（未开始/已结束）返回 404
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException

from lib.agent_runtime.runtime_client import RuntimeCallError, RuntimeClient, RuntimeUnavailable
from lib.schema import BaseSchema

router = APIRouter(prefix="/headless", tags=["headless"])


def _runtime_client() -> RuntimeClient:
    """构造 runtime 客户端；测试可 monkeypatch 本函数注入替身。"""
    return RuntimeClient()


class InterruptResponse(BaseSchema):
    """POST /headless/sessions/{session_id}/interrupt 响应。"""
    session_id: str
    interrupted: bool
    message: str = ""


class HeadlessSessionCreateRequest(BaseSchema):
    """POST /headless/sessions 请求。

    字段与 `[loops.headless_session.*]` config 段同名；留 None / 空串走 HeadlessConfig 默认值。
    刻意不暴露 `one_shot`——它的含义是"跑完把所属 loop task 暂停"，直连创建的会话没有所属 task。
    """
    trigger_text: str = ""
    skill: str = ""
    model: str = ""
    judge_model: str = ""
    max_iterations: int | None = None
    wall_clock_budget_secs: int | None = None
    judge_max_iterations: int | None = None
    judge_wall_clock_budget_secs: int | None = None


class HeadlessSessionCreateResponse(BaseSchema):
    """POST /headless/sessions 响应（HeadlessSessionAction 的返回字典按本 schema 过滤）。"""
    success: bool
    main_session_id: str = ""
    judge_session_id: str = ""
    judge_verdict: str = ""
    judge_reason: str = ""
    error: str = ""
    skipped: bool = False
    skip_reason: str = ""
    outcome_unknown: bool = False
    elapsed_secs: float = 0.0


_CREATE_CONFIG_FIELDS = (
    "model", "judge_model", "max_iterations", "wall_clock_budget_secs",
    "judge_max_iterations", "judge_wall_clock_budget_secs",
)


@router.post(
    "/sessions",
    operation_id="headless_create_session",
    response_model=HeadlessSessionCreateResponse,
)
async def create_headless_session(
    req: HeadlessSessionCreateRequest,
) -> HeadlessSessionCreateResponse:
    """直接创建一个 headless 会话：主 agent 跑任务 → judge 判成败 → 留档。

    与 loop 里的 `headless_session` action 走**同一条编排**（`HeadlessSessionAction.execute`），
    差别只有两点：不经过 LoopManager 的 cron 或 `/loop/tasks/{id}/run`；`one_shot` 强制 False。

    同步执行——请求会占到会话跑完为止（受 `wall_clock_budget_secs` 约束），与
    `/loop/tasks/{id}/run` 的语义一致。跑完照常写一条 memory 和一条 completed/failed 的 WIP。

    Raises:
        HTTPException 400: trigger_text 与 skill 都为空（否则只会产出一条 skipped 的空会话）
    """
    if not req.trigger_text.strip() and not req.skill.strip():
        raise HTTPException(status_code=400, detail="trigger_text 和 skill 至少给一个")

    from server.activity_tracker.loop_actions import HeadlessSessionAction

    cfg: dict = {"trigger_text": req.trigger_text, "skill": req.skill, "one_shot": False}
    for name in _CREATE_CONFIG_FIELDS:
        value = getattr(req, name)
        if value is not None and value != "":
            cfg[name] = value

    started = time.time()
    result = await HeadlessSessionAction().execute({"config": cfg, "task_id": ""})
    result["elapsed_secs"] = round(time.time() - started, 2)

    # BaseSchema 是 extra='forbid'：action 的返回字典一旦多出本 schema 没声明的键，
    # 不过滤就会在构造响应时抛 ValidationError（同 /vision/status 的处理）
    payload = {k: v for k, v in result.items()
               if k in HeadlessSessionCreateResponse.model_fields}
    return HeadlessSessionCreateResponse(**payload)



@router.post(
    "/sessions/{session_id}/interrupt",
    operation_id="headless_interrupt_session",
    response_model=InterruptResponse,
)
async def interrupt_headless_session(session_id: str) -> InterruptResponse:
    """中断 headless 会话（主会话或 judge 会话）。

    Args:
        session_id: headless 会话 ID（headless-{uuid} 或 headless-judge-{uuid}）

    Returns:
        InterruptResponse：interrupted=True + message

    Raises:
        HTTPException 404: 会话不在运行（未开始或已结束）
        HTTPException 503: butler runtime 未运行
        HTTPException 502: runtime 返回错误
    """
    try:
        result = await _runtime_client().interrupt(session_id)
    except RuntimeUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail=f"butler runtime unavailable: {e}",
        ) from e
    except RuntimeCallError as e:
        raise HTTPException(
            status_code=502,
            detail=f"runtime interrupt failed: {e.code}: {e.message}",
        ) from e

    if not result.get("interrupted"):
        raise HTTPException(
            status_code=404,
            detail=(
                f"headless session {session_id!r} not running "
                "(not started or already finished)"
            ),
        )

    return InterruptResponse(
        session_id=session_id,
        interrupted=True,
        message="interrupt signal sent to runtime",
    )

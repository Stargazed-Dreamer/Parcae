"""chat-mgmt-probe Ticket 03: 探针 HTTP 路由

端点：
- POST /probe/runs：启动探针会话，返回 ProbeSummary

通过 parcae_advanced_tool(tool="probe_run", params={...}) 网关调用。
operation_id="probe_run"。

butler-runtime T6 起：探针引擎在 runtime 进程里跑（MockLLM + 独立探针库），
本路由只做 HTTP 代理，后端不再持有对话循环。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import Field

from lib.agent_runtime.runtime_client import (
    RuntimeCallError,
    RuntimeClient,
    RuntimeUnavailable,
)
from lib.schema import BaseSchema
from server.probe.runner import ProbeParams, ProbeSummary, run_probe

logger = logging.getLogger("parcae.probe.router")

router = APIRouter(prefix="/probe", tags=["probe"])


def _runtime_client() -> RuntimeClient:
    """构造 runtime 客户端；测试可 monkeypatch 本函数注入替身。"""
    return RuntimeClient()


# ============================================================================
# Pydantic 模型
# ============================================================================


class ProbeRunRequest(BaseSchema):
    """POST /probe/runs 请求体。"""

    trigger_text: str = Field(..., description="触发任务文本（如 hello）")
    mode: str = Field("script", description='"script" | "pattern"')
    script: list = Field(default_factory=list, description="script 模式：每条 str | dict")
    pattern: str = Field("", description="pattern 模式：pattern 名（Ticket 04）")
    use_stream: bool = Field(False, description="是否走流式路径（Ticket 05）")
    trigger_approval: bool = Field(False, description="是否触发审批路径（Ticket 05）")
    max_context_messages: int = Field(0, description="上下文压缩触发阈值，0=不限制（Ticket 05）")
    auto_cleanup: bool = Field(True, description="跑完是否自动删除会话")
    db_path: str = Field(
        "",
        description="独立 DB 路径；空则用探针专属 temp/probe/agent.db（不会写生产库）",
    )
    base_url: str = Field(
        "",
        description="工具调用用的后端 base_url；留空=runtime 自己的 [server] 地址",
    )
    persist: bool = Field(
        False,
        description="是否落盘 6 个 JSON 到 temp/probe/runs/<sid>/（默认 False=纯内存跑）",
    )
    include_full: bool = Field(
        False,
        description="摘要是否附全文（默认只给 80 字符 head；纯内存验证需置 True）",
    )


class ProbeSummaryResponse(BaseSchema):
    """POST /probe/runs 响应体（ProbeSummary 的 Pydantic 版本）。"""

    session_id: str
    mode: str
    status: str
    messages_count: int
    events_count: int
    tool_calls_count: int
    messages_preview: list[dict]
    tool_calls_preview: list[dict]
    llm_calls: list[dict]
    run_dir: str
    cleaned_up: bool


# ============================================================================
# 端点
# ============================================================================


@router.post(
    "/runs",
    operation_id="probe_run",
    response_model=ProbeSummaryResponse,
)
async def probe_run_endpoint(req: ProbeRunRequest) -> ProbeSummaryResponse:
    """启动探针会话。

    用 MockLLM 替代真 LLM 跑完整对话生命周期，采集数据并返回摘要
    （默认纯内存：persist=False 不落盘、只给 80 字符 head；需要全量数据时
    传 persist=True 落 temp/probe/runs/、传 include_full=True 摘要带全文）。
    用于评估项目自己的对话管理系统（EventStore + SessionRunner + 工具调用）。

    Ticket 02 范围：script 模式 + 纯文本 + 非流式 + 无工具。
    Ticket 04+ 扩展 pattern 模式 + tool_call。
    Ticket 05+ 扩展流式 / 审批 / 压缩。
    """
    logger.info(
        "probe_run request: mode=%s trigger_len=%d use_stream=%s auto_cleanup=%s",
        req.mode, len(req.trigger_text), req.use_stream, req.auto_cleanup,
    )
    try:
        params = ProbeParams(
            trigger_text=req.trigger_text,
            mode=req.mode,
            script=req.script,
            pattern=req.pattern,
            use_stream=req.use_stream,
            trigger_approval=req.trigger_approval,
            max_context_messages=req.max_context_messages,
            auto_cleanup=req.auto_cleanup,
            db_path=req.db_path,
            base_url=req.base_url,
            persist=req.persist,
            include_full=req.include_full,
        )
        summary: ProbeSummary = await run_probe(params, client=_runtime_client())
        return ProbeSummaryResponse(
            session_id=summary.session_id,
            mode=summary.mode,
            status=summary.status,
            messages_count=summary.messages_count,
            events_count=summary.events_count,
            tool_calls_count=summary.tool_calls_count,
            messages_preview=summary.messages_preview,
            tool_calls_preview=summary.tool_calls_preview,
            llm_calls=summary.llm_calls,
            run_dir=summary.run_dir,
            cleaned_up=summary.cleaned_up,
        )
    except RuntimeUnavailable as e:
        raise HTTPException(status_code=503, detail=f"butler runtime unavailable: {e}") from e
    except RuntimeCallError as e:
        raise HTTPException(
            status_code=e.status_code if 400 <= e.status_code < 500 else 500,
            detail=f"{e.code}: {e.message}",
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("probe_run failed")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e

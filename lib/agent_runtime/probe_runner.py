"""chat-mgmt-probe Ticket 02: 探针主逻辑（butler-runtime T6 迁入 lib）

组装 RunnerDeps（MockLLM + 真 EventStore + 真 ToolRegistry + 真 HttpClientToolExecutor），
调 SessionFacade.start(mode="probe") 跑完一个会话，采集 messages/events/tool_calls/llm 请求响应，
返回结构化摘要；`persist=True` 时才额外落盘到 temp/probe/runs/<session_id>/。

归属：引擎在 runtime 进程里跑（runtime `/probe/run` 调用），8766 后端只做代理。
放 lib 而不是 server，是为了让 8766 后端不再持有对话循环（T6 验收：
`grep -rn "SessionFacade" server/` 为 0）。

落盘策略（2026-10-09 整改）：
- JSON 落盘**默认关闭**（`persist=False`）—— 摘要本身就在内存里，跑完即可评估。
  默认路径纯内存跑，不产生任何磁盘增长。
- 需要全量落盘给下游工具读时显式传 `persist=True`；此时每次落盘后按 `_MAX_RUNS`
  裁剪 RUNS_DIR，只保留最近若干次 run 目录（默认 persist 不落盘，此上限兜
  "显式反复 persist" 这一条会累积的路径）。
- 落盘根目录为 `temp/probe/runs/`（探针专属子目录）：仍归 temp/ 管辖、
  `system.temp_cleanup` 可覆盖，但收进独立 `temp/probe/` 命名空间，
  不再与 temp 根的一次性清理候选混在一起（旧 `temp/chat_mgmt_probe/` 就散在根下）。
- 默认 DB 从生产库 `data/client/agent.db` 改为探针专属 `temp/probe/agent.db`，
  探针不再往生产对话库写。

区别：llm_gateway 用 MockLLM 替代 LLMPoolGateway，mode="probe" 替代 "headless"。

tracer bullet 范围（Ticket 02）：
- 纯文本单轮（script=["hi"]）
- 非流式（use_stream=False）
- 无工具调用（script 不含 tool_calls）
- 无审批 / 无压缩

Ticket 04+ 扩展 pattern 模式 + tool_call。
Ticket 05+ 扩展流式 / 审批 / 压缩。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from lib.agent_runtime.compactor import Compactor, ContextOverflow
from lib.agent_runtime.event_store import EventStore
from lib.agent_runtime.facade import SessionFacade
from lib.agent_runtime.http_client_tool_executor import HttpClientToolExecutor
from lib.agent_runtime.mock_llm import MockLLM
from lib.agent_runtime.mock_tool_executor import MockToolExecutor
from lib.agent_runtime.tool_registry import ToolRegistry
from lib.agent_runtime.types import (
    TOOL_SAFETY_APPROVAL_REQUIRED,
    LLMRequest,
    LLMResponse,
    Message,
    RunnerConfig,
    RunnerDeps,
    ToolCall,
    ToolResult,
    ToolResultVariant,
)

logger = logging.getLogger("parcae.probe.runner")

# 探针专属落盘区（temp/probe/）：收进独立子命名空间，不再散在 temp 根；
# 被 .gitignore 的 `temp/*` 整目录排除，`system.temp_cleanup` 也会覆盖。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROBE_HOME = _PROJECT_ROOT / "temp" / "probe"
# JSON 落盘根目录（仅 persist=True 时写入）
RUNS_DIR = PROBE_HOME / "runs"
# 探针默认 DB（db_path 留空时用；⛔ 绝不回退到生产库 data/client/agent.db）
DEFAULT_PROBE_DB = PROBE_HOME / "agent.db"

def default_base_url() -> str:
    """后端 base_url：运行时声明优先（端口可能被顺延），回退 config.toml。"""
    try:
        from lib.runtime_endpoint import resolve_base_url

        return resolve_base_url(probe_health=False)
    except Exception:
        return "http://127.0.0.1:8766"


# 兼容旧引用；运行时请用 default_base_url()（常量是加载时快照，端口顺延后过期）
DEFAULT_BASE_URL = default_base_url()

# 摘要中 content 前缀长度（控制上下文压力）；include_full=True 时另附全文，不受此限
_CONTENT_HEAD_LEN = 80

# RUNS_DIR 保留上限：只保留最近 _MAX_RUNS 个 run 目录（按 mtime）。
# 默认 persist=False 不落盘，此上限只兜"显式反复 persist=True"这一条会累积的路径
# （temp/probe/ 虽可被 system.temp_cleanup 覆盖，但那个任务不常跑，此上限兜底）。
_MAX_RUNS = 50


# ============================================================================
# 数据模型
# ============================================================================


@dataclass
class ProbeParams:
    """探针入参（Ticket 02 最小集，Ticket 04/05 扩展）。"""

    trigger_text: str
    mode: str = "script"  # "script" | "pattern"
    script: list[Any] = field(default_factory=list)  # script 模式：每条 str | dict
    pattern: str = ""  # pattern 模式：pattern 名
    use_stream: bool = False  # Ticket 05 扩展
    trigger_approval: bool = False  # Ticket 05 扩展
    max_context_messages: int = 0  # Ticket 05 扩展（0=不限制）
    auto_cleanup: bool = True
    db_path: str = ""  # 空则用探针专属 DEFAULT_PROBE_DB（不写生产库）
    base_url: str = field(default_factory=default_base_url)
    persist: bool = False  # 是否落盘 6 个 JSON 到 RUNS_DIR（默认关，纯内存跑）
    include_full: bool = False  # 摘要是否附全文（默认只给 80 字符 head）


@dataclass
class ProbeSummary:
    """探针返回摘要（HTTP 返回 + 落盘索引）。"""

    session_id: str
    mode: str
    status: str
    messages_count: int
    events_count: int
    tool_calls_count: int
    # [{seq, role, source, content_head}]；include_full=True 时每条另附 content（全文）
    messages_preview: list[dict]
    # [{name, status}]；include_full=True 时另附 args / safety
    tool_calls_preview: list[dict]
    # [{request_messages_len, request_system_head, response_content_head,
    #   response_tool_calls_count}]；include_full=True 时另附 request_system /
    #   request_messages（组装后的完整 prompt）/ response_content（全文）
    llm_calls: list[dict]
    run_dir: str  # persist=False 时为空串（未落盘）
    cleaned_up: bool


# ============================================================================
# 序列化辅助
# ============================================================================


def _content_to_str(content: Any) -> str:
    """Message.content 可能是 str 或 list[dict]（OpenAI content blocks），转成预览字符串。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # OpenAI content blocks: [{"type": "text", "text": "..."}, ...]
        parts = []
        for block in content:
            if isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block["text"]))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return " ".join(parts)
    return str(content)


def _message_to_dict(msg: Message) -> dict:
    """Message dataclass → 可 JSON 序列化的 dict。"""
    return {
        "id": msg.id,
        "session_id": msg.session_id,
        "seq": msg.seq,
        "role": msg.role,
        "content": msg.content,  # 可能是 str 或 list[dict]，json.dumps 都能处理
        "source": msg.source,
        "visible": msg.visible,
        "tool_call_id": msg.tool_call_id,
        "tool_calls": msg.tool_calls,
        "thinking": msg.thinking,
        "created_at": msg.created_at,
    }


def _llm_request_to_dict(req: LLMRequest) -> dict:
    """LLMRequest dataclass → dict。messages 转成 dict list。"""
    return {
        "messages": [_message_to_dict(m) for m in req.messages],
        "model": req.model,
        "tools": req.tools,
        "tool_choice": req.tool_choice,
        "system": req.system,
        "max_tokens": req.max_tokens,
    }


def _llm_response_to_dict(resp: LLMResponse) -> dict:
    """LLMResponse dataclass → dict。"""
    return {
        "content": resp.content,
        "tool_calls": resp.tool_calls,
        "usage": resp.usage,
        "stop_reason": resp.stop_reason,
        "model": resp.model,
        # B2（spec D2/D3）：删 cost_usd 字段，本地无法准确算 cost
        "thinking": resp.thinking,
    }


def _message_preview(msg: Message, include_full: bool) -> dict:
    """Message → 摘要预览项。include_full=True 时另附 content 全文。

    为什么需要 full 档：head 只有 80 字符，判"跑通没/几轮"够，
    判 prompt 拼接 / 回灌格式 / 流式合并不够（要看全文）。
    """
    text = _content_to_str(msg.content)
    item: dict = {
        "seq": msg.seq,
        "role": msg.role,
        "source": msg.source,
        "content_head": text[:_CONTENT_HEAD_LEN],
    }
    if include_full:
        item["content"] = text
        item["tool_call_id"] = msg.tool_call_id
        item["thinking"] = msg.thinking
    return item


def _tool_call_preview(tc: ToolCall, include_full: bool) -> dict:
    """ToolCall → 摘要预览项。include_full=True 时另附 args / safety（判工具调用状态机用）。"""
    item: dict = {"name": tc.name, "status": tc.status}
    if include_full:
        item["args"] = tc.args
        item["args_raw"] = tc.args_raw
        item["safety"] = tc.safety
    return item


def _llm_call_preview(req: LLMRequest, resp: LLMResponse, include_full: bool) -> dict:
    """LLMRequest + LLMResponse → 摘要预览项。

    include_full=True 时另附 request_system / request_messages（组装后的完整 prompt，
    判 prompt 拼接与回灌格式的关键）/ response_content（全文）。
    """
    item: dict = {
        "request_messages_len": len(req.messages),
        "request_system_head": (req.system or "")[:_CONTENT_HEAD_LEN],
        "response_content_head": (resp.content or "")[:_CONTENT_HEAD_LEN],
        "response_tool_calls_count": len(resp.tool_calls),
    }
    if include_full:
        item["request_system"] = req.system or ""
        item["request_messages"] = [
            {"seq": m.seq, "role": m.role, "source": m.source,
             "content": _content_to_str(m.content)}
            for m in req.messages
        ]
        item["request_tools"] = req.tools
        item["response_content"] = resp.content or ""
        item["response_thinking"] = resp.thinking
        item["response_stop_reason"] = resp.stop_reason
    return item


# ============================================================================
# 主逻辑
# ============================================================================


def _generate_probe_session_id() -> str:
    """生成 probe-xxx session_id。"""
    return f"probe-{uuid.uuid4().hex[:12]}"


def _build_mock_llm(params: ProbeParams) -> MockLLM:
    """根据 params 构造 MockLLM。

    Ticket 02：script 模式（每条 str）。
    Ticket 04：script dict 格式 + pattern 模式（5 种 callable）。
    Ticket 05：script dict 支持 raise_context_overflow 字段（这一轮抛 ContextOverflow）。
    """
    if params.mode == "script":
        # Ticket 05: 如果任何 script item 含 raise_context_overflow，转 callable 模式
        # （因为 MockLLM.script 模式只能消费 str | LLMResponse，不能抛异常）
        has_overflow = any(
            isinstance(item, dict) and item.get("raise_context_overflow")
            for item in params.script
        )
        if has_overflow:
            return MockLLM(callable=_build_script_callable_with_overflow(params.script))
        # 无 overflow：原 script 模式
        script_items: list[str | LLMResponse] = []
        for item in params.script:
            if isinstance(item, str):
                script_items.append(item)
            elif isinstance(item, dict):
                # Ticket 04: dict → LLMResponse 转换
                script_items.append(_dict_to_llm_response(item))
            else:
                script_items.append(str(item))
        return MockLLM(script=script_items)
    elif params.mode == "pattern":
        # Ticket 04: pattern 模式用 callable
        return MockLLM(callable=_build_pattern_callable(params.pattern))
    else:
        raise ValueError(f"unknown mode: {params.mode}")


def _build_script_callable_with_overflow(script: list[Any]):
    """Ticket 05: 构造 script callable，支持 raise_context_overflow 字段。

    每次调用消费一条 script item：
    - str → LLMResponse(content=str)
    - dict with raise_context_overflow=True → 抛 ContextOverflow
    - dict 其他 → _dict_to_llm_response 转换
    - LLMResponse → 原样返回
    """
    state = {"index": 0}

    def _callable(req: LLMRequest) -> LLMResponse:
        if state["index"] >= len(script):
            raise StopIteration(
                f"probe script exhausted: index={state['index']}, len={len(script)}"
            )
        item = script[state["index"]]
        state["index"] += 1

        if isinstance(item, str):
            return LLMResponse(
                content=item,
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                stop_reason="end_turn",
                model="mock-llm",
            )
        if isinstance(item, dict):
            if item.get("raise_context_overflow"):
                raise ContextOverflow(
                    item.get("overflow_message", "probe mock context overflow"),
                    status_code=item.get("status_code", 413),
                )
            return _dict_to_llm_response(item)
        if isinstance(item, LLMResponse):
            return item
        # 兜底
        return LLMResponse(content=str(item), model="mock-llm")

    return _callable


def _dict_to_llm_response(d: dict) -> LLMResponse:
    """Ticket 04: dict → LLMResponse 转换。

    支持字段：
    - content: str（默认 ""）
    - tool_calls: list[dict]（OpenAI 格式，默认 []）
    - stop_reason: str（默认 "end_turn" 或 "tool_use" 如果有 tool_calls）
    - thinking: str（默认 ""）
    - usage: dict（默认空）
    - raise_context_overflow: bool（Ticket 05 新增，True 时这一轮抛 ContextOverflow）
      注：raise_context_overflow 不在 LLMResponse 字段里，由 _build_mock_llm 检测并转 callable
    """
    content = d.get("content", "")
    tool_calls = d.get("tool_calls", [])
    stop_reason = d.get("stop_reason", "tool_use" if tool_calls else "end_turn")
    thinking = d.get("thinking", "")
    usage = d.get("usage", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        thinking=thinking,
        usage=usage,
        model="mock-llm",
    )


def _build_approval_mock_executor() -> MockToolExecutor:
    """Ticket 05: 构造审批拒绝路径的 mock ToolExecutor。

    真实审批路径需要 server 弹 GUI 弹窗，不适合探针场景。
    本函数返回 MockToolExecutor，模拟"approval_required 工具被用户拒绝"路径：
    - tool_call.safety == "approval_required" 或 name 属于典型写入操作（exec_cmd/exec_apply_patch/exec_python）
      → 返回 is_error=True "User denied approval: probe mock"
    - 其他工具 → 返回 is_error=True "Tool not executed (probe mock executor)"

    这样可以测试 runner 对审批拒绝路径的处理是否完善（tool_calls 表 status=failed + transition(next_turn) + 模型回灌 tool_result）。
    """
    approval_required_names = {"exec_cmd", "exec_apply_patch", "exec_python", "exec_kill", "exec_send_input"}

    def _mock_execute(tc: ToolCall) -> ToolResult:
        is_approval = (
            tc.safety == TOOL_SAFETY_APPROVAL_REQUIRED
            or tc.name in approval_required_names
        )
        if is_approval:
            return ToolResult(
                tool_call_id=tc.id,
                content="User denied approval: probe mock (trigger_approval=True)",
                variant=ToolResultVariant.USER_DENIED,
            )
        # 非 approval 工具也拒绝（探针在 trigger_approval 模式下不走真 HTTP）
        return ToolResult(
            tool_call_id=tc.id,
            content=f"Tool '{tc.name}' not executed (probe mock executor in approval mode)",
            variant=ToolResultVariant.ERROR,
            error_type="probe_mock_blocked",
        )

    return MockToolExecutor(callable=_mock_execute)


def _make_openai_tool_call(call_id: str, name: str, args: dict | None = None) -> dict:
    """构造 OpenAI 格式的 tool_call dict（参考 tests/test_v6_lite_t02.py）。"""
    import json as _json
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": _json.dumps(args or {})},
    }


def _build_pattern_callable(pattern: str):
    """Ticket 04: 构造 pattern callable（根据 pattern 名返回 LLMRequest→LLMResponse 的函数）。

    5 种 pattern：
    - text_only：每轮返回纯文本 "MockLLM text_only response #N"
    - tool_call_then_text：第一轮 tool_call（memory_list），第二轮纯文本
    - always_tool_call：每轮都返回 tool_call（memory_list）
    - streaming_text：返回带 thinking 字段的响应（配合 use_stream 测流式合并）
    - refuse：返回 "I refuse to do this"
    """
    if pattern == "text_only":
        def _text_only(req: LLMRequest) -> LLMResponse:
            # 简化：不追踪编号，每次返回固定文本
            return LLMResponse(
                content="MockLLM text_only response",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                stop_reason="end_turn",
                model="mock-llm",
            )
        return _text_only

    elif pattern == "tool_call_then_text":
        # 用闭包变量追踪是否已经调过 tool_call
        state = {"called_tool": False}
        def _tc_then_text(req: LLMRequest) -> LLMResponse:
            if not state["called_tool"]:
                state["called_tool"] = True
                return LLMResponse(
                    content="let me check memory",
                    tool_calls=[_make_openai_tool_call("call-probe-tc-1", "memory_list", {})],
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    stop_reason="tool_use",
                    model="mock-llm",
                )
            return LLMResponse(
                content="done checking memory",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                stop_reason="end_turn",
                model="mock-llm",
            )
        return _tc_then_text

    elif pattern == "always_tool_call":
        def _always_tc(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="calling tool again",
                tool_calls=[_make_openai_tool_call(f"call-probe-always-{id(req)}", "memory_list", {})],
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                stop_reason="tool_use",
                model="mock-llm",
            )
        return _always_tc

    elif pattern == "streaming_text":
        def _streaming_text(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="final answer after thinking",
                thinking="let me think about this... the answer is straightforward",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                stop_reason="end_turn",
                model="mock-llm",
            )
        return _streaming_text

    elif pattern == "refuse":
        def _refuse(req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="I refuse to do this",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                stop_reason="end_turn",
                model="mock-llm",
            )
        return _refuse

    else:
        raise ValueError(f"unknown pattern: {pattern}. Available: text_only / tool_call_then_text / always_tool_call / streaming_text / refuse")


async def run_probe(params: ProbeParams) -> ProbeSummary:
    """探针主入口：组装 deps → 跑 facade.start → 采集 → 落盘 → 摘要。

    Args:
        params: ProbeParams

    Returns:
        ProbeSummary
    """
    sid = _generate_probe_session_id()
    logger.info(
        "probe start: session=%s mode=%s trigger_len=%d use_stream=%s auto_cleanup=%s",
        sid, params.mode, len(params.trigger_text), params.use_stream, params.auto_cleanup,
    )

    # 组装 MockLLM
    mock_llm = _build_mock_llm(params)

    # 组装 EventStore（探针专属 db 或调用方指定的独立 db）
    # ⛔ db_path 留空时**不再**回退到 EventStore 的默认生产库 data/client/agent.db
    #   （旧行为会让 MCP / HTTP 路径的探针把 session 写进生产对话库）
    db_path = params.db_path or str(DEFAULT_PROBE_DB)
    store = EventStore(db_path=db_path)
    store.init()

    # 组装 ToolRegistry + HttpClientToolExecutor（真工具调用）
    # Ticket 05: trigger_approval=True 时改用 mock executor 模拟审批拒绝路径
    # （真实审批路径需要 server 弹 GUI 弹窗，不适合探针场景）
    registry = ToolRegistry(base_url=params.base_url)
    if params.trigger_approval:
        # mock 模式：不 refresh registry（避免依赖后端在线）
        executor = _build_approval_mock_executor()
        logger.info("probe trigger_approval=True: using mock executor (no real HTTP)")
    else:
        try:
            registry.refresh()
        except Exception as e:
            logger.warning("ToolRegistry refresh failed (will run without tools): %s", e)
        executor = HttpClientToolExecutor(registry=registry, base_url=params.base_url)

    # 组装 interrupt_event
    interrupt_event = asyncio.Event()

    # 组装 RunnerDeps
    # Ticket 05: max_context_messages>0 时注入 Compactor，配合 script raise_context_overflow 测压缩路径
    deps_kwargs: dict[str, Any] = {
        "llm_gateway": mock_llm,
        "event_store": store,
        "tool_executor": executor,
        "tool_registry": registry,
        "interrupt_event": interrupt_event,
    }
    if params.max_context_messages > 0:
        # Compactor 的 max_context_tokens 用很小值（让 should_compact 容易达到阈值）
        # 实际触发由 MockLLM 抛 ContextOverflow 主动驱动（runner 捕获后调 compactor.compact）
        deps_kwargs["compactor"] = Compactor(
            max_context_tokens=params.max_context_messages * 100,  # 粗估：每条 ~100 tokens
            tail_keep=2,  # 保留尾部 2 条（便于测试观察压缩效果）
        )
        logger.info(
            "probe max_context_messages=%d: compactor injected (max_tokens=%d, tail_keep=2)",
            params.max_context_messages,
            params.max_context_messages * 100,
        )
    deps = RunnerDeps(**deps_kwargs)

    # 组装 RunnerConfig（frozen dataclass）
    # Ticket 02：use_stream=False（tracer bullet 最窄路径）
    runner_config = RunnerConfig(
        session_id=sid,
        model="mock-llm",  # MockLLM 不在乎 model
        max_iterations=10,  # 探针场景不需要 50 轮
        # B2（spec D2/D3）：删 max_budget_usd，本地无法准确算 cost
        use_stream=params.use_stream,
    )

    # 调 SessionFacade.start（mode="probe"）
    facade = SessionFacade(deps=deps, config=runner_config)
    cleaned_up = False
    try:
        outcome = await facade.start(sid, params.trigger_text, mode="probe")
        status = outcome.status
        logger.info(
            "probe completed: session=%s status=%s iterations=%d",
            sid, status, outcome.iterations,
        )
    except Exception:
        logger.exception("probe failed: session=%s", sid)
        status = "failed"

    # 采集
    messages = await store.load_messages(sid)
    # 探针落盘是排障用途，需要看到 live 增量（X10 默认回放不含）
    events = await store.load_events(sid, include_live=True)
    tool_calls = await store.load_tool_calls(sid)
    llm_requests = mock_llm.requests
    llm_responses = mock_llm.responses
    session = await store.get_session(sid)

    # 落盘（persist=True 才写；4-11: 同步 JSON 写移入线程，避免阻塞事件循环）
    # 默认 persist=False ⇒ 纯内存跑，run_dir 返回空串
    run_dir = RUNS_DIR / sid if params.persist else None

    def _dump_all() -> None:
        assert run_dir is not None
        run_dir.mkdir(parents=True, exist_ok=True)
        _dump_json(run_dir / "messages.json", [_message_to_dict(m) for m in messages])
        _dump_json(run_dir / "events.json", [asdict(e) for e in events])
        _dump_json(run_dir / "tool_calls.json", [asdict(tc) for tc in tool_calls])
        _dump_json(run_dir / "llm_requests.json", [_llm_request_to_dict(r) for r in llm_requests])
        _dump_json(run_dir / "llm_responses.json", [_llm_response_to_dict(r) for r in llm_responses])
        _dump_json(run_dir / "session.json", asdict(session) if session else {})
        # 落盘后裁剪：只保留最近 _MAX_RUNS 个 run 目录，防止显式反复 persist 无界增长
        pruned = _prune_old_runs()
        if pruned:
            logger.info("probe prune: deleted %d old run dir(s), keep=%d", pruned, _MAX_RUNS)

    if run_dir is not None:
        await asyncio.to_thread(_dump_all)

    # auto_cleanup
    if params.auto_cleanup:
        try:
            deleted = await store.delete_session(sid)
            cleaned_up = deleted
            logger.info("probe auto_cleanup: session=%s deleted=%s", sid, deleted)
        except Exception as e:
            logger.warning("probe auto_cleanup failed: session=%s %s", sid, e)
            cleaned_up = False

    # 关闭 store
    try:
        store.close()
    except Exception:
        pass

    # 构造摘要
    summary = ProbeSummary(
        session_id=sid,
        mode="probe",
        status=status,
        messages_count=len(messages),
        events_count=len(events),
        tool_calls_count=len(tool_calls),
        messages_preview=[
            _message_preview(m, params.include_full)
            for m in messages
        ],
        tool_calls_preview=[
            _tool_call_preview(tc, params.include_full)
            for tc in tool_calls
        ],
        llm_calls=[
            _llm_call_preview(req, resp, params.include_full)
            for req, resp in zip(llm_requests, llm_responses, strict=False)
        ],
        run_dir=str(run_dir) if run_dir is not None else "",
        cleaned_up=cleaned_up,
    )
    logger.info(
        "probe summary: session=%s messages=%d events=%d tool_calls=%d llm_calls=%d cleaned_up=%s",
        sid, summary.messages_count, summary.events_count,
        summary.tool_calls_count, len(summary.llm_calls), summary.cleaned_up,
    )
    return summary


def _dump_json(path: Path, data: Any) -> None:
    """JSON 落盘辅助。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def _prune_old_runs(keep: int = _MAX_RUNS) -> int:
    """裁剪 RUNS_DIR，只保留最近 keep 个 run 目录（按 mtime 旧→新）。返回删除数。

    仅 persist=True 落盘后调用。只删 RUNS_DIR 下以 ``probe-`` 开头的目录，绝不触碰
    RUNS_DIR 之外；keep 至少 1，避免把刚写的当前 run 也删掉。
    """
    keep = max(1, keep)
    if not RUNS_DIR.exists():
        return 0
    runs = [d for d in RUNS_DIR.iterdir() if d.is_dir() and d.name.startswith("probe-")]
    if len(runs) <= keep:
        return 0
    # mtime 升序 = 旧→新；mtime 相同（同一秒内连续写）用名字兜底定序
    runs.sort(key=lambda d: (d.stat().st_mtime, d.name))
    deleted = 0
    for d in runs[: len(runs) - keep]:
        try:
            shutil.rmtree(d)
            deleted += 1
        except OSError as e:
            logger.warning("probe prune failed: dir=%s %s", d, e)
    return deleted

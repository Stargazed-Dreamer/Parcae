"""headless-agent-session Ticket 02 验收测试：headless_runner.py 主 agent 提交逻辑

覆盖 Ticket 02 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] 新建 headless_runner.py，实现 run_main_agent 函数签名
- [x] session_id=f"headless-{uuid4.hex[:12]}"，mode="headless"
- [x] 从 runtime 取主 agent 最后 20 条 assistant 消息
- [x] 返回 MainAgentResult(session_id, outcome, last_assistant_messages)
- [x] 单元测试：scripted runtime client 验证提交与结果处理

butler-runtime T6 起：8766 进程不再组装 deps / 不跑 SessionFacade，
run_main_agent 只做 submit → 等终态 → 取消息/账本。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fake_runtime import FakeRuntimeClient, assistant  # noqa: E402

from lib.agent_runtime.event_store import EventStore  # noqa: E402
from lib.agent_runtime.runtime_client import (  # noqa: E402
    DEFAULT_WAIT_TIMEOUT,
    RuntimeUnavailable,
)
from lib.agent_runtime.types import Message  # noqa: E402
from server.activity_tracker import headless_runner as hr  # noqa: E402
from server.activity_tracker.headless_runner import (  # noqa: E402
    DEFAULT_JUDGE_MAX_ITERATIONS,
    HEADLESS_JUDGE_PREFIX,
    HEADLESS_MAIN_PREFIX,
    HeadlessConfig,
    MainAgentResult,
    _extract_last_assistant_messages,
    _generate_judge_session_id,
    _generate_main_session_id,
    _make_runtime_client,
    _wait_timeout,
    parse_headless_config,
    run_main_agent,
)

# ============================================================================
# Fixtures
# ============================================================================


# ============================================================================
# 1. session_id 生成（Ticket 02 acceptance）
# ============================================================================


def test_generate_main_session_id_has_headless_prefix():
    """Ticket 02: 主会话 session_id 以 'headless-' 开头。"""
    sid = _generate_main_session_id()
    assert sid.startswith(HEADLESS_MAIN_PREFIX)
    # headless-{12 hex chars}
    assert len(sid) == len(HEADLESS_MAIN_PREFIX) + 12
    # 后缀是 hex
    suffix = sid[len(HEADLESS_MAIN_PREFIX):]
    int(suffix, 16)  # 不抛异常即为合法 hex


def test_generate_judge_session_id_has_headless_judge_prefix():
    """Ticket 02: judge 会话 session_id 以 'headless-judge-' 开头。"""
    sid = _generate_judge_session_id()
    assert sid.startswith(HEADLESS_JUDGE_PREFIX)
    assert len(sid) == len(HEADLESS_JUDGE_PREFIX) + 12
    suffix = sid[len(HEADLESS_JUDGE_PREFIX):]
    int(suffix, 16)


def test_generate_main_session_id_unique():
    """两次调用生成不同的 session_id。"""
    sid1 = _generate_main_session_id()
    sid2 = _generate_main_session_id()
    assert sid1 != sid2


# ============================================================================
# 2. parse_headless_config（默认值 + 覆盖）
# ============================================================================


def test_parse_headless_config_override():
    """Ticket 02: 部分配置覆盖默认值。"""
    cfg = parse_headless_config({
        "trigger_text": "查日志",
        "max_iterations": 50,
        "one_shot": True,
        "judge_model": "deepseek-chat",
    })
    assert cfg.trigger_text == "查日志"
    assert cfg.max_iterations == 50
    assert cfg.one_shot is True
    # 未覆盖的字段保持默认值
    # B2: 删 max_budget_usd 字段
    assert cfg.judge_model == "deepseek-chat"
    assert cfg.judge_max_iterations == DEFAULT_JUDGE_MAX_ITERATIONS


# ============================================================================
# 3. runtime 客户端构造（T6：中断注册表已移入 runtime）
# ============================================================================


def test_make_runtime_client_uses_configured_runtime_url():
    """T6: config.runtime_url 显式给出时，客户端直接用它。"""
    client = _make_runtime_client(
        HeadlessConfig(runtime_url="http://127.0.0.1:19999")
    )
    assert client.base_url == "http://127.0.0.1:19999"


def test_make_runtime_client_env_override(monkeypatch):
    """T6: 配置留空时按环境变量 PARCAE_RUNTIME_API 解析。"""
    monkeypatch.setenv("PARCAE_RUNTIME_API", "127.0.0.1:18888")
    client = _make_runtime_client(HeadlessConfig())
    assert client.base_url == "http://127.0.0.1:18888"


def test_wait_timeout_adds_margin_and_default():
    """T6: 等终态的超时 = 墙钟预算 + 收尾余量；0 预算用默认超时。"""
    assert _wait_timeout(60) == 180.0
    assert _wait_timeout(0) == DEFAULT_WAIT_TIMEOUT


# ============================================================================
# 4. run_main_agent 结果处理（scripted runtime client）
# ============================================================================


def test_run_main_agent_uses_runtime_outcome_status(monkeypatch):
    """T6: 主 agent 的终态来自 runtime（interrupted 原样上报）。"""
    fake = FakeRuntimeClient()
    fake.on("headless", status="interrupted", stop_reason="user_interrupted")
    monkeypatch.setattr(hr, "_make_runtime_client", lambda config: fake)

    async def run():
        return await run_main_agent(
            trigger_text="test",
            config=HeadlessConfig(),
        )

    result = asyncio.run(run())
    assert result.error == ""
    assert result.outcome is not None
    assert result.outcome.status == "interrupted"
    assert result.outcome.stop_reason == "user_interrupted"


def test_run_main_agent_submit_failure_returns_error(monkeypatch):
    """T6: runtime 提交失败 → MainAgentResult(error=...)，不抛出。"""
    fake = FakeRuntimeClient()
    fake.submit_error = RuntimeUnavailable("runtime down")
    monkeypatch.setattr(hr, "_make_runtime_client", lambda config: fake)

    async def run():
        return await run_main_agent(
            trigger_text="test",
            config=HeadlessConfig(),
        )

    result = asyncio.run(run())
    assert isinstance(result, MainAgentResult)
    assert result.outcome is None
    assert "runtime down" in result.error


# ============================================================================
# 5. _extract_last_assistant_messages（Ticket 02 acceptance）
# ============================================================================


def test_extract_last_assistant_messages_returns_assistant_only(tmp_db_path):
    """Ticket 02: 只取 assistant 消息，过滤 user/system。"""
    async def run():
        store = EventStore(db_path=tmp_db_path)
        store.init()
        try:
            await store.create_session(session_id="s1", mode="headless")
            # 写入混合消息
            await store.append_message("s1", Message(role="user", content="hi", source="user"))
            await store.append_message("s1", Message(role="assistant", content="hello 1", source="assistant"))
            await store.append_message("s1", Message(role="user", content="again", source="user"))
            await store.append_message("s1", Message(role="assistant", content="hello 2", source="assistant"))
            await store.append_message("s1", Message(role="assistant", content="hello 3", source="assistant"))

            msgs = _extract_last_assistant_messages(await store.load_messages("s1"), 20)
            assert len(msgs) == 3
            assert "hello 1" in msgs[0]
            assert "hello 2" in msgs[1]
            assert "hello 3" in msgs[2]
        finally:
            store.close()
    asyncio.run(run())


def test_extract_last_assistant_messages_respects_count(tmp_db_path):
    """Ticket 02: count 参数限制取最后 N 条。"""
    async def run():
        store = EventStore(db_path=tmp_db_path)
        store.init()
        try:
            await store.create_session(session_id="s2", mode="headless")
            for i in range(25):
                await store.append_message(
                    "s2", Message(role="assistant", content=f"msg {i}", source="assistant")
                )

            messages = await store.load_messages("s2")
            # count=20 取最后 20 条
            msgs = _extract_last_assistant_messages(messages, 20)
            assert len(msgs) == 20
            assert "msg 5" in msgs[0]  # 第 6 条（index 5）是最早的
            assert "msg 24" in msgs[-1]  # 最后一条

            # count=3 取最后 3 条
            msgs3 = _extract_last_assistant_messages(messages, 3)
            assert len(msgs3) == 3
            assert "msg 22" in msgs3[0]
            assert "msg 24" in msgs3[-1]
        finally:
            store.close()
    asyncio.run(run())


def test_extract_last_assistant_messages_empty_session(tmp_db_path):
    """Ticket 02: 空会话返回空列表。"""
    async def run():
        store = EventStore(db_path=tmp_db_path)
        store.init()
        try:
            await store.create_session(session_id="s3", mode="headless")
            msgs = _extract_last_assistant_messages(await store.load_messages("s3"), 20)
            assert msgs == []
        finally:
            store.close()
    asyncio.run(run())


def test_extract_last_assistant_messages_no_assistant_msgs(tmp_db_path):
    """Ticket 02: 只有 user 消息时返回空列表。"""
    async def run():
        store = EventStore(db_path=tmp_db_path)
        store.init()
        try:
            await store.create_session(session_id="s4", mode="headless")
            await store.append_message("s4", Message(role="user", content="hi", source="user"))
            await store.append_message("s4", Message(role="user", content="again", source="user"))
            msgs = _extract_last_assistant_messages(await store.load_messages("s4"), 20)
            assert msgs == []
        finally:
            store.close()
    asyncio.run(run())


# ============================================================================
# 6. run_main_agent 端到端（scripted runtime client）
# ============================================================================


def test_run_main_agent_with_mock_llm(tmp_db_path, monkeypatch):
    """Ticket 02: run_main_agent 提交后从 runtime 读回终态/消息/账本。

    验证：
    - session_id 以 "headless-" 开头
    - 以 mode="headless" 提交、trigger_text 原样发出
    - 主 agent 的 assistant 消息从 runtime API 读回
    - MainAgentResult 含 session_id + outcome + last_assistant_messages + evidence
    """
    fake = FakeRuntimeClient()
    fake.on(
        "headless",
        messages=[
            {"role": "user", "content": "回复'headless test ok'即可，不要调任何工具"},
            assistant("headless test ok"),
        ],
        tool_summary={"total": 0, "counts": {}, "non_success": [], "rejected_terminal_writes": 0},
        total_tokens=42,
    )
    monkeypatch.setattr(hr, "_make_runtime_client", lambda config: fake)

    config = HeadlessConfig(
        trigger_text="回复'headless test ok'即可，不要调任何工具",
        max_iterations=5,
        # B2: 删 max_budget_usd 字段
        wall_clock_budget_secs=60,
    )

    async def run():
        result = await run_main_agent(
            trigger_text=config.trigger_text,
            config=config,
        )
        return result

    result = asyncio.run(run())

    # 验证 MainAgentResult 结构
    assert isinstance(result, MainAgentResult)
    assert result.session_id.startswith(HEADLESS_MAIN_PREFIX)
    assert result.error == ""  # 无错误

    # 验证 outcome 存在
    assert result.outcome is not None
    assert result.outcome.session_id == result.session_id
    assert result.outcome.status == "completed"

    # 验证提交内容（mode / trigger_text / 资源限制）
    assert len(fake.submits) == 1
    submitted = fake.submits[0]
    assert submitted["session_id"] == result.session_id
    assert submitted["mode"] == "headless"
    assert submitted["text"] == config.trigger_text
    assert submitted["max_iterations"] == 5

    # 验证从 runtime 读回的 assistant 消息
    assert len(result.last_assistant_messages) == 1
    assert "headless test ok" in result.last_assistant_messages[-1]

    # 验证执行账本
    assert result.execution_evidence["total"] == 0


def test_run_main_agent_with_explicit_session_id(tmp_db_path, monkeypatch):
    """Ticket 02: 支持外部传入 session_id（不自动生成）。"""
    fake = FakeRuntimeClient()
    fake.on("headless", messages=[assistant("ok")])
    monkeypatch.setattr(hr, "_make_runtime_client", lambda config: fake)

    explicit_sid = "headless-explicit-test1"

    async def run():
        return await run_main_agent(
            trigger_text="test",
            config=HeadlessConfig(max_iterations=3),
            session_id=explicit_sid,
        )

    result = asyncio.run(run())
    assert result.session_id == explicit_sid
    assert result.error == ""
    assert fake.submits[0]["session_id"] == explicit_sid


def test_run_main_agent_passes_resource_limits(tmp_db_path, monkeypatch):
    """Ticket 02/11: 资源限制进入 submit 载荷（由 runtime 执行）。"""
    fake = FakeRuntimeClient()
    fake.on("headless", messages=[assistant("ok")])
    monkeypatch.setattr(hr, "_make_runtime_client", lambda config: fake)

    async def run():
        return await run_main_agent(
            trigger_text="test",
            config=HeadlessConfig(
                max_iterations=3,
                wall_clock_budget_secs=45,
                model="test-model",
            ),
        )

    result = asyncio.run(run())
    assert result.error == ""
    submitted = fake.submits[0]
    assert submitted["max_iterations"] == 3
    assert submitted["wall_clock_budget_secs"] == 45
    assert submitted["model_override"] == "test-model"


def test_run_main_agent_returns_execution_evidence(tmp_db_path, monkeypatch):
    """T13: 执行账本从 runtime 读回（judge 的结构化证据）。"""
    evidence = {
        "total": 2,
        "counts": {"completed": 1, "failed": 1},
        "non_success": [{"id": "c2", "name": "exec_cmd", "status": "failed"}],
        "rejected_terminal_writes": 0,
    }
    fake = FakeRuntimeClient()
    fake.on("headless", messages=[assistant("done")], tool_summary=evidence)
    monkeypatch.setattr(hr, "_make_runtime_client", lambda config: fake)

    async def run():
        return await run_main_agent(
            trigger_text="test",
            config=HeadlessConfig(),
        )

    result = asyncio.run(run())
    assert result.error == ""
    assert result.execution_evidence == evidence


def test_run_main_agent_runtime_unavailable_returns_error(tmp_db_path, monkeypatch):
    """Ticket 02: runtime 不可达时返回 MainAgentResult(error=...)，不抛出。"""
    fake = FakeRuntimeClient()
    fake.submit_error = RuntimeUnavailable("cannot reach butler runtime")
    monkeypatch.setattr(hr, "_make_runtime_client", lambda config: fake)

    async def run():
        return await run_main_agent(
            trigger_text="test",
            config=HeadlessConfig(max_iterations=3),
        )

    result = asyncio.run(run())
    assert isinstance(result, MainAgentResult)
    assert result.session_id.startswith(HEADLESS_MAIN_PREFIX)
    assert result.outcome is None
    assert "cannot reach butler runtime" in result.error

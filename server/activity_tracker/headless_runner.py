"""headless-agent-session: 后端自主启动 agent 会话的提交逻辑

spec: temp/sdd/headless-agent-session/spec.md
ticket: Ticket 02 (主 agent 启动) + Ticket 03 (judge agent)

butler-runtime T6 起：本模块**不再持有对话循环**。headless 会话提交给
butler runtime（独立进程），这里只做 submit → 等终态 → 取消息/账本。
后端进程挂掉不影响作业跑完；作业结果通过 runtime HTTP 读回。

- 作业在 runtime 里以 mode="headless" / "headless_judge" 运行
- 消息、执行账本（tool_call_outcome_summary）都从 runtime API 读
- 中断由 runtime 按 session_id 处理（headless_endpoints 代理转发）

资源限制默认值（spec 我替领导拍的板 + 用户反馈放宽）：
- max_iterations: 100（chat 会话 50 翻倍，正常任务能跑完）
- wall_clock_budget_secs: 1800（30 分钟，复杂任务需多轮工具调用）

B2（spec D2/D3）：删 max_budget_usd 字段，本地无法准确算 cost（缓存比例不明），
只记账 token 不控制预算。
"""

from __future__ import annotations

import logging
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any

from lib.agent_runtime.runtime_client import (
    DEFAULT_WAIT_TIMEOUT,
    RuntimeClient,
)
from lib.agent_runtime.types import (
    HEADLESS_JUDGE_SESSION_PREFIX,
    HEADLESS_SESSION_PREFIX,
)

logger = logging.getLogger("parcae.headless_runner")

# ============================================================================
# 常量与默认值（spec 我替领导拍的板）
# ============================================================================

# 主 agent 资源限制默认值（spec 我替领导拍的板 + 用户反馈放宽）
DEFAULT_MAX_ITERATIONS = 100
DEFAULT_WALL_CLOCK_BUDGET_SECS = 1800  # 30 分钟

# judge agent 资源限制默认值（更紧凑，判定任务简单）
DEFAULT_JUDGE_MAX_ITERATIONS = 10
DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS = 300  # 5 分钟

# judge 取主 agent 最后 N 条 assistant 消息作为判定材料
DEFAULT_JUDGE_MAIN_OUTPUT_MESSAGES = 20

HEADLESS_MAIN_PREFIX = HEADLESS_SESSION_PREFIX
HEADLESS_JUDGE_PREFIX = HEADLESS_JUDGE_SESSION_PREFIX


# ============================================================================
# 数据类
# ============================================================================


@dataclass
class HeadlessConfig:
    """headless session 配置（从 [loops.headless_session.*] config 段解析）。

    所有字段都有默认值，支持部分配置（缺失字段用默认值）。
    """
    trigger_text: str = ""
    skill: str = ""  # trigger_text 留空时用 skill 的 first_action
    one_shot: bool = False

    # 主 agent 资源限制（spec 默认值，宽松）
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    # B2（spec D2/D3）：删 max_budget_usd，本地无法准确算 cost
    wall_clock_budget_secs: int = DEFAULT_WALL_CLOCK_BUDGET_SECS
    model: str = ""  # 留空用默认 tier

    # judge agent 配置
    judge_model: str = ""  # 留空用默认 tier
    judge_max_iterations: int = DEFAULT_JUDGE_MAX_ITERATIONS
    # B2（spec D2/D3）：删 judge_max_budget_usd，本地无法准确算 cost
    judge_wall_clock_budget_secs: int = DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS

    # runtime 地址；留空按运行时声明文件解析（data/runtime/butler_runtime.json）
    runtime_url: str = ""


@dataclass
class MainAgentResult:
    """run_main_agent 的返回值。"""
    session_id: str
    outcome: Any  # RuntimeOutcome（用 Any 避免 import cycle）
    last_assistant_messages: list[str] = field(default_factory=list)
    # T13：主 agent 的执行账本（终态计数 + 非成功项），judge 的判定材料之一。
    # 此前 judge 只能读主 agent 说了什么——"说完成了"就算完成。
    execution_evidence: dict = field(default_factory=dict)
    error: str = ""


# ============================================================================
# JUDGE_PROMPT_TEMPLATE（spec 草拟）
# ============================================================================

JUDGE_PROMPT_TEMPLATE = """你是任务成功判定 agent。下面给你一个 headless agent 任务及其最终输出，请判定任务是否成功完成。

【任务描述】
{task}

【主 agent 的对话历史（最后 {msg_count} 条消息）】
{main_output}

【执行账本（结构化证据，来自事件存储，不是主 agent 的自述）】
{execution_evidence}

【判定规则】
- success：主 agent 完成了任务描述要求的工作，输出明确表示完成或给出了所需结果，
  **且执行账本里没有 failed / interrupted / unknown 的调用**
- failed：主 agent 明确报告失败、无法完成，或输出与任务要求不符
- uncertain：出现以下任一情况都判 uncertain，不要勉强判成功——
  主 agent 输出含"不确定""可能""大概""未确认"等模糊表述；主 agent 中途中断/未给出明确结论；
  执行账本里存在 unknown（结果未知）或 interrupted（被打断）的调用；
  账本与主 agent 的自述互相矛盾（它说做完了，但账本里有非成功项）

叙述可以撒，账本不会。以执行账本为准。

【输出格式】
严格输出以下 JSON，不要其他内容：
{{"verdict": "success" | "failed" | "uncertain", "reason": "简短说明（一句话）"}}
"""


def format_execution_evidence(evidence: dict | None) -> str:
    """执行账本 dict → judge prompt 里那段可读文本（T13）。"""
    if not evidence:
        return "(无执行账本数据：拿不到事件存储，请按证据不足处理，倾向 uncertain)"
    counts = evidence.get("counts") or {}
    total = evidence.get("total", 0)
    non_success = evidence.get("non_success") or []
    rejected = evidence.get("rejected_terminal_writes", 0)
    lines = [
        f"工具调用总数：{total}",
        "终态分布：" + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "无",
    ]
    if non_success:
        shown = non_success[:20]
        lines.append(
            "非成功项（failed/interrupted/unknown/pending/running）："
            + "; ".join(f"{i.get('name')}→{i.get('status')}" for i in shown)
        )
        if len(non_success) > len(shown):
            lines.append(f"（另有 {len(non_success) - len(shown)} 项未列出）")
    else:
        lines.append("非成功项：无")
    if rejected:
        lines.append(
            f"被拒绝的终态回写次数：{rejected}（有旧写入试图覆盖已落定的事实，需人工看一眼）"
        )
    return "\n".join(lines)


# ============================================================================
# judge agent 数据类
# ============================================================================


@dataclass
class JudgeResult:
    """run_judge_agent 的返回值。"""
    verdict: str  # "success" / "failed" / "uncertain"
    reason: str = ""
    session_id: str = ""
    raw_output: str = ""  # judge 最后一条 assistant 消息原文（调试用）
    error: str = ""  # judge 自身失败时的错误信息


# verdict 合法值集合
VALID_VERDICTS = {"success", "failed", "uncertain"}


# ============================================================================
# runtime 客户端
# ============================================================================


def _make_runtime_client(config: HeadlessConfig) -> RuntimeClient:
    """构造 runtime 客户端；测试可 monkeypatch 本函数注入替身。"""
    return RuntimeClient(base_url=config.runtime_url or None)


def _wait_timeout(wall_clock_budget_secs: int) -> float:
    """等终态的超时：runtime 自己会在墙上时间到点时收尾，这里只多给收尾余量。"""
    if wall_clock_budget_secs and wall_clock_budget_secs > 0:
        return float(wall_clock_budget_secs) + 120.0
    return DEFAULT_WAIT_TIMEOUT


def _generate_main_session_id() -> str:
    """生成主会话 session_id：headless-{uuid4.hex[:12]}。"""
    return f"{HEADLESS_MAIN_PREFIX}{_uuid.uuid4().hex[:12]}"


def _generate_judge_session_id() -> str:
    """生成 judge 会话 session_id：headless-judge-{uuid4.hex[:12]}。"""
    return f"{HEADLESS_JUDGE_PREFIX}{_uuid.uuid4().hex[:12]}"


# ============================================================================
# 主 agent 启动逻辑
# ============================================================================


async def run_main_agent(
    trigger_text: str,
    config: HeadlessConfig,
    *,
    session_id: str | None = None,
    client: RuntimeClient | None = None,
) -> MainAgentResult:
    """把主 agent 会话（mode="headless"）提交给 runtime 并等它跑完。

    Args:
        trigger_text: 触发任务文本（如 "查 dmca-backup 最近一次 run 的日志"）
        config: HeadlessConfig（资源限制 + runtime_url）
        session_id: 可选 session_id（留空自动生成 headless-{uuid}）
        client: 可选 runtime 客户端（测试注入；留空按 config.runtime_url 构造）

    Returns:
        MainAgentResult：含 session_id + outcome + last_assistant_messages
        失败时 outcome 可能是 None，error 字段含错误信息
    """
    sid = session_id or _generate_main_session_id()
    client = client or _make_runtime_client(config)

    try:
        logger.info(
            "headless main agent submit: session=%s trigger_len=%d max_iter=%d wall=%ds",
            sid, len(trigger_text), config.max_iterations, config.wall_clock_budget_secs,
        )
        await client.submit(
            sid,
            trigger_text,
            mode="headless",
            model_override=config.model,
            max_iterations=config.max_iterations,
            wall_clock_budget_secs=config.wall_clock_budget_secs,
        )
        outcome = await client.outcome(sid, timeout=_wait_timeout(config.wall_clock_budget_secs))

        messages = await client.messages(sid)
        last_msgs = _extract_last_assistant_messages(
            messages, DEFAULT_JUDGE_MAIN_OUTPUT_MESSAGES
        )

        # T13：同时取执行账本。judge 不能只听主 agent 自述，必须看到
        # 这一轮工具调用里有多少 completed / failed / interrupted / unknown。
        evidence: dict = {}
        try:
            evidence = await client.tool_call_summary(sid)
        except Exception as e:
            logger.warning("读取执行账本失败 (session=%s): %s", sid, e)

        return MainAgentResult(
            session_id=sid,
            outcome=outcome,
            last_assistant_messages=last_msgs,
            execution_evidence=evidence,
        )

    except Exception as e:
        logger.exception("headless main agent failed: session=%s", sid)
        return MainAgentResult(
            session_id=sid,
            outcome=None,
            error=f"{type(e).__name__}: {e}",
        )


def _extract_last_assistant_messages(
    messages: list, count: int
) -> list[str]:
    """取最后 N 条 assistant 消息的 content 文本。

    Args:
        messages: runtime `/sessions/{sid}/messages` 返回的消息 dict 列表
            （也接受带 role/content 属性的对象，便于旧调用点/测试复用）
        count: 取最后 N 条（不足则全取；<=0 取全部）

    Returns:
        list[str]：每条 assistant 消息的 content 文本（按时间顺序，最老的在前）
    """
    def _field(m: Any, name: str, default: Any = "") -> Any:
        if isinstance(m, dict):
            return m.get(name, default)
        return getattr(m, name, default)

    assistant_msgs = [m for m in messages if _field(m, "role") == "assistant"]
    last_n = assistant_msgs[-count:] if count > 0 else assistant_msgs

    result: list[str] = []
    for m in last_n:
        content = _field(m, "content")
        if isinstance(content, str):
            result.append(content)
        elif isinstance(content, list):
            # OpenAI content blocks: [{"type": "text", "text": "..."}, ...]
            text_parts = [
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            result.append("\n".join(text_parts))
        else:
            result.append(str(content))
    return result


# ============================================================================
# 工具函数（供 Ticket 04 HeadlessSessionAction 用）
# ============================================================================


def parse_headless_config(config_dict: dict) -> HeadlessConfig:
    """从 dict 解析 HeadlessConfig（缺失字段用默认值）。

    Args:
        config_dict: loop task config dict（如 {
            "trigger_text": "查日志",
            "max_iterations": 50,
            "one_shot": True,
        }）

    Returns:
        HeadlessConfig
    """
    return HeadlessConfig(
        trigger_text=config_dict.get("trigger_text", ""),
        skill=config_dict.get("skill", ""),
        one_shot=config_dict.get("one_shot", False),
        max_iterations=config_dict.get("max_iterations", DEFAULT_MAX_ITERATIONS),
        # B2（spec D2/D3）：删 max_budget_usd 解析（字段已删，配置文件残留被 .get 忽略）
        wall_clock_budget_secs=config_dict.get(
            "wall_clock_budget_secs", DEFAULT_WALL_CLOCK_BUDGET_SECS
        ),
        model=config_dict.get("model", ""),
        judge_model=config_dict.get("judge_model", ""),
        judge_max_iterations=config_dict.get(
            "judge_max_iterations", DEFAULT_JUDGE_MAX_ITERATIONS
        ),
        # B2（spec D2/D3）：删 judge_max_budget_usd 解析（字段已删）
        judge_wall_clock_budget_secs=config_dict.get(
            "judge_wall_clock_budget_secs", DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS
        ),
        runtime_url=config_dict.get("runtime_url", ""),
    )


# ============================================================================
# judge agent 启动逻辑
# ============================================================================


def _parse_judge_verdict(raw_output: str) -> tuple[str, str]:
    """解析 judge agent 输出的 JSON verdict。

    Args:
        raw_output: judge 最后一条 assistant 消息的 content 文本

    Returns:
        (verdict, reason) tuple。解析失败时 verdict="uncertain", reason 含解析错误说明。
    """
    import json
    import re

    if not raw_output or not raw_output.strip():
        return ("uncertain", "judge output is empty")

    text = raw_output.strip()

    # 尝试直接解析整段为 JSON
    try:
        data = json.loads(text)
        verdict = str(data.get("verdict", "")).strip().lower()
        reason = str(data.get("reason", "")).strip()
        if verdict in VALID_VERDICTS:
            return (verdict, reason or f"verdict={verdict}")
        return ("uncertain", f"invalid verdict value: {verdict!r}")
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # 兜底：尝试从文本中提取 JSON 块（如 ```json ... ``` 或 {...}）
    json_match = re.search(r"\{[^{}]*\"verdict\"[^{}]*\}", text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            verdict = str(data.get("verdict", "")).strip().lower()
            reason = str(data.get("reason", "")).strip()
            if verdict in VALID_VERDICTS:
                return (verdict, reason or f"verdict={verdict}")
            return ("uncertain", f"invalid verdict value: {verdict!r}")
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    return ("uncertain", f"judge output not valid JSON: {text[:100]!r}")


async def run_judge_agent(
    main_output: list[str],
    trigger_text: str,
    config: HeadlessConfig,
    *,
    session_id: str | None = None,
    execution_evidence: dict | None = None,
    client: RuntimeClient | None = None,
) -> JudgeResult:
    """把 judge 会话（mode="headless_judge"）提交给 runtime 并解析判定结果。

    流程（spec Solution 节阶段 2）：
    1. judge prompt = JUDGE_PROMPT_TEMPLATE.format(task=trigger_text, main_output=...)
    2. 提交 judge session，等终态
    3. 解析 judge 最后一条 assistant 消息 JSON
    4. 降级规则：
       - JSON 解析失败 → verdict="uncertain"
       - judge session 自身失败（outcome.status != completed）→ verdict="uncertain"
       - judge 超过 max_iterations → verdict="uncertain"

    Args:
        main_output: 主 agent 最后 N 条 assistant 消息文本列表
        trigger_text: 主 agent 的触发任务文本（判定时对照任务描述）
        config: HeadlessConfig（judge_model / judge_max_iterations 等字段用）
        session_id: 可选 judge session_id（留空自动生成 headless-judge-{uuid}）
        execution_evidence: 主 agent 的执行账本（EventStore.tool_call_outcome_summary
            的形状）。T13：判定材料不再只有对话文本；缺省时 prompt 里如实标注
            "拿不到账本"，规则会倾向 uncertain 而不是勉强判成功。
        client: 可选 runtime 客户端（测试注入；留空按 config.runtime_url 构造）

    Returns:
        JudgeResult：含 verdict + reason + session_id + raw_output
    """
    sid = session_id or _generate_judge_session_id()
    client = client or _make_runtime_client(config)

    try:
        # 组装 judge prompt
        main_output_text = "\n---\n".join(main_output) if main_output else "(无主 agent 输出)"
        judge_prompt = JUDGE_PROMPT_TEMPLATE.format(
            task=trigger_text,
            msg_count=len(main_output),
            main_output=main_output_text,
            execution_evidence=format_execution_evidence(execution_evidence),
        )

        logger.info(
            "headless judge agent submit: session=%s judge_model=%s max_iter=%d",
            sid, config.judge_model or "(default)", config.judge_max_iterations,
        )
        await client.submit(
            sid,
            judge_prompt,
            mode="headless_judge",
            model_override=config.judge_model,
            max_iterations=config.judge_max_iterations,
            wall_clock_budget_secs=config.judge_wall_clock_budget_secs,
        )
        outcome = await client.outcome(
            sid, timeout=_wait_timeout(config.judge_wall_clock_budget_secs)
        )

        # 取 judge 最后一条 assistant 消息
        messages = await client.messages(sid)
        judge_msgs = _extract_last_assistant_messages(messages, 1)
        raw_output = judge_msgs[-1] if judge_msgs else ""

        # 降级规则 1：judge session 自身失败
        if outcome is None:
            return JudgeResult(
                verdict="uncertain",
                reason="judge session failed (outcome is None)",
                session_id=sid,
                raw_output=raw_output,
                error="outcome is None",
            )

        # 降级规则 2：judge 超过 max_iterations（stop_reason 含 max_iterations）
        stop_reason = getattr(outcome, "stop_reason", "") or ""
        if "max_iterations" in stop_reason:
            return JudgeResult(
                verdict="uncertain",
                reason="judge exceeded max_iterations",
                session_id=sid,
                raw_output=raw_output,
            )

        # 降级规则 3：judge session status 不是 completed
        outcome_status = getattr(outcome, "status", "") or ""
        if outcome_status != "completed":
            return JudgeResult(
                verdict="uncertain",
                reason=f"judge session not completed (status={outcome_status})",
                session_id=sid,
                raw_output=raw_output,
            )

        # 降级规则 4：raw_output 为空
        if not raw_output.strip():
            return JudgeResult(
                verdict="uncertain",
                reason="judge output is empty",
                session_id=sid,
                raw_output=raw_output,
            )

        # 解析 JSON verdict
        verdict, reason = _parse_judge_verdict(raw_output)
        return JudgeResult(
            verdict=verdict,
            reason=reason,
            session_id=sid,
            raw_output=raw_output,
        )

    except Exception as e:
        logger.exception("headless judge agent failed: session=%s", sid)
        return JudgeResult(
            verdict="uncertain",
            reason=f"judge session failed: {type(e).__name__}: {e}",
            session_id=sid,
            error=f"{type(e).__name__}: {e}",
        )

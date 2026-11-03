"""8766 侧探针入口：把探针提交给 butler runtime（butler-runtime T6）。

引擎在 `lib.agent_runtime.probe_runner`（runtime 进程里用 MockLLM 跑会话），
本模块只做参数序列化 + HTTP 提交；8766 进程里不再起对话循环。
"""

from __future__ import annotations

from dataclasses import asdict, fields

from lib.agent_runtime.probe_runner import ProbeParams, ProbeSummary
from lib.agent_runtime.runtime_client import RuntimeClient


async def run_probe(
    params: ProbeParams, *, client: RuntimeClient | None = None
) -> ProbeSummary:
    """把探针参数提交给 runtime，返回其结果摘要。

    Args:
        params: 探针参数（形状见 lib.agent_runtime.probe_runner.ProbeParams）
        client: 可选 runtime 客户端（测试注入；留空按运行时声明构造）

    Raises:
        RuntimeUnavailable: runtime 未运行
        RuntimeCallError: runtime 返回结构化错误（如未知 pattern → 400 范畴）
    """
    body = await (client or RuntimeClient()).probe(asdict(params))
    return ProbeSummary(**{f.name: body[f.name] for f in fields(ProbeSummary)})

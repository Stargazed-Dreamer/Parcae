"""runtime 的真实依赖装配：LLM 网关 + 工具目录 + 内置工具 + 压缩器。

测试不走这里（`RuntimeManager` 接受注入的 deps_factory）；本模块只在 runtime
进程里用，依赖 8766 提供 `/openapi.json` 与 LLM 网关。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from lib.agent_runtime.ask_user import RuntimeAskUserProvider
from lib.agent_runtime.builtin_tool_executor import BuiltinToolExecutor
from lib.agent_runtime.compactor import Compactor
from lib.agent_runtime.event_store import EventStore
from lib.agent_runtime.http_client_tool_executor import HttpClientToolExecutor
from lib.agent_runtime.l0_artifact_store import L0ArtifactStore
from lib.agent_runtime.llm_pool_gateway import LLMPoolGateway
from lib.agent_runtime.runtime_manager import RuntimeSettings
from lib.agent_runtime.tool_registry import ToolRegistry
from lib.agent_runtime.types import RunnerDeps

logger = logging.getLogger("parcae.butler_runtime.deps")


async def build_runtime_deps(
    store: EventStore,
    settings: RuntimeSettings,
    interrupt_event: asyncio.Event,
    provider: RuntimeAskUserProvider,
) -> RunnerDeps:
    gateway = LLMPoolGateway(base_url=settings.base_url)
    registry = ToolRegistry(base_url=settings.base_url)
    try:
        # refresh 是同步 requests；放线程里，别让一次目录拉取冻住所有会话与 WS
        await asyncio.to_thread(registry.refresh)
    except Exception as exc:
        logger.warning("ToolRegistry refresh failed (will run without tools): %s", exc)

    executor = HttpClientToolExecutor(
        registry=registry, base_url=settings.base_url, l0_store=L0ArtifactStore(),
    )
    builtin = BuiltinToolExecutor(
        base_url=settings.base_url,
        ask_callback=provider.ask,
    )
    try:
        registry.register_builtin_tools(builtin)
    except Exception as exc:
        logger.warning("register_builtin_tools failed (fail-open): %s", exc)

    return RunnerDeps(
        llm_gateway=gateway,
        event_store=store,
        tool_executor=executor,
        tool_registry=registry,
        builtin_executor=builtin,
        interrupt_event=interrupt_event,
        compactor=Compactor(max_context_tokens=0),
    )


def settings_from_config(base_url: str) -> RuntimeSettings:
    """从 config.toml `[runner]` 取运行参数；缺失一律用既有默认值。"""
    from lib.config_reader import load_config

    try:
        cfg: dict[str, Any] = load_config() or {}
    except Exception:
        cfg = {}
    runner = cfg.get("runner", {}) if isinstance(cfg, dict) else {}
    if not isinstance(runner, dict):
        runner = {}

    def _int(key: str, default: int) -> int:
        try:
            return int(runner.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    def _float(key: str, default: float) -> float:
        try:
            return float(runner.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    return RuntimeSettings(
        base_url=base_url,
        model=str(runner.get("model") or ""),
        max_iterations=_int("max_iterations", 50),
        wall_clock_budget_secs=_int("wall_clock_budget_secs", 0),
        ask_user_timeout_secs=max(0.0, _float("ask_user_timeout_secs", 0.0)),
        stream_idle_timeout_secs=_int("stream_idle_timeout_secs", 90),
        long_tool_first_check_secs=_int("long_tool_first_check_secs", 180),
    )

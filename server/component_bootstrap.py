"""可选组件注册表刷新（lifespan 入口 + 手动入口）。

背景
----
``workspace/<component>/loop_actions.py`` 等可选扩展点在**模块 import 期**就被
``server/activity_tracker/loop_manager.py`` / ``server/agent_guide.py`` 加载
（历史约定，且被 ``tests/componentization/test_component_e2e.py`` 等测试依赖:
reload 模块 ⇒ 注册表重建）。加载逻辑本身已由 ``lib/component_loader`` 加固成
「失败只降级、不上抛」，所以 import 期不再会因外部代码崩掉后端。

本模块在 lifespan startup 里再做一次**幂等刷新**，作用是：

1. 重置 manifest 缓存 —— 本次启动新增 / 删除的组件目录能被看见；
2. 让注册表与磁盘现状一致（import 期之后才出现的组件也可补进来）；
3. 把降级清单打到启动日志，并写入 ``/health`` 的 ``component_load`` 段。

⚠ 这里**不 reload 模块**：注册表是模块级 dict，``update`` 即可；reload 会替换
dict 对象本身，破坏其它模块已持有的引用。测试走的是 reload 路径，互不干扰。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("parcae.component_bootstrap")


def refresh_component_registries(*, rescan: bool = True) -> dict:
    """幂等刷新 agent_guide / loop_manager 的可选组件注册表。

    不抛异常：每一步单独 try，失败只记日志并继续，保证 lifespan 不被外部组件拖垮。
    返回 {guide_entries, loop_builders, failed}，failed>0 表示有组件被隔离。
    """
    summary = {"guide_entries": 0, "loop_builders": 0, "failed": 0}

    if rescan:
        try:
            from server.component_manifest import reset_cache

            reset_cache()
        except Exception as exc:  # 缓存重置失败只是「看不见新目录」，不阻断
            logger.warning("manifest 缓存重置失败（沿用 import 期扫描结果）: %s", exc)

    try:
        from server import agent_guide

        entries = agent_guide._load_optional_guide_entries()
        agent_guide.GUIDE_REGISTRY.update(entries)
        summary["guide_entries"] = len(entries)
    except Exception as exc:
        logger.error("agent_guide 组件注册表刷新失败（沿用 import 期结果）: %s", exc)

    try:
        from server.activity_tracker import loop_manager

        defs = loop_manager._load_optional_loop_defs()
        loop_manager.TASK_BUILDERS.update(defs)
        summary["loop_builders"] = len(defs)
    except Exception as exc:
        logger.error("loop_manager 组件注册表刷新失败（沿用 import 期结果）: %s", exc)

    try:
        from lib.component_loader import log_failures

        summary["failed"] = log_failures("组件加载降级")
    except Exception as exc:
        logger.warning("组件降级清单输出失败: %s", exc)

    if not summary["failed"]:
        logger.info(
            "可选组件加载正常: guide 条目 %d 条 / loop 任务段 %d 个",
            summary["guide_entries"],
            summary["loop_builders"],
        )
    return summary

"""核心路由：/config、/shutdown、/mcp/stats"""

import logging
import time

from fastapi import FastAPI, HTTPException

from lib.schema import BaseSchema
from server.config import get_config_masked, update_config
from server.core.middleware import get_mcp_stats as _get_mcp_stats
from server.core.middleware import reset_mcp_stats as _reset_mcp_stats
from server.screen import on_shutdown as screen_on_shutdown
from server.system import on_shutdown as system_on_shutdown
from server.vl.vision import on_shutdown as vision_on_shutdown

logger = logging.getLogger("parcae.config_routes")


class ConfigUpdateRequest(BaseSchema):
    """配置更新请求模型"""
    path: str  # 点分隔路径，如 "llm.providers.mimo.api_key"
    value: str | int | float | bool  # 要更新的配置值


class ConfigResponse(BaseSchema):
    config: dict  # 脱敏后的配置


class McpStatsResponse(BaseSchema):
    """MCP 统计响应"""
    total_calls: int  # 总调用次数
    unique_tools: int  # 独立工具数量
    tools: list[dict]  # 工具详细信息列表


def register_config_routes(app: FastAPI):
    """注册 /config、/shutdown、/mcp/stats 等核心路由"""

    @app.get("/config", response_model=ConfigResponse)
    async def get_config():
        """获取当前配置（敏感字段已脱敏）"""
        return ConfigResponse(config=get_config_masked())

    @app.post("/config", response_model=ConfigResponse)
    async def update_config_api(req: ConfigUpdateRequest):
        """更新配置中的指定字段

        path: 点分隔路径，如 "llm.providers.mimo.api_key"
        value: 新值
        """
        try:
            masked = update_config(req.path, req.value)
            return ConfigResponse(config=masked)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"配置更新失败: {e}") from None

    @app.post("/shutdown", operation_id="shutdown_server")
    async def shutdown_server():
        """关闭服务器 - 返回OK后开始退出"""
        import threading
        logger.info("收到关闭请求，服务器即将退出...")

        def _do_shutdown():
            """执行清理后退出。os._exit 不触发 FastAPI shutdown 事件，需手动清理子进程。"""
            # 与 lifespan 清理链同款兜底：这条链任一步阻塞（典型是 overlay 子进程
            # terminate / 终端子进程回收）就永远走不到 os._exit(0)，进程会滞留。
            # 看门狗给进程一个硬上限；mark_shutdown_step 让日志能指到卡住的步骤。
            from server.core.lifecycle import mark_shutdown_step, start_shutdown_watchdog

            start_shutdown_watchdog(source="POST /shutdown")
            time.sleep(0.5)
            try:
                mark_shutdown_step("[POST /shutdown] 关闭 Screen 模块（含 overlay GUI 子进程）")
                screen_on_shutdown()
                mark_shutdown_step("[POST /shutdown] 关闭 System 模块")
                system_on_shutdown()
                mark_shutdown_step("[POST /shutdown] 关闭视觉(VL)模块")
                vision_on_shutdown()
                mark_shutdown_step("[POST /shutdown] 落盘 LLM 池统计")
                try:
                    from server.llm_pool import get_pool
                    get_pool().flush_stats()
                except Exception:
                    pass
                mark_shutdown_step("[POST /shutdown] 关闭记忆系统")
                from server.memory.manager import reset_memory_manager
                reset_memory_manager()
                mark_shutdown_step("[POST /shutdown] 关闭 overlay 客户端")
                from server.overlay_client import overlay_client
                overlay_client.shutdown()
                mark_shutdown_step("[POST /shutdown] 回收终端子进程")
                from server.exec import _terminals
                for _tid, session in list(_terminals.items()):
                    proc = session.get("_proc")
                    if proc and proc.returncode is None:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"清理过程出错: {e}")
            # 运行时端口声明收尾：本路径走 os._exit，**不会触发 lifespan shutdown**，
            # 所以 lifecycle.py 里的 stopped/pid 清理不会执行，必须在这里自己收。
            # 否则声明会一直停在 ready，客户端以为后端还活着、死连一个已释放的端口。
            try:
                from lib.runtime_endpoint import (
                    STATUS_STOPPED,
                    clear_pid_file,
                    mark_declaration_status,
                )

                mark_declaration_status(STATUS_STOPPED, reason="通过 POST /shutdown 关闭")
                clear_pid_file()
            except Exception as e:
                logger.warning(f"关闭时更新端口声明失败: {e}")
            import os
            os._exit(0)

        threading.Thread(target=_do_shutdown, daemon=True).start()
        return {"status": "ok", "message": "服务器正在关闭..."}

    @app.get("/mcp/stats", response_model=McpStatsResponse, operation_id="mcp_stats")
    async def mcp_stats():
        """查询 MCP 工具调用统计

        返回每个工具的调用次数、平均耗时、错误率等。
        用于项目整理时识别低频/未使用的工具，以及检查上游修复进度。
        """
        return _get_mcp_stats()

    @app.post("/mcp/stats/reset", operation_id="mcp_stats_reset")
    async def mcp_stats_reset():
        """重置 MCP 工具调用统计

        清空所有统计数据，从零开始重新记录。
        """
        _reset_mcp_stats()
        return {"status": "ok", "message": "统计数据已重置"}

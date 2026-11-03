"""应用生命周期：启动初始化 + 关闭清理"""

import logging
import os
import threading
import time

from server.config import get_server_config, validate_config
from server.screen import on_shutdown as screen_on_shutdown
from server.screen import on_startup as screen_on_startup
from server.system import on_shutdown as system_on_shutdown
from server.vl.vision import on_shutdown as vision_on_shutdown

logger = logging.getLogger("parcae.lifecycle")

_start_time = None


# ========== 关停兜底：分步打点 + 总超时 ==========

_watchdog_lock = threading.Lock()
_watchdog_started = False


def shutdown_cleanup_deadline() -> float:
    """清理链总兜底秒数（配置项 server.shutdown_cleanup_deadline_secs，下限 5）"""
    return float(get_server_config()["shutdown_cleanup_deadline_secs"])


def start_shutdown_watchdog(source: str, deadline_s: float | None = None) -> None:
    """启动关停总兜底看门狗（幂等，daemon 线程，超时后强制 os._exit(0)）。

    为什么需要它：清理链的每一步（屏幕/系统/视觉/记忆/Loop/浏览器/httpx 关闭，以及
    overlay GUI 子进程 terminate）本身都没有超时。任一步阻塞，后面的步骤与
    ``os._exit(0)`` 都不会执行 ⇒ 进程永久滞留，且日志停在阻塞前的那一行。

    为什么不做「每步线程隔离 + wait_for」：``screen_on_shutdown()`` 会 terminate
    overlay 子进程、SessionManager 会 join worker，把它们挪到别的线程执行会改变既有
    语义（曾踩过 multiprocessing.Queue put 后 terminate 死锁）。改为「分步打点 +
    总兜底」：``mark_shutdown_step()`` 让日志能指出卡在哪一步，看门狗保证进程有硬上限。

    Args:
        source: 触发来源，仅用于日志（如 "lifespan" / "POST /shutdown"）。
        deadline_s: 覆盖配置里的兜底秒数（None = 读配置）。
    """
    global _watchdog_started
    with _watchdog_lock:
        if _watchdog_started:
            return
        _watchdog_started = True

    deadline = shutdown_cleanup_deadline() if deadline_s is None else float(deadline_s)

    def _watch() -> None:
        time.sleep(deadline)
        logger.error(
            f"关闭清理超过 {deadline:.0f}s 未完成（来源={source}），强制退出。"
            "请查上面最后一条『关闭步骤: xxx』定位阻塞步骤"
        )
        os._exit(0)

    threading.Thread(target=_watch, name="shutdown-watchdog", daemon=True).start()
    logger.info(f"关闭看门狗已启动：最多 {deadline:.0f}s 后强制退出（来源={source}）")


def mark_shutdown_step(step: str) -> None:
    """打点清理链当前步骤——若某步阻塞，这条就是本次关停日志里的最后一行"""
    logger.info(f"关闭步骤: {step}")


def get_start_time() -> float | None:
    """启动时间戳（供 /health 计算 uptime，不暴露 raw 私有变量）"""
    return _start_time


def _ensure_pool_initialized() -> bool:
    """确保 LLM 并发池已初始化（供 startup 和 /llm/pool/call 自动调用）

    从 data/llm/keys.json（v5 unified key 库）加载 keys 并初始化全局池。
    如果池已初始化则直接返回 True。
    """
    from server.llm_pool import (
        _load_default_policy,
        _resolve_default_stats_file,
        init_pool,
        is_initialized,
        load_provider_keys,
    )
    if is_initialized():
        return True
    keys = load_provider_keys()
    if not keys:
        logger.warning(
            "LLM 池初始化失败: 未加载到任何 key。"
            "请检查 data/llm/keys.json 是否存在且含 scope=llm 的记录。"
        )
        return False
    # v14：加载全局默认 pool 策略（从 config.toml [llm.pool] 段）
    default_policy = _load_default_policy()
    # 后端使用统一的 stats 文件（不再按脚本分文件，因为所有调用都经过后端）
    # _resolve_default_stats_file 已处理相对路径相对于项目根目录的解析
    init_pool(
        keys,
        stats_file=_resolve_default_stats_file(),
        default_policy=default_policy,
    )
    logger.info(
        f"LLM 并发池已自动初始化: {len(keys)} 个 key, "
        f"policy={default_policy.name} retry={default_policy.retry_count} "
        f"cooldown={default_policy.rate_limit_cooldown_seconds}s"
    )
    return True


def register_lifecycle(app):
    """注册启动和关闭事件处理器"""

    @app.on_event("startup")
    async def _on_startup():
        """应用启动：初始化屏幕、LLM 池、待办迁移、Loop 调度。"""
        global _start_time
        _start_time = time.perf_counter()

        config_warnings = validate_config()
        if config_warnings:
            for w in config_warnings:
                logger.warning(f"配置验证: {w}")
        else:
            logger.info("配置验证通过")

        # 可选组件注册表刷新：workspace/* 的外部代码已在 import 期由
        # lib.component_loader 安全加载（语法预检 + 子进程试 import + 异常兜底）。
        # 这里重置 manifest 缓存、把注册表补齐到磁盘现状，并把降级清单打到启动日志。
        # 失败只降级，绝不阻断启动 —— 一个坏组件不该决定后端的生死。
        try:
            from server.component_bootstrap import refresh_component_registries

            refresh_component_registries()
        except Exception as e:
            logger.warning(f"组件注册表刷新失败（沿用 import 期结果）: {e}")

        screen_on_startup()

        # T18：显式初始化 SessionManager 单例（首次调用会从 config 读取 TimePolicy + 启动 worker）
        # 不依赖 overlay_client 模块级 import 时的隐式初始化，确保 SessionManager 在任何模块访问前已就绪
        try:
            from server.screen.session import get_session_manager
            get_session_manager()
        except Exception as e:
            logger.warning(f"SessionManager 初始化失败: {e}")

        # 提高 anyio 默认线程池上限（默认 40），以支持大量并发的 LLM 池代理调用
        # 多个脚本同时通过 /llm/pool/call 发起请求时，每个请求占用一个线程
        try:
            import anyio.to_thread
            limiter = anyio.to_thread.current_default_thread_limiter()
            old = limiter.total_tokens
            limiter.total_tokens = 200
            logger.info(f"anyio 线程池上限: {old} → {limiter.total_tokens}")
        except Exception as e:
            logger.warning(f"调整 anyio 线程池上限失败: {e}")

        # 初始化 LLM 并发池（自动从 data/llm/keys/ 加载）
        try:
            _ensure_pool_initialized()
        except Exception as e:
            logger.warning(f"LLM 并发池自动初始化失败（可手动调用 /llm/pool/init）: {e}")

        # keys.json 热重载守护：改完 keys.json（如给 key 补 allowed_uses）不再需要
        # 手动 POST /llm/pool/init；顺带做一次 use_case 覆盖自检，把「无 key 授权
        # inbound_gateway」这类配置错在启动/重载时直接告警出来。
        try:
            import asyncio as _asyncio

            from server.core.keys_watch import (
                warn_use_case_coverage,
                watch_keys_json_loop,
            )
            warn_use_case_coverage()
            # 保存 task 引用防 GC，并供 shutdown 取消
            app.state.keys_watch_task = _asyncio.create_task(watch_keys_json_loop())
        except Exception as e:
            logger.warning(f"keys.json 热重载守护启动失败（改 keys.json 需手动 /llm/pool/init）: {e}")

        # T5-1：注册模型存活管理器驱动并启动监控（design §11 步骤 5）
        # 先注册后监控（design §13）：全部驱动注册完成才启动监控循环，
        # 避免逐出逻辑遇到未知模型。start() 内部会先采首样本堵失明窗口。
        # 采样只有一个 owner：常驻资源中枢（NVML/psutil，不创建子进程）。
        # 中枢先于管理器启动——活动采集的高负载门也读它，不依赖 MLM 是否启用。
        try:
            from server.resource_probe import get_resource_hub

            await get_resource_hub().ensure_started()
        except Exception as e:
            logger.warning(f"资源采样中枢启动失败（GPU/RAM 采样降级为按需直采）: {e}", exc_info=True)
        try:
            from server.model_manager import get_model_manager
            from server.model_manager.drivers import (
                GuideEmbeddingDriver,
                MemoryEmbeddingDriver,
                MindForgeSearcherDriver,
                OcrDriver,
            )
            manager = get_model_manager()
            manager.registry.register(OcrDriver())
            manager.registry.register(MemoryEmbeddingDriver())
            manager.registry.register(GuideEmbeddingDriver())
            manager.registry.register(MindForgeSearcherDriver())
            await manager.start()
        except Exception as e:
            logger.warning(f"ModelLifecycleManager 启动失败（管理器降级为透传）: {e}", exc_info=True)

        # 迁移待办数据（从 memory task_reminders/wip_index → todos 表，幂等）
        try:
            from server.memory.manager import get_memory_manager
            from server.todos.migration import backfill_extra_data, migrate_from_memory
            mgr = get_memory_manager()
            result = migrate_from_memory(mgr)
            if not result.get("skipped"):
                logger.info(f"待办迁移完成: {result}")
            # 回填 wip_tasks.extra_data 并移动旧 .json 到 temp/wip_migrated_backup/
            backfill_result = backfill_extra_data(mgr)
            if not backfill_result.get("skipped"):
                logger.info(f"extra_data 回填完成: {backfill_result}")
        except Exception as e:
            logger.error(f"待办迁移失败: {e}", exc_info=True)

        # 启动 Loop 自动触发系统（asyncio 后台调度协程）
        try:
            import asyncio as _asyncio

            from server.activity_tracker.loop_actions import register_loop_actions
            from server.activity_tracker.loop_manager import get_manager as _get_loop_manager
            mgr = _get_loop_manager()
            register_loop_actions(mgr)
            # 保存 task 引用防止 GC 回收（Python 文档要求）
            app.state.loop_start_task = _asyncio.create_task(mgr.start())
        except Exception as e:
            logger.warning(f"Loop 系统启动失败: {e}")

        # P1-A：可选预热 OCR（后台任务，不阻塞 startup；首次调用可省 ~10s 加载）
        try:
            from server.config import get_ocr_config as _get_ocr_cfg
            if _get_ocr_cfg().get("preload_on_startup", False):
                import asyncio as _asyncio

                async def _preload_ocr_bg():
                    try:
                        from server.ocr import models as ocr_models
                        logger.info("OCR preload_on_startup=true，后台预热 PaddleOCR...")
                        await _asyncio.to_thread(ocr_models.get_ocr)
                        logger.info(f"OCR 预热完成: load_elapsed_ms={ocr_models._load_elapsed_ms}")
                    except Exception as e:
                        logger.warning(f"OCR 后台预热失败: {e}")

                app.state.ocr_preload_task = _asyncio.create_task(_preload_ocr_bg())
        except Exception as e:
            logger.warning(f"OCR 预热配置读取失败: {e}")

        # T00: 启动 terminal TTL 清理后台任务（spec D11 第三层保障）
        try:
            from server.exec import start_terminal_ttl_cleanup
            start_terminal_ttl_cleanup()
        except Exception as e:
            logger.warning(f"terminal TTL 清理任务启动失败: {e}")

        logger.info("Parcae API 启动完成")
        # 运行时端口声明：binding → ready。下游（客户端/外部前端）据此判断
        # 「声明里的端口确实已经能服务」，而不是拿到一个刚 bind 还没 listen 的端口。
        try:
            from lib.runtime_endpoint import STATUS_READY, mark_declaration_status

            mark_declaration_status(STATUS_READY)
        except Exception as e:  # 声明失败不影响服务本身
            logger.warning(f"运行时端口声明更新失败（不影响服务）: {e}")

    @app.on_event("shutdown")
    async def _on_shutdown():
        """应用关闭：清理屏幕、系统、LLM 池统计、记忆、GUI、Loop、浏览器 session。

        每一步用 ``mark_shutdown_step()`` 打点：若某步阻塞，文件日志里最后一条「关闭步骤: xxx」
        就是阻塞点。``start_shutdown_watchdog()`` 给出总时长上限，避免永久滞留。
        """
        start_shutdown_watchdog(source="lifespan")
        _t0 = time.perf_counter()
        logger.info("关闭清理开始")
        # 停止 keys.json 热重载守护（先停，避免关闭过程中又被 mtime 变化触发重建）
        mark_shutdown_step("取消 keys.json 热重载守护")
        try:
            _kw = getattr(app.state, "keys_watch_task", None)
            if _kw is not None:
                _kw.cancel()
        except Exception:
            pass
        # T18：先撤销 SessionManager 授权 + 停止 worker，再清理 GUI 子进程
        # （避免 worker 在 GUI 已关闭后仍 publish 事件到无人订阅的总线）
        mark_shutdown_step("停止 SessionManager（撤销授权 + 停 worker）")
        try:
            from server.screen.session import get_session_manager
            get_session_manager().shutdown()
        except Exception:
            pass
        # T5-1：停止模型存活管理器监控循环（design §11 步骤 5）
        mark_shutdown_step("停止 ModelLifecycleManager")
        try:
            from server.model_manager import get_model_manager
            await get_model_manager().stop()
        except Exception as e:
            logger.warning(f"ModelLifecycleManager 停止失败: {e}")
        # 停止常驻采样中枢（关 NVML 会话、取消 ticker）
        mark_shutdown_step("停止资源采样中枢")
        try:
            from server.resource_probe import get_resource_hub, reset_resource_hub

            await get_resource_hub().stop()
            reset_resource_hub()
        except Exception as e:
            logger.warning(f"资源采样中枢停止失败: {e}")
        mark_shutdown_step("关闭 Screen 模块（含 overlay GUI 子进程）")
        screen_on_shutdown()
        mark_shutdown_step("关闭 System 模块")
        system_on_shutdown()
        # 批量保存模式下 shutdown 时强制落盘
        mark_shutdown_step("落盘 LLM 池统计")
        try:
            from server.llm_pool import get_pool
            get_pool().flush_stats()
        except Exception:
            pass
        mark_shutdown_step("关闭视觉(VL)模块")
        try:
            vision_on_shutdown()
        except Exception:
            pass
        mark_shutdown_step("关闭记忆系统")
        try:
            from server.memory.manager import reset_memory_manager
            reset_memory_manager()
        except Exception:
            pass
        # T18：overlay_client.shutdown() 已在 screen_on_shutdown() 内调用，此处不再重复
        # （重复调用会导致 multiprocessing.Queue put 后再 terminate，子进程可能死锁）
        mark_shutdown_step("停止 Loop 调度器")
        try:
            from server.activity_tracker.loop_manager import get_manager as _get_loop_manager
            from server.activity_tracker.loop_manager import reset_manager as _reset_loop_manager
            mgr = _get_loop_manager()
            await mgr.stop()
            _reset_loop_manager()
        except Exception:
            pass
        mark_shutdown_step("复位收件箱存储")
        try:
            from server.inbox import reset_store as _reset_inbox_store
            _reset_inbox_store()
        except Exception:
            pass
        # 清理持久浏览器 session（评估文档 P0）：关闭长连接 Playwright
        mark_shutdown_step("关闭浏览器 session")
        try:
            from server.browser.session.manager import reset_session_manager
            await reset_session_manager()
        except Exception as e:
            logger.warning(f"清理浏览器 session 失败: {e}")
        # 停止 supervisor 自己启动的浏览器（adopt 的旧实例不动）
        mark_shutdown_step("停止浏览器 supervisor")
        try:
            from server.browser.supervisor import reset_browser_supervisor
            await reset_browser_supervisor()
        except Exception as e:
            logger.warning(f"停止浏览器 supervisor 失败: {e}")
        # Ticket 06: VL 配额批量写盘模式下 shutdown 时强制 flush，避免 dirty 状态丢失
        mark_shutdown_step("落盘 VL 配额")
        try:
            from server.activity_tracker.vl_quota import vl_quota
            vl_quota.flush()
        except Exception as e:
            logger.warning(f"VL 配额 flush 失败: {e}")
        # T07：关闭模块级 httpx client 单例（连接池复用清理）
        # close_async_client 是 async（aclose），close_sync_client 是 sync（close）
        mark_shutdown_step("关闭 httpx 连接池")
        try:
            from lib.async_http import close_async_client, close_sync_client
            await close_async_client()
            close_sync_client()
        except Exception as e:
            logger.warning(f"httpx client 关闭失败: {e}")
        logger.info(f"关闭清理完成，耗时 {int((time.perf_counter() - _t0) * 1000)}ms")
        logger.info("Parcae API 已关闭")
        # 声明置 stopped（保留文件供事后排查）并删掉 pid 文件（start.bat 靠它杀旧实例，
        # 残留 pid 会去杀一个可能已被系统复用的无关进程）。
        try:
            from lib.runtime_endpoint import STATUS_STOPPED, clear_pid_file, mark_declaration_status

            mark_declaration_status(STATUS_STOPPED)
            clear_pid_file()
        except Exception as e:
            logger.warning(f"运行时端口声明收尾失败: {e}")

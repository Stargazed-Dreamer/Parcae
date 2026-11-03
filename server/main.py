"""Parcae FastAPI 后端 - 统一入口

路由拆分:
  - /ocr/*    → server.ocr     (远程 VL / 经典OCR)
  - /agent/*  → server.agent   (LLM评分/对话/社群总结)
  - /browser/*→ server.browser (Chrome CDP控制)
  - /exec/*   → server.exec    (实时执行Python代码)
  - /screen/* → server.screen  (截图/窗口/键鼠操控)
  - /vision/* → server.vision  (视觉AI/远程 VL)
  - /health, /config, /shutdown, /mcp/stats → server.core.*
  - /llm/pool/* → server.core.llm_endpoints
"""

# PaddlePaddle 3.x PIR执行引擎 + oneDNN 兼容性修复
# 必须在import paddle之前设置，否则predict会报
import os

os.environ.setdefault("FLAGS_use_mkldnn", "0")

import ctypes
import sys
import uuid
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI Aware V2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # System DPI Aware
        except Exception:
            pass

import logging

# Windows: 强制 stdout/stderr 用 UTF-8，避免 GBK 导致中文日志/响应在 chcp 65001 终端乱码
# 必须在 colorama.init() 之前执行，让 colorama 包装的是已 reconfigure 的 UTF-8 stream
# 治本：start.bat 设了 chcp 65001（终端按 UTF-8 解码），但 Python 主进程的 sys.stderr
# 默认按 locale 编码（GBK）写入，导致中文日志字节序列不匹配 → 乱码（如 "服务器正在关闭" → "æå¡å¨æ­£å¨å³é­"）
if sys.platform == "win32":
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

# Windows: 初始化 colorama，让 uvicorn/click 输出的 ANSI 颜色码在 cmd.exe 中正确渲染
# 必须在 logging.basicConfig 之前调用，否则 logging 会持有未包装的原始 stream
try:
    import colorama
    colorama.init()
except ImportError:
    pass

# truststore: 让 Python 用 Windows 系统证书库（而非 certifi），修复 GitHub API SSL 验证失败
# 必须在任何 HTTPS 请求发出前注入（workspace 可选组件、远程 VL 等）
# 缺失时静默降级会导致 GitHub API 调用全部 SSL 失败但不报错，
# 因此显式记录 warning 让缺失可观测（缺 truststore 时 backend 仍能启动其他功能）。
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    logging.getLogger(__name__).warning(
        "truststore 未安装：GitHub API SSL 验证会失败（certifi CA bundle 不完整）。"
        "请 `uv pip install truststore` 后重启后端。"
    )

# PaddleX PIR monkey-patch 已移至 server.ocr.ModelManager._apply_pir_patch()
# 此处在启动时同步 import paddlex 会阻塞 4.3 秒，改为在首次 OCR 调用时懒加载。


# ---------------------------------------------------------------------------
# torch 引入口径取证（wip_d6417a7d，**默认关闭**，2026-10-24）
#
# 背景：OCR 只要被加载一次，整套 torch CUDA 栈（torch/lib 映射 ~4GB）就进主进程，
#       而 unload 不 FreeLibrary ⇒ 进程活多久它留多久（空闲 10s 自动卸载只卸模型，
#       卸不掉这 4GB）。"触发面"已钉死 = `POST /models/ocr/load`；唯一没钉死的是
#       **paddlex 的哪个函数 import 的**（候选见 `workspace/windows_forensics/report/`）。
#
# 用法（只在点名实验时开，平常零开销）：
#   PARCAE_TORCH_IMPORT_TRACE=<日志绝对路径> 起后端 → POST /models/ocr/load
#   → 读该日志里的调用栈点名 → **摘掉变量重启回基线**（否则这 4GB 留在进程里）
#
# ⛔ 审计钩子装上就卸不掉，且此后每次 import 都要过一遍回调 ⇒ 必须显式开启，
#    绝不默认挂：生产入口不留取证代码。
# ---------------------------------------------------------------------------
def _install_torch_import_trace() -> None:
    log_path = (os.environ.get("PARCAE_TORCH_IMPORT_TRACE") or "").strip()
    if not log_path:
        return
    import hashlib
    import threading
    import time
    import traceback

    try:
        path = os.path.abspath(log_path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    except Exception:
        return  # 路径不可用就当没开，不能拦住启动

    seen: set[str] = set()
    state = {"n": 0}
    max_entries = 200

    def _hook(event: str, args: tuple) -> None:
        if event != "import" or state["n"] >= max_entries:
            return
        try:
            name = str(args[0]) if args else ""
            # 只记顶层 torch：任何 `torch.xxx` 的首次导入都必然先触发 `torch` 事件，
            # 而子模块事件（torch._utils / torch.nn …）是噪声，一次 import 能刷几百条
            if name != "torch":
                return
            frames = traceback.format_stack(limit=14)  # 只要最近的调用方
            sig = name + "|" + hashlib.md5(
                "".join(frames).encode("utf-8", "replace")).hexdigest()[:8]
            if sig in seen:
                return  # 同一 nam+栈 的重复事件（torch 子模块会连发）只留一条
            seen.add(sig)
            state["n"] += 1
            head = (f"\n===== #{state['n']} import {name} | pid={os.getpid()}"
                    f" | thread={threading.current_thread().name}"
                    f" | {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            # 写文件会再触发 "open" 审计事件 → 回调开头即返回，不会递归
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(head + "".join(frames))
        except BaseException:
            return  # 审计钩子绝不能往外抛（抛了会毒化解释器）

    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n##### trace installed pid={os.getpid()}"
                     f" {time.strftime('%Y-%m-%d %H:%M:%S')} #####\n")
        sys.addaudithook(_hook)
    except Exception:
        pass


_install_torch_import_trace()

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server.activity_tracker.activity import router as activity_router
from server.activity_tracker.headless_endpoints import router as headless_router
from server.activity_tracker.loop_manager import router as loop_router
from server.advanced import router as advanced_router
from server.agent import router as agent_router
from server.agent_guide import router as agent_guide_router
from server.apikey import router as apikey_router
from server.approval_panel_router import router as approval_panel_router
from server.auto_shutdown import router as auto_shutdown_router
from server.browser import router as browser_router
from server.butler.router import router as butler_router
from server.command_guard import router as command_guard_router
from server.dbview import router as dbview_router
from server.docviewer import router as docviewer_router
from server.exec import output_router as exec_output_router
from server.exec import router as exec_router
from server.exec import terminal_router as exec_terminal_router
from server.inbound_gateway.router import router as inbound_gateway_router
from server.inbox import router as inbox_router
from server.memory.router import router as memory_router
from server.mindforge import router as mindforge_router
from server.model_manager.routes import router as model_manager_router
from server.model_manager.types import ModelUnavailableError

# 子路由
from server.ocr import router as ocr_router
from server.probe.router import router as probe_router
from server.screen import router as screen_router
from server.system import router as system_router
from server.templates import router as templates_router

# 所有 /memory/* 端点由 server/memory 提供（三层记忆系统）
from server.todos import router as todos_router
from server.todos import tasks_router as todos_tasks_router
from server.todos import wip_router as todos_wip_router
from server.user_message import router as user_message_router
from server.version import VERSION  # 版本号唯一真源（见 server/version.py 顶部说明）
from server.video import router as video_router
from server.vl.vision import router as vision_router

logger = logging.getLogger("parcae")


class _TraeNotificationFilter(logging.Filter):
    """D10: 抑制 trae/session_stop 通知的 Pydantic 校验错误 WARNING。

    MCP 库 (mcp.shared.session._receive_loop) 对未知通知方法（如
    notifications/trae/session_stop）执行 Pydantic 校验时失败，
    每次都调用 logging.warning("Failed to validate notification: ...")，
    产生数万行噪音。此 filter 匹配并静默这些特定消息。

    保留其他 WARNING 消息（只过滤含 "Failed to validate notification"
    且含 "trae" 的记录）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not ("Failed to validate notification" in msg and "trae" in msg.lower())


def _setup_logging():
    """根据 config.toml [logging] 段配置根 logger。

    - 控制台 handler：按 console_level 输出
    - 文件 handler：file_enabled=true 时追加 RotatingFileHandler（按 file_level）
    - 日志格式统一：%(asctime)s [%(name)s] %(levelname)s: %(message)s
    - D10: 过滤 trae/session_stop 通知的 Pydantic 校验错误 WARNING
    """
    from server.config import get_logging_config

    cfg = get_logging_config()
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    root = logging.getLogger()
    # 清空 basicConfig 默认 handler，避免重复输出
    for h in list(root.handlers):
        root.removeHandler(h)

    # D10: 过滤 trae 通知校验噪音
    # MCP 库 (mcp.shared.session) 对未知通知方法（如 notifications/trae/session_stop）
    # 执行 Pydantic 校验时失败，每次都 log WARNING，产生数万行噪音
    trae_filter = _TraeNotificationFilter()

    # 控制台
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(getattr(logging, cfg["console_level"], logging.INFO))
    console.addFilter(trae_filter)
    root.addHandler(console)

    # Windows 坑（2026-09-12 实测）：multiprocessing spawn 子进程（如 overlay GUI）启动时会
    # 重新 import 本模块（以 __mp_main__ 身份），顶层 _setup_logging() 随之在子进程执行，
    # 子进程便打开了自己的 server.log 句柄并长期持有；主进程 RotatingFileHandler 轮转
    # rename 时因句柄被占永远失败（WinError 32），日志从轮转点起全部写入失败。
    # 因此文件 handler 仅在主进程挂载，子进程日志只走控制台。
    import multiprocessing
    is_main_process = multiprocessing.current_process().name == "MainProcess"

    # pytest 进程内不挂文件 handler（2026-09-30 实测：测试的 ERROR 会落进生产
    # data/logs/server.log——如紧急停止单测的假异常"session 模块炸了"出现 6 组，
    # 污染运维判读）。import 时 pytest 已在 sys.modules，用它判定；生产进程不受影响。
    under_pytest = "pytest" in sys.modules

    # 文件（默认开启，崩溃后可追溯）
    if cfg["file_enabled"] and is_main_process and not under_pytest:
        try:
            from logging.handlers import RotatingFileHandler

            log_path = Path(__file__).parent.parent / cfg["file_path"]
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_h = RotatingFileHandler(
                log_path,
                maxBytes=cfg["max_bytes"],
                backupCount=cfg["backup_count"],
                encoding="utf-8",
            )
            file_h.setFormatter(fmt)
            file_h.setLevel(getattr(logging, cfg["file_level"], logging.INFO))
            file_h.addFilter(trae_filter)
            root.addHandler(file_h)
            root.setLevel(min(
                getattr(logging, cfg["console_level"], logging.INFO),
                getattr(logging, cfg["file_level"], logging.INFO),
            ))
            logger.info(f"文件日志已启用：{log_path} (maxBytes={cfg['max_bytes']}, backupCount={cfg['backup_count']})")
        except Exception as e:
            # 文件日志初始化失败不应阻塞启动，控制台仍可用
            print(f"[WARN] 文件日志初始化失败，仅控制台输出：{e}", file=sys.stderr)
            root.setLevel(getattr(logging, cfg["console_level"], logging.INFO))
    else:
        root.setLevel(getattr(logging, cfg["console_level"], logging.INFO))


_setup_logging()

app = FastAPI(title="Parcae API", version=VERSION)


# ========== 异常处理 ==========

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    """将 Pydantic 验证错误转为可读的 JSON 响应，明确指出哪个字段出错"""
    errors = []
    for err in exc.errors():
        loc = " -> ".join(str(part) for part in err.get("loc", []))
        msg = err.get("msg", "")
        err_type = err.get("type", "")
        # 提供更友好的提示
        hint = ""
        if err_type == "int_type" or "int" in err_type:
            hint = " (需要整数，不能传浮点数)"
        elif err_type == "missing":
            hint = " (必填字段缺失)"
        errors.append({"field": loc, "message": msg, "type": err_type, "hint": hint})
    logger.warning(f"请求验证失败 {request.url.path}: {errors}")
    return JSONResponse(
        status_code=422,
        content={"detail": "请求参数验证失败", "errors": errors},
    )


@app.exception_handler(ModelUnavailableError)
async def model_unavailable_handler(request, exc: ModelUnavailableError):
    """模型存活管理器准入拒绝 → 503（与现有"远程 VL 不可用"503 语义对齐）。

    调用方可程序化区分"暂时不可用"（503，可重试）与"代码错误"（500）。
    """
    hints = {
        "pressure_refusing": "资源压力过高，该模型已被暂停；压力回落后重试",
        "probe_degraded": "资源探测失败，保持保护姿态；请稍后重试",
        "cooldown": "该模型近期加载失败，处于冷却期；请稍后重试",
        "awaiting_first_sample": "监控尚未完成首次采样；请稍后重试",
        "reload_degraded": "该模型连续加载失败，建议重启后端",
        "unloading_in_progress": "模型正在卸载中；请稍后重试",
    }
    logger.warning(
        f"模型不可用 {request.url.path}: model={exc.model_id} reason={exc.reason}"
    )
    return JSONResponse(
        status_code=503,
        content={
            "error": "model_unavailable",
            "model_id": exc.model_id,
            "reason": exc.reason,
            "hint": hints.get(exc.reason, "模型暂不可用，请稍后重试"),
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception):
    """全局兜底：未捕获异常返回 500 + trace_id，记录完整堆栈到日志（含文件）。

    trace_id 由 trace_id_middleware 预先生成并存于 request.state；
    客户端收到 trace_id 后可在 data/logs/server.log 中检索对应请求。
    """
    import traceback

    trace_id = getattr(request.state, "trace_id", None) or uuid.uuid4().hex[:12]
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error(
        f"未捕获异常 trace_id={trace_id} {request.method} {request.url.path}: {exc}\n{tb}"
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal Server Error",
            "trace_id": trace_id,
            "error_type": type(exc).__name__,
            "message": str(exc),
        },
    )


# ========== CORS ==========

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|tauri\.localhost)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========== 中间件 ==========

from server.core.middleware import _load_mcp_stats, register_middleware

register_middleware(app)
_load_mcp_stats()  # 启动时加载 MCP 调用统计


# ========== 子路由注册 ==========

app.include_router(ocr_router)
app.include_router(agent_router)
app.include_router(auto_shutdown_router)
app.include_router(apikey_router)
app.include_router(inbound_gateway_router)  # 入站网关：/v1/* OpenAI 兼容 + /inbound/* 管理
app.include_router(browser_router)
app.include_router(exec_router)
app.include_router(exec_terminal_router)  # 终端会话（独立 prefix /terminals）
app.include_router(exec_output_router)    # 执行输出查看（独立 prefix /output）
app.include_router(command_guard_router)
app.include_router(approval_panel_router)  # 审批面板（/approvals/*）
app.include_router(screen_router)
app.include_router(vision_router)
app.include_router(memory_router)     # 三层记忆系统 v2（向后兼容旧 KV 接口）
app.include_router(dbview_router)      # 数据库/文件只读浏览（面板「数据库」页，prefix /db）
app.include_router(todos_router)       # 待办模块（周期任务）
app.include_router(todos_wip_router)   # WIP 任务追踪（独立 prefix /wip）
app.include_router(todos_tasks_router)  # 事务分类/队列（DL2，prefix /tasks）
app.include_router(butler_router)        # 会话码/上下文/收尾（DL4，prefix /butler）
app.include_router(mindforge_router)
app.include_router(model_manager_router)   # Model Lifecycle Manager 控制面（design §10）
app.include_router(docviewer_router)
app.include_router(system_router)
app.include_router(advanced_router)
app.include_router(templates_router)
app.include_router(video_router)          # 课程视频按秒取帧（内存直读 ImageContent）
app.include_router(agent_guide_router)
app.include_router(user_message_router)
app.include_router(loop_router)          # Loop 自动触发系统
app.include_router(headless_router)      # headless-agent-session（中断端点）
app.include_router(inbox_router)          # 收件箱
app.include_router(activity_router)       # Activity 数据查看/编辑/审核
app.include_router(probe_router)          # chat-mgmt-probe（对话管理探针）

# 运行时端点：/runtime/port 供客户端与外部前端查询实际端口（端口顺延后仍能找到）
from server.core.runtime_routes import router as runtime_router

app.include_router(runtime_router)


# ========== 核心路由（health/config/shutdown/mcp_stats/llm_pool） ==========

from server.core.config_routes import register_config_routes
from server.core.health import register_health_routes
from server.core.llm_endpoints import register_llm_routes

register_health_routes(app, version=VERSION)
register_config_routes(app)
register_llm_routes(app)


# ========== 路由安全标签 ==========

from server.route_tags import auto_tag_routes, build_safety_lookup, init_op_path_map

auto_tag_routes(app)
app.state.safety_lookup = build_safety_lookup(app)
# 构建 operation_id → (method, path) 反查表（MCP 网关二次确认审批级别时用）
init_op_path_map(app)

# 构建路径 → operation_id 映射（三层审批用，必须在所有路由注册后）
from server.approval_review import init_path_map

init_path_map(app)


# ========== 生命周期（startup/shutdown） ==========

from server.core.lifecycle import register_lifecycle

register_lifecycle(app)


# ========== MCP 工具接口 ==========

from server.core.mcp_gateway import setup_mcp

setup_mcp(app)


# ========== 静态文件服务 ==========

# workspace 数据（供可视化页面读取）
# ⛔ 这里不要打印带端口的 URL：模块导入时还不知道最终端口（见文件末尾的
# 端口预探测），写死 8766 会在端口顺延时给出误导性地址。
WORKSPACE_DIR = Path(__file__).parent.parent / "workspace"
if WORKSPACE_DIR.exists():
    app.mount("/workspace", StaticFiles(directory=str(WORKSPACE_DIR)), name="workspace")
    logger.info("工作区静态目录已挂载: /workspace")


if __name__ == "__main__":
    import uvicorn

    from lib.port_binding import bind_first_available, diagnose_commands, log_port_environment
    from lib.runtime_endpoint import (
        STATUS_BINDING,
        plan_rewrite_sources,
        read_declaration,
        rewrite_consumers,
        write_declaration,
    )
    from server.config import get_server_config

    cfg = get_server_config()
    host: str = cfg["host"]
    preferred_port: int = int(cfg["port"])

    # ========== 端口环境体检（只读，不阻塞启动）==========
    # 根因层面：动态端口范围被「网络优化 / 游戏加速器」类工具改回低位后，WinNAT 的
    # 保留块会落到 8766 这类常用端口上，于是某次开机后突然「起不来」。
    # 体检结果落 data/logs/server.log ⇒ 以后这类改动在日志里可见，不用等下次启动失败才发现。
    port_env = log_port_environment(preferred_port)

    # ========== 端口预探测（必须在 uvicorn 之前）==========
    # uvicorn 的 bind 失败发生在 **lifespan startup 之后**，异常被它内部吞掉并退出：
    # 我们既捕获不到、也无法换端口重试，而文件日志里只会留下「启动完成 → 关闭清理」，
    # 一条错误都没有（2026-10-19 实际事故：只看 server.log 会误判成"启动成功但莫名自杀"）。
    # 所以先自己 bind，再通过 server.run(sockets=[sock]) 交给 uvicorn（无 TOCTOU 竞态）。
    prev_decl = read_declaration()
    prev_port = prev_decl.get("port") if isinstance(prev_decl, dict) else None

    bind = bind_first_available(host, preferred_port)
    if bind.sock is None:
        lines = [
            f"后端启动失败：从 {preferred_port} 起的 {len(bind.attempts)} 个端口全部不可绑定。",
            f"  {bind.failure_summary}",
            "诊断命令（按建议顺序执行）：",
        ]
        lines += [f"  {cmd}" for cmd in diagnose_commands(preferred_port)]
        msg = "\n".join(lines)
        logger.error(msg)  # 落 data/logs/server.log —— 不再只活在 stderr 里
        print("\n" + msg + "\n", file=sys.stderr, flush=True)
        sys.exit(2)

    # 对外声明实际端口：此时服务尚未 listen，故 status=binding；
    # lifespan 就绪后由 server/core/lifecycle.py 改成 ready。
    decl = write_declaration(
        host=host,
        port=bind.port,
        preferred_port=preferred_port,
        degraded=bind.degraded,
        reason=bind.reason,
        status=STATUS_BINDING,
        # 端口环境一并对外：下游查 /runtime/port 就能知道"这次顺延是偶发还是环境被改坏了"
        extra={"port_env": port_env.as_dict()},
    )

    if bind.degraded:
        msg = (
            f"⚠ 端口顺延：首选 {preferred_port} 不可用，已改用 {bind.port}。\n"
            f"  原因: {bind.reason}\n"
            f"  ⛔ 外部 MCP 客户端（IDE）不读我们的声明文件，需把配置里的端口改成 {bind.port} 并重连。"
        )
        logger.warning(msg)
        print(msg, flush=True)
        # 同步已登记的下游配置文件（改前备份、只替换匹配到的回环 URL）
        #
        # ⚠ 源端口不能只试首选端口：上一次若已顺延过，下游文件里写的是**上次的实际端口**
        # （如 9808）而不是首选端口（8766），只按首选端口匹配会得到 skipped_no_match，
        # 下游静默停在旧端口 ⇒ MCP 客户端连不上（2026-10-20 实际事故：8766→9808→8908
        # 两次顺延后，四个下游配置全部 "未见 127.0.0.1:8766" 而没被改写）。
        _src_ports = plan_rewrite_sources(preferred_port, prev_port, bind.port)
        for _src in _src_ports:
            for rep in rewrite_consumers(_src, bind.port):
                if rep.status == "rewritten":
                    logger.warning("下游配置已同步: %s → %s（%s）", rep.consumer_id, rep.path, rep.detail)
                    print(f"  ✔ 已同步下游配置 {rep.consumer_id}: {rep.path}（{rep.detail}）", flush=True)
                elif rep.status != "skipped_missing":
                    logger.info("下游配置未改动: %s [%s] %s", rep.consumer_id, rep.status, rep.detail)
    elif prev_port and int(prev_port) != bind.port:
        # 上次顺延过、这次首选端口恢复了 ⇒ 把下游改回去，否则它们还指着旧端口
        for rep in rewrite_consumers(int(prev_port), bind.port):
            if rep.status == "rewritten":
                logger.warning("端口已恢复，下游配置回写: %s → %s", rep.consumer_id, rep.path)
                print(f"  ✔ 端口已恢复，回写下游配置 {rep.consumer_id}: {rep.path}", flush=True)

    logger.info("Parcae 后端: %s", decl["base_url"])
    print(f"Parcae 后端: {decl['base_url']}", flush=True)

    # 传 app 对象而非字符串 "server.main:app"：避免 uvicorn 重新 import server.main 模块
    # 导致顶层代码（auto_tag_routes/setup_mcp/静态挂载等）执行两次（重复启动 + 重复打 tag）
    #
    # timeout_graceful_shutdown 必须显式给值（配置项 server.graceful_shutdown_timeout_secs）：
    # uvicorn 默认 None = 无限等待「在飞连接/任务结束」（server.py: wait_for(..., timeout=None)）。
    # 本机未装 httptools ⇒ 走 h11，h11 的 Connection.shutdown() 对「有在飞请求」的连接
    # 只置 keep_alive=False、不关连接 ⇒ 只要有 1 个请求不返回（LLM 池排队 / 未断开的 SSE /
    # IDE 的 MCP 长连接），后端就永久停在
    # "Waiting for connections to close. (CTRL+C to force quit)"，进程永远退不出。
    # 给了超时后：到点 uvicorn 打 "Cancel N running task(s), timeout graceful shutdown
    # exceeded"、取消挂起任务，再正常走 lifespan 清理（server/core/lifecycle.py）。
    #
    # ⛔ 不能用 uvicorn.run(app, sockets=[...])：0.49 的 run() 不接 sockets 参数
    # （Config 没有该形参），只有 Server.run/serve 接。所以这里直接构造 Config + Server。
    uvicorn_config = uvicorn.Config(
        app,
        host=host,
        # 必须传**实际**端口：uvicorn 的启动横幅与访问日志都用它，
        # 传首选端口会让日志显示 8766 而服务其实在 8767。
        port=bind.port,
        timeout_graceful_shutdown=cfg["graceful_shutdown_timeout_secs"],
    )
    server = uvicorn.Server(uvicorn_config)
    try:
        server.run(sockets=[bind.sock])
    except KeyboardInterrupt:
        # 与 uvicorn.run() 内部行为一致：Ctrl+C 走正常关停，不当作错误
        pass

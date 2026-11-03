"""HTTP 中间件：Host 白名单 + trace_id 注入 + MCP 统计 + 用户消息注入"""

import asyncio
import json
import logging
import threading
import time
import uuid
from pathlib import Path

from fastapi.responses import JSONResponse
from fastapi.responses import Response as FastAPIResponse

from server.user_message import (
    get_supplement_text,
    has_pending,
    should_inject,
)

logger = logging.getLogger("parcae.middleware")


# ========== MCP 工具调用统计（原 server/mcp_stats.py 归并） ==========
# 数据持久化到 data/mcp_stats.json，重启后保留。
_STATS_FILE = Path(__file__).parent.parent.parent / "data" / "mcp_stats.json"
_stats: dict[str, dict] = {}
_stats_lock = threading.Lock()

# 网关入口工具名：这些工具本身只是一个转发壳，真实被调用的操作由 arguments.tool 指定。
# 不拆出来的话，117 个网关 operation_id 的调用量全堆在壳这一个 key 上，覆盖度不可见。
# ⛔ 真源是 server/advanced.py 的 build_registry_from_app（网关 operation_id 集合）；
# 这里只列"壳"的名字。新增网关入口时要同步本集合，漂移由
# workspace/tool_dojo 的 L0 契约层检测（比对 /advanced/tools 的实际入口）。
_GATEWAY_TOOL_NAMES: frozenset[str] = frozenset({"parcae_advanced_tool"})


def _load_mcp_stats() -> None:
    """启动时加载统计数据（一次性，由 setup_mcp 触发）"""
    global _stats
    if _STATS_FILE.exists():
        try:
            _stats = json.loads(_STATS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _stats = {}
    else:
        _stats = {}


def _save_mcp_stats(snapshot: dict[str, dict]) -> None:
    """写全量统计 JSON。只序列化调用方传入的快照，不直接读 _stats（避免竞态）。"""
    try:
        _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATS_FILE.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def record_call(
    tool_name: str,
    duration_ms: float,
    success: bool = True,
    via_gateway: bool = False,
) -> None:
    """记录一次 MCP 工具调用。

    via_gateway=True 表示这次调用是经网关壳转发的，tool_name 已被解析为真实
    operation_id（见 mcp_stats_middleware）。该标记只写不擦：同一操作若两种
    通道都走过，最终为 true，用于区分"直连调过"与"只能走网关调"。
    """
    with _stats_lock:
        if tool_name not in _stats:
            _stats[tool_name] = {
                "count": 0, "error_count": 0, "total_ms": 0.0,
                "last_called": "", "last_error": "",
            }
        entry = _stats[tool_name]
        if via_gateway:
            entry["via_gateway"] = True
        entry["count"] += 1
        entry["total_ms"] += duration_ms
        entry["last_called"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if not success:
            entry["error_count"] += 1
            entry["last_error"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        # 持锁内做快照：保存线程只序列化快照，不与后续 record_call 变更竞态
        snapshot = {name: dict(e) for name, e in _stats.items()}
    # 异步保存（不阻塞请求）
    threading.Thread(target=_save_mcp_stats, args=(snapshot,), daemon=True).start()


def get_mcp_stats() -> dict:
    """获取统计数据，按调用次数降序排列"""
    with _stats_lock:
        result = []
        for name, entry in _stats.items():
            avg_ms = round(entry["total_ms"] / entry["count"], 1) if entry["count"] > 0 else 0
            result.append({
                "tool": name,
                "count": entry["count"],
                "error_count": entry.get("error_count", 0),
                "avg_ms": avg_ms,
                "total_ms": round(entry["total_ms"], 1),
                "last_called": entry.get("last_called", ""),
            })
        result.sort(key=lambda x: x["count"], reverse=True)
        return {
            "total_calls": sum(r["count"] for r in result),
            "unique_tools": len(result),
            "tools": result,
        }


def reset_mcp_stats() -> None:
    """重置统计数据"""
    global _stats
    with _stats_lock:
        _stats = {}
    _save_mcp_stats({})


# ========== Host 白名单（DNS rebinding 防护） ==========
# 为什么 CORS 挡不住：后端只监听 127.0.0.1，但攻击者可让受害者在浏览器里访问
# http://evil.com/ 时把该域名解析到 127.0.0.1:8766 —— 此时浏览器发出的 Origin
# 就是 http://127.0.0.1:8766，main.py 的 allow_origin_regex 照放行。
# 唯一能区分"真本地客户端"与"借道浏览器的外网页面"的依据是 Host 头：
# 正常客户端发 127.0.0.1，被 rebinding 的页面发 evil.com。
#
# ⛔ 但光看 Host 会误伤合法客户端：Host 是客户端的内部实现细节，**与 url 里的主机名
# 可以不一致** —— IDE 的 streamableHttp MCP 客户端 url 写 http://127.0.0.1:8766/mcp，
# 实际发出的 Host 却是 `apiserver`（IDE 内部网关名）；经本地代理 / sidecar /
# stdio↔http 桥 / 云转发的 harness 同理。只按 Host 判定 ⇒ 这类客户端被整体 421，
# agent 直接失能（2026-09-23 实测）。
# ⇒ 判据改为**合取**：像浏览器 **且** Host 不在放行集合。rebinding 只能借道浏览器，
# 非浏览器客户端不在攻击面上 —— 既不误伤，防护强度也不降。
_HOST_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
# FastAPI/Starlette 的 TestClient 固定用 base_url=http://testserver（httpx 约定）。
# 冒烟测试与大量单测走 TestClient —— 不放行会把"提交前机械防线"整体打红，
# 因此它属于白名单而非可配置开关（攻击者无法借 rebinding 使用这个名字：
# 浏览器只会把用户正在访问的那个域名放进 Host）。
_HOST_TESTCLIENT = "testserver"
# ⛔ IDE 的 streamableHttp MCP 客户端（~/.workbuddy/mcp.json 里 url 写的是
# http://127.0.0.1:8766/mcp，但实际发出的 Host 头是 `apiserver`——IDE 内部网关名，
# 与 URL 里的 127.0.0.1 不一致）。不放行 ⇒ **全部 MCP 工具调用 421，agent 直接失能**
# （2026-09-23 实测：`GET /wip/wip_0d3ff369` 被拒，data/logs/server.log 留证）。
# 安全评估与 testserver 同级：rebinding 只能借道浏览器，浏览器只会把用户正在访问的
# 那个域名放进 Host，攻击者无法让受害者浏览器把 Host 设成 apiserver。
# 与 testserver 一样放在代码常量而非 config：属于"工具面能工作"的硬前提，
# 不能因为某台机器缺 config.toml 而静默退化成 agent 失能。
_HOST_IDE_MCP = "apiserver"
# 监听地址写成通配 = 有意暴露到局域网，此时不再自动放行，必须显式配 host_whitelist
_HOST_WILDCARD_BIND = {"0.0.0.0", "::", ""}
# 浏览器指纹头：现代浏览器（Chrome 76+ / Firefox 90+ / Safari 16.4+）发起的请求必带
# Sec-Fetch-*（跨站时为 Sec-Fetch-Site: cross-site），跨站 fetch/XHR 还带 Origin。
# 任一项出现 ⇒ 请求由浏览器发出 ⇒ 才需要校验 Host。
_BROWSER_FINGERPRINT_HEADERS = (
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "sec-fetch-user",
)

_allowed_hosts_cache: set[str] | None = None


def _looks_like_browser(request) -> bool:
    """请求是否由浏览器发出（DNS rebinding 只能借道浏览器）。

    三个信号任一命中即算：Origin 头、Sec-Fetch-* 头、Mozilla 系 User-Agent。
    非浏览器客户端（curl / httpx / 各路 MCP harness）默认都不带 ⇒ 不会被 Host 检查误伤。
    """
    headers = request.headers
    if headers.get("origin"):
        return True
    if any(h in headers for h in _BROWSER_FINGERPRINT_HEADERS):
        return True
    # UA 由浏览器决定、页面 JS 改不了 ⇒ 也是可靠指纹；curl/httpx 的 UA 不含 Mozilla/
    return headers.get("user-agent", "").startswith("Mozilla/")


def _reset_allowed_hosts_cache() -> None:
    """清空白名单缓存（测试或 config 热改后用；下次请求重算）"""
    global _allowed_hosts_cache
    _allowed_hosts_cache = None


def _allowed_hosts() -> set[str]:
    """当前允许的 Host 名集合（小写、不含端口）。

    组成 = 回环地址 + testserver + IDE MCP 客户端名(apiserver) + 实际监听地址(非通配时)
    + config 的 server.host_whitelist。
    结果缓存在模块级：config.toml 改这几项需要重启后端才生效（避免每请求读盘）。
    """
    global _allowed_hosts_cache
    if _allowed_hosts_cache is not None:
        return _allowed_hosts_cache

    hosts = set(_HOST_LOOPBACK) | {_HOST_TESTCLIENT, _HOST_IDE_MCP}
    try:
        from server.config import get_server_config

        cfg = get_server_config()
    except Exception:  # 配置不可读时不缩小放行面，退化为纯回环
        logger.warning("[host_guard] 读取 server 配置失败，Host 白名单退化为仅回环")
        _allowed_hosts_cache = hosts
        return hosts

    bind = str(cfg.get("host") or "").strip().lower()
    if bind and bind not in _HOST_WILDCARD_BIND:
        hosts.add(bind)

    extra = cfg.get("host_whitelist") or []
    if isinstance(extra, str):
        extra = [h for h in extra.replace(",", " ").split() if h]
    hosts.update(str(h).strip().lower() for h in extra if str(h).strip())

    _allowed_hosts_cache = hosts
    return hosts


def _host_header_to_name(host_header: str) -> str:
    """'evil.com:8766' / '[::1]:8766' / 'user@host:80' → 'evil.com' / '::1' / 'host'"""
    name = host_header.strip().lower()
    if "@" in name:  # Host 允许带 userinfo，实际不会出现在正常请求里
        name = name.rsplit("@", 1)[1]
    if name.startswith("["):  # IPv6 字面量
        end = name.find("]")
        name = name[1:end] if end > 0 else name[1:]
    elif ":" in name:  # IPv4 或域名带端口
        name = name.split(":", 1)[0]
    return name.strip(".")


def register_middleware(app):
    """注册所有 HTTP 中间件。

    注册顺序 = 执行顺序（Starlette 先注册者在外层）：host_guard 放在最外层，
    被拒请求不再进入后续中间件与路由。
    """

    @app.middleware("http")
    async def host_guard_middleware(request, call_next):
        """拒绝"像浏览器发起 且 Host 不在白名单"的请求（DNS rebinding 防护）。

        两个条件**合取**，缺一不可：
          1. 像浏览器（`_looks_like_browser`：Origin / Sec-Fetch-* / Mozilla 系 UA）
          2. Host 名不在放行集合（回环 / testserver / apiserver / 监听地址 / host_whitelist）

        为什么必须加条件 1：Host 与 url 里的主机名可以不一致（IDE MCP 客户端 url 写
        127.0.0.1 却发 `apiserver`），只按 Host 判定会把这类合法客户端整体打 421。
        rebinding 只能借道浏览器，因此"非浏览器 + 奇怪 Host"不在攻击面上 ⇒ 放行。
        缺失 Host 头时放行 —— 浏览器对 HTTP/1.1 必带 Host，rebinding 只能借道浏览器。
        """
        host_header = request.headers.get("host", "")
        if not host_header:
            return await call_next(request)

        name = _host_header_to_name(host_header)
        if name in _allowed_hosts():
            return await call_next(request)

        if not _looks_like_browser(request):
            logger.debug(
                f"[host_guard] 非浏览器客户端，放行非白名单 Host={host_header!r} "
                f"path={request.url.path}"
            )
            return await call_next(request)

        logger.warning(
            f"[host_guard] 拒绝非白名单 Host={host_header!r} "
            f"path={request.url.path} method={request.method}"
        )
        return JSONResponse(
            status_code=421,  # Misdirected Request：服务器不为该 Host 提供服务
            content={
                "error": "host_not_allowed",
                "message": "Host 头不在白名单内（疑似 DNS rebinding）。"
                           "本机后端仅服务 127.0.0.1/localhost。",
                "host": name,
            },
        )

    @app.middleware("http")
    async def trace_id_middleware(request, call_next):
        """为每个请求注入 trace_id，贯穿日志和错误响应。

        - 优先复用客户端传入的 X-Request-ID（便于跨进程追踪）
        - 否则生成 12 位 hex
        - 存入 request.state.trace_id 供异常处理器和路由使用
        - 响应头回传 X-Request-ID
        """
        trace_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.trace_id = trace_id
        response = await call_next(request)
        try:
            response.headers["X-Request-ID"] = trace_id
        except Exception:
            pass
        return response

    @app.middleware("http")
    async def mcp_stats_middleware(request, call_next):
        """拦截 /mcp POST 请求，记录工具调用统计"""
        if request.url.path == "/mcp" and request.method == "POST":
            # 读取请求体
            body = await request.body()
            tool_name = None
            via_gateway = False
            try:
                data = json.loads(body)
                # MCP JSON-RPC: {"method": "tools/call", "params": {"name": "tool_name"}}
                if data.get("method") == "tools/call":
                    params = data.get("params") or {}
                    tool_name = params.get("name")
                    # 网关壳：真实操作在 arguments.tool 里，拆出来单独统计
                    if tool_name in _GATEWAY_TOOL_NAMES:
                        arguments = params.get("arguments") or {}
                        inner = arguments.get("tool") if isinstance(arguments, dict) else None
                        if isinstance(inner, str) and inner:
                            tool_name = inner
                            via_gateway = True
            except (json.JSONDecodeError, AttributeError):
                pass

            start = time.perf_counter()
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start) * 1000

            if tool_name:
                success = 200 <= response.status_code < 400
                record_call(tool_name, duration_ms, success, via_gateway=via_gateway)

                # 记忆系统触发器：记录工具调用到三层记忆
                try:
                    from server.memory.manager import get_memory_manager
                    mgr = get_memory_manager()
                    if mgr.config.auto_record and tool_name not in mgr.config.exclude_tools:
                        # 从请求中提取参数摘要
                        args_summary = {}
                        try:
                            params = data.get("params", {})
                            arguments = params.get("arguments", {})
                            if isinstance(arguments, dict):
                                for k, v in list(arguments.items())[:5]:
                                    args_summary[k] = str(v)[:100] if isinstance(v, str) else v
                        except Exception:
                            pass
                        mgr.recorder.record_tool_call(
                            tool_name=tool_name,
                            arguments=args_summary,
                            duration_ms=duration_ms,
                        )
                except Exception:
                    pass  # 记忆系统不可用时不影响主流程

            return response
        return await call_next(request)

    @app.middleware("http")
    async def user_message_inject_middleware(request, call_next):
        """将待发送的用户补充指令注入到非工作端点的响应中"""
        response = await call_next(request)

        # 排除工作端点和系统端点
        if not should_inject(request.url.path):
            return response
        if not has_pending():
            return response

        # 消费待发送消息
        supplement = get_supplement_text()
        if not supplement:
            return response

        # 始终添加 header（URL 编码，因为 HTTP header 只支持 Latin-1）
        try:
            from urllib.parse import quote
            response.headers["X-User-Supplement"] = quote(supplement)[:2000]
        except Exception:
            pass  # header 设置失败不影响 JSON 注入

        # 对 JSON 响应注入 _user_supplement 字段
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            # 收集响应体
            body_bytes = b""
            async for chunk in response.body_iterator:
                if isinstance(chunk, str):
                    body_bytes += chunk.encode("utf-8")
                else:
                    body_bytes += chunk

            try:
                data = json.loads(body_bytes)
                if isinstance(data, dict):
                    # 端点自己可能已经挂了「定向给这一路」的留言（v14），别覆盖掉
                    existing = data.get("_user_supplement")
                    data["_user_supplement"] = (
                        f"{existing}\n{supplement}" if existing else supplement
                    )
                else:
                    data = {"_original": data, "_user_supplement": supplement}
                new_body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                headers = dict(response.headers)
                headers["content-length"] = str(len(new_body))
                return FastAPIResponse(
                    content=new_body,
                    status_code=response.status_code,
                    headers=headers,
                    media_type="application/json",
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                pass  # 非 JSON，仅使用 header

        return response

    @app.middleware("http")
    async def http_guard_middleware(request, call_next):
        """对 agent 调用的 approval_required 端点要求 X-Approval-Token。

        仅拦截 agent 发起的请求（判据见 lib.agent_identity.is_agent_caller：
        出现 X-Agent-Caller 头且值不是否定词）。用户/GUI 请求不带此头，直接放行。
        """
        if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
            return await call_next(request)

        safety_lookup = getattr(request.app.state, "safety_lookup", None)
        if not safety_lookup:
            return await call_next(request)

        safety = None
        for path_regex, method, level in safety_lookup:
            if method == request.method and path_regex.match(request.url.path):
                safety = level
                break

        if safety != "approval_required":
            return await call_next(request)

        # 二次确认：按当前 approval_level 过滤（用户在 GUI 改 level 后立即生效，
        # 无需重建 safety_lookup）。lookup 始终按 strict 完整清单构建，
        # 若当前 level 为 moderate/loose/none，对应端点在此降级为 safe 放行。
        from server.route_tags import classify_safety_runtime
        runtime_safety = classify_safety_runtime(request.method, request.url.path)
        if runtime_safety != "approval_required":
            return await call_next(request)

        # 判据真源在 lib.agent_identity：客户端发的是 "v6-lite-agent"，
        # 旧代码写死 == "true" 会把 agent 流量误判成 GUI 用户流量而直接放行。
        from lib.agent_identity import is_agent_caller

        is_agent = is_agent_caller(request.headers)
        if not is_agent:
            return await call_next(request)

        body = await request.body()

        async def _receive():
            return {"type": "http.request", "body": body, "more_body": False}
        request._receive = _receive

        # 三层递进审批：静态规则 → LLM 审查 → 人审
        from server.approval_review import get_op_from_path, try_auto_approve, user_review_for_llm_deny
        operation_id = get_op_from_path(request.url.path)
        user_feedback = ""
        if operation_id:
            body_dict = {}
            if body:
                try:
                    body_dict = json.loads(body)
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            review = await asyncio.to_thread(
                try_auto_approve, operation_id, body_dict, request.method, request.url.path
            )
            if review["auto_approved"]:
                return await call_next(request)
            # 静态 block 或 LLM 未放行 → 直接弹人审，用户批准则放行
            if not review["auto_approved"] and review.get("layer1_static") in ("block", "pass"):
                block_decision = "static_block" if review.get("layer1_static") == "block" else review.get("layer2_llm", "deny")
                user_result = await user_review_for_llm_deny(
                    operation_id, body_dict, request.method, request.url.path,
                    body, review.get("reason", ""), block_decision,
                )
                user_feedback = user_result.get("feedback", "")
                if user_result["approved"]:
                    return await call_next(request)
                # 用户已拒绝：直接返回 403 + user_denied + feedback，不再创建新 approval_id
                # 治本：避免 agent 收到 "approval_required" 后重复申请，同时回传用户 feedback
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "user_denied",
                        "message": "用户已拒绝此操作。请尊重用户决定，不要重复申请审批；如需继续请说明理由或改用更安全方案。",
                        "user_feedback": user_feedback,
                        "operation_id": operation_id,
                    },
                )

        # 未弹过人审（layer1=skipped，非审查端点），走标准 approval_required 流程
        from server.http_guard import check_approval
        token = request.headers.get("X-Approval-Token", "")
        approval_id = check_approval(request.method, request.url.path, body, token)
        if approval_id is None:
            return await call_next(request)

        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=403,
            content={
                "error": "approval_required",
                "approval_id": approval_id,
                "message": f"HTTP {request.method} {request.url.path} 需要 user 审批",
            },
        )

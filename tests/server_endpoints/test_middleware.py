"""HTTP 中间件测试 — Host 白名单 + MCP 统计 + 用户消息注入

middleware 注册三个 http 中间件：
1. host_guard_middleware — 拒绝非白名单 Host（DNS rebinding 防护）
2. mcp_stats_middleware — 拦截 /mcp POST，记录工具调用统计到 mcp_stats
3. user_message_inject_middleware — 将待发送用户消息注入非工作端点响应

测试通过 TestClient 发送请求验证中间件行为。
"""


import pytest

import server.user_message as um
from server.core.middleware import (
    _host_header_to_name,
    _reset_allowed_hosts_cache,
)

# ========== Host 白名单（DNS rebinding 防护） ==========

class TestHostWhitelist:
    """放行集合内的 Host 一律放行；非白名单 Host **仅当请求像浏览器**时才 421"""

    @pytest.mark.parametrize("host", [
        "127.0.0.1:8766",
        "localhost:8766",
        "[::1]:8766",
        "testserver",          # TestClient 的 base_url，冒烟测试依赖
        "apiserver",           # IDE streamableHttp MCP 客户端实际发出的 Host（不放行=agent 失能）
    ])
    def test_allowed_hosts(self, client, host):
        """白名单 Host 正常放行（不破坏既有本地调用）"""
        resp = client.get("/health", headers={"Host": host})
        assert resp.status_code == 200, f"Host={host} 应放行"

    @pytest.mark.parametrize("host", [
        "evil.example.com",
        "attacker.top:8766",        # rebinding 典型形态：域名解析到 127.0.0.1
        "192.168.4.105:8766",       # 局域网 IP 写法同样不在默认白名单
    ])
    def test_rejected_hosts_from_browser(self, client, host):
        """浏览器发起 + 非白名单 Host → 421 + host_not_allowed"""
        resp = client.get("/health", headers={
            "Host": host,
            "Origin": "http://evil.example.com",
            "Sec-Fetch-Site": "cross-site",
        })
        assert resp.status_code == 421, f"Host={host}（浏览器发起）应被拒"
        assert resp.json()["error"] == "host_not_allowed"

    @pytest.mark.parametrize("fingerprint", [
        {"Origin": "http://evil.example.com"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Mode": "cors"},
        {"Sec-Fetch-Dest": "empty"},
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    ])
    def test_rejected_on_any_browser_fingerprint(self, client, fingerprint):
        """任一浏览器指纹命中即判为浏览器（堵住只带其中一种头的浏览器）"""
        headers = {"Host": "evil.example.com"}
        headers.update(fingerprint)
        resp = client.get("/health", headers=headers)
        assert resp.status_code == 421, f"指纹 {fingerprint} 应判为浏览器"

    @pytest.mark.parametrize("host", [
        "evil.example.com",
        "attacker.top:8766",
        "192.168.4.105:8766",
        "apiserver",                 # IDE MCP 客户端：url 写 127.0.0.1，Host 却是网关名
        "some-internal-gateway",     # 未知的本地代理 / sidecar / stdio↔http 桥
    ])
    def test_non_browser_client_never_blocked(self, client, host):
        """★ 非浏览器客户端无论 Host 写成什么都不拦。

        Host 是客户端的内部实现细节，与 url 里的主机名可以不一致；只按 Host 判定
        会让经中间层的合法客户端整体 421（agent 失能）。rebinding 只能借道浏览器，
        因此"非浏览器 + 奇怪 Host"不在攻击面上。
        """
        resp = client.get("/health", headers={"Host": host})
        assert resp.status_code == 200, f"非浏览器客户端 Host={host} 不应被拦"

    def test_host_whitelist_config_extends(self, client, monkeypatch):
        """config 的 server.host_whitelist 可追加放行项（浏览器发起也放行）"""
        monkeypatch.setattr(
            "server.config.get_server_config",
            lambda: {"host": "127.0.0.1", "port": 8766, "host_whitelist": ["mybox.local"]},
        )
        _reset_allowed_hosts_cache()
        try:
            resp = client.get("/health", headers={
                "Host": "mybox.local:8766",
                "Sec-Fetch-Site": "same-origin",
            })
            assert resp.status_code == 200
        finally:
            _reset_allowed_hosts_cache()

    @pytest.mark.parametrize("header,expected", [
        ("evil.com:8766", "evil.com"),
        ("[::1]:8766", "::1"),
        ("LOCALHOST:8766", "localhost"),
        ("user@host.example:80", "host.example"),
        ("127.0.0.1", "127.0.0.1"),
    ])
    def test_host_header_parsing(self, header, expected):
        """Host 头解析：去端口 / 去 IPv6 方括号 / 去 userinfo / 小写"""
        assert _host_header_to_name(header) == expected

# ========== 用户消息注入中间件 ==========

class TestUserMessageInjection:
    """用户消息注入到非工作端点的 JSON 响应"""

    @pytest.fixture(autouse=True)
    def clean_messages(self):
        """每个测试前后清空消息"""
        um.clear_all()
        yield
        um.clear_all()

    def test_inject_into_json_response(self, client):
        """消息注入到 JSON 响应的 _user_supplement 字段"""
        um.add_message("开始新任务")
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "_user_supplement" in data
        assert "开始新任务" in data["_user_supplement"]

    def test_inject_header(self, client):
        """消息注入到 X-User-Supplement header"""
        um.add_message("header 测试")
        resp = client.get("/health")
        assert "x-user-supplement" in {k.lower() for k in resp.headers}

    def test_no_injection_when_no_messages(self, client):
        """无消息时不注入"""
        resp = client.get("/health")
        data = resp.json()
        assert "_user_supplement" not in data

    def test_no_injection_into_excluded_paths(self, client):
        """排除路径不注入"""
        um.add_message("不应出现")
        # /static 是排除路径
        resp = client.get("/static/index.html")
        # 即使返回 200/404，也不应有 _user_supplement
        if resp.headers.get("content-type", "").startswith("application/json"):
            assert "_user_supplement" not in resp.json()

    def test_injection_consumes_messages(self, client):
        """注入后消息被消费"""
        um.add_message("只出现一次")
        client.get("/health")  # 第一次注入
        resp = client.get("/health")  # 第二次不应有
        data = resp.json()
        assert "_user_supplement" not in data

    def test_inject_multiple_messages(self, client):
        """多条消息合并注入"""
        um.add_message("第一条")
        um.add_message("第二条")
        resp = client.get("/health")
        data = resp.json()
        supplement = data["_user_supplement"]
        assert "第一条" in supplement
        assert "第二条" in supplement


# ========== MCP 统计中间件 ==========

class TestMCpStatsMiddleware:
    """/mcp POST 请求的统计记录"""

    def test_mcp_post_records_stats(self, client, monkeypatch):
        """POST /mcp tools/call 触发统计记录"""
        recorded = []

        # ⚠ 形参必须跟随 server.core.middleware.record_call 的签名：
        # via_gateway 由 9dea394（2026-10-09 网关遥测拆分）加入，是带默认值的可选参数。
        # mock 少收这个形参会以 TypeError 炸掉——表现为"中间件坏了"的假红，实为测试没跟上实现。
        def fake_record_call(tool_name, duration_ms, success, via_gateway=False):
            recorded.append({
                "tool": tool_name,
                "duration_ms": duration_ms,
                "success": success,
                "via_gateway": via_gateway,
            })

        # middleware 模块在模块级定义 record_call（原 mcp_stats.py 已归并），
        # 直接 patch middleware 模块中的引用即可
        monkeypatch.setattr("server.core.middleware.record_call", fake_record_call)

        # 模拟 MCP JSON-RPC tools/call 请求
        client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "test_tool", "arguments": {}},
            "id": 1,
        })

        # 中间件应记录了调用（即使端点返回错误）
        assert len(recorded) == 1
        assert recorded[0]["tool"] == "test_tool"
        assert recorded[0]["success"] in [True, False]
        # 钉住「直连 tools/call 不得被标成网关转发」：test_tool 不在
        # _GATEWAY_TOOL_NAMES（只含 parcae_advanced_tool）内，故必为 False。
        assert recorded[0]["via_gateway"] is False

    def test_mcp_get_no_stats(self, client, monkeypatch):
        """GET /mcp 不记录统计"""
        recorded = []
        monkeypatch.setattr("server.core.middleware.record_call",
                           lambda *a, **kw: recorded.append(1))

        client.get("/mcp")
        assert len(recorded) == 0

    def test_non_mcp_path_no_stats(self, client, monkeypatch):
        """非 /mcp 路径不记录 MCP 统计"""
        recorded = []
        monkeypatch.setattr("server.core.middleware.record_call",
                           lambda *a, **kw: recorded.append(1))

        client.get("/health")
        client.post("/ocr/path/json", json={"path": "nonexistent.png"})
        assert len(recorded) == 0

    def test_mcp_non_tools_call_no_record(self, client, monkeypatch):
        """POST /mcp 但非 tools/call 方法不记录"""
        recorded = []
        monkeypatch.setattr("server.core.middleware.record_call",
                           lambda *a, **kw: recorded.append(1))

        client.post("/mcp", json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {},
            "id": 1,
        })
        assert len(recorded) == 0


# ========== 中间件集成 ==========

class TestMiddlewareIntegration:
    """中间件与实际端点的集成测试"""

    def test_health_works_with_middleware(self, client):
        """中间件不破坏正常请求"""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_supplement_does_not_break_response(self, client):
        """注入 _user_supplement 不破坏原有响应字段"""
        um.add_message("补充")
        resp = client.get("/health")
        data = resp.json()
        # 原有字段仍在
        assert data["status"] == "ok"
        # 补充字段也在
        assert "_user_supplement" in data

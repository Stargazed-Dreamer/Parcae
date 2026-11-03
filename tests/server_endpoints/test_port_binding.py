"""端口顺延与运行时声明的单测。

覆盖 2026-10-19 那次「后端一启动就关闭」暴露的三条链路：
1. uvicorn 之前预探测端口、按序顺延、整段跳过 Windows 保留区
2. 运行时端口声明（data/runtime/server_port.json）读写与状态机
3. 下游消费方配置的端口同步改写（只改匹配项、改前备份）

⛔ 这些用例**不绑定真实业务端口**（8766 可能被系统保留区占着），
一律用系统分配的临时端口或 monkeypatch，避免测试受本机环境状态影响。
"""

from __future__ import annotations

import json
import logging
import socket

import pytest

from lib import port_binding as pb
from lib import runtime_endpoint as rt

# ==================== 端口探测 ====================


def _free_port() -> int:
    """要一个当前可绑定的端口（绑 0 让系统分配，随即释放）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_bind_first_available_returns_preferred_when_free():
    port = _free_port()
    result = pb.bind_first_available("127.0.0.1", port)
    try:
        assert result.sock is not None
        assert result.port == port
        assert result.degraded is False
        assert result.reason == ""
    finally:
        if result.sock:
            result.sock.close()


def test_bind_first_available_shifts_when_preferred_taken():
    """首选端口被占用（10048）⇒ 顺延到下一个可用端口。"""
    port = _free_port()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        result = pb.bind_first_available("127.0.0.1", port, attempts=4)
        try:
            assert result.sock is not None, "应顺延成功"
            assert result.degraded is True
            assert result.port != port
            assert result.port > port
            assert "已被其他进程监听" in result.reason
        finally:
            if result.sock:
                result.sock.close()
    finally:
        blocker.close()


def test_bind_skips_whole_reserved_range(monkeypatch):
    """撞到 10013（保留区）⇒ 整段跳过，而不是逐 1 硬撞。

    本机实测保留段首尾相连到 1000+ 个端口：若跳段占配额，attempts=8 会在
    段中间耗尽 ⇒ 顺延永远是死路。这里用假段 [port, port+200] 验证跳段逻辑。
    """
    port = _free_port()
    fake_range = (port, port + 200)
    real_socket = socket.socket

    class _DeniedSocket:
        """前 N 次 bind 抛 10013，模拟保留区。"""

        def __init__(self, *a, **kw):
            self._s = real_socket(*a, **kw)
            self._denied = True

        def bind(self, addr):
            if self._denied and fake_range[0] <= addr[1] <= fake_range[1]:
                exc = OSError(13, "denied")
                exc.winerror = 10013  # type: ignore[attr-defined]
                raise exc
            return self._s.bind(addr)

        def __getattr__(self, name):
            return getattr(self._s, name)

        def close(self):
            self._s.close()

    monkeypatch.setattr(pb.socket, "socket", _DeniedSocket)
    monkeypatch.setattr(pb, "excluded_port_ranges", lambda: [fake_range])

    result = pb.bind_first_available("127.0.0.1", port, attempts=2)
    try:
        assert result.sock is not None, "应越过整段保留区后成功"
        assert result.port == fake_range[1] + 1
        # 跳段不该吃掉 attempts：只有真正的 bind 才计数
        skipped = [a for a in result.attempts if "整段跳过" in a.message]
        assert len(skipped) >= 1
    finally:
        if result.sock:
            result.sock.close()


def test_bind_returns_none_when_all_attempts_fail(monkeypatch):
    """全部失败时返回 None（调用方据此非 0 退出并打印诊断）。"""
    port = _free_port()

    def _always_denied(self, addr):
        exc = OSError(13, "denied")
        exc.winerror = 10013  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(socket.socket, "bind", _always_denied)
    monkeypatch.setattr(pb, "excluded_port_ranges", lambda: [])
    result = pb.bind_first_available("127.0.0.1", port, attempts=3)
    assert result.sock is None
    assert "10013" in result.failure_summary
    assert result.failure_summary


def test_access_denied_detects_posix_errno_too():
    """Windows 上 socket 错误是 errno=13 + winerror=10013，两个都要认。"""
    exc = OSError(13, "denied")
    assert pb._is_access_denied(exc) is True  # 仅 errno=13
    exc2 = OSError(13, "denied")
    exc2.winerror = 10013  # type: ignore[attr-defined]
    assert pb._is_access_denied(exc2) is True
    assert pb._is_access_denied(OSError(10048, "in use")) is False


def test_describe_error_mentions_netstat_trap():
    """报错文案必须点明「10013 不是端口被占用」，否则会误导排查方向。"""
    exc = OSError(13, "denied")
    exc.winerror = 10013  # type: ignore[attr-defined]
    msg = pb._describe_bind_error(exc, 8766)
    assert "10013" in msg
    assert "不是" in msg and "占用" in msg


# ==================== 运行时声明 ====================


@pytest.fixture()
def tmp_runtime(tmp_path, monkeypatch):
    """把声明/pid/登记表路径指到 tmp，避免污染真实 data/runtime。

    同时禁用"首次自动探测真实 harness 配置"：否则 register_consumer 触发的
    seed 会把 C:\\Users\\... 的真实路径写进 tmp 登记表，断言 len==1 立刻失败。
    """
    monkeypatch.setattr(rt, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(rt, "DECLARATION_PATH", tmp_path / "server_port.json")
    monkeypatch.setattr(rt, "PID_PATH", tmp_path / "server.pid")
    monkeypatch.setattr(rt, "CONSUMERS_PATH", tmp_path / "consumers.json")
    monkeypatch.setattr(rt, "_default_consumers", lambda: [])
    return tmp_path


def test_declaration_roundtrip(tmp_runtime):
    decl = rt.write_declaration(
        host="127.0.0.1",
        port=9808,
        preferred_port=8766,
        degraded=True,
        reason="reserved",
        pid=1234,
    )
    assert decl["base_url"] == "http://127.0.0.1:9808"
    assert (tmp_runtime / "server.pid").read_text(encoding="utf-8").strip() == "1234"

    loaded = rt.read_declaration()
    assert loaded is not None
    assert loaded["port"] == 9808
    assert loaded["preferred_port"] == 8766
    assert loaded["degraded"] is True
    assert loaded["status"] == rt.STATUS_BINDING


def test_declaration_status_transitions(tmp_runtime):
    rt.write_declaration(host="127.0.0.1", port=9808, preferred_port=8766, degraded=True)
    ready = rt.mark_declaration_status(rt.STATUS_READY)
    assert ready is not None and ready["status"] == rt.STATUS_READY
    rt.mark_declaration_status(rt.STATUS_STOPPED)
    # stopped 的声明不应该再被当成"可用后端"（否则客户端会死连已释放的端口）
    assert rt.declared_base_url() is None
    stopped = rt.read_declaration()
    assert stopped is not None and stopped["status"] == rt.STATUS_STOPPED


def test_declaration_corrupt_returns_none(tmp_runtime):
    (tmp_runtime / "server_port.json").write_text("{not json", encoding="utf-8")
    assert rt.read_declaration() is None
    assert rt.declared_base_url() is None


def test_clear_pid_file_is_idempotent(tmp_runtime):
    rt.write_declaration(host="127.0.0.1", port=9808, preferred_port=8766, degraded=True)
    rt.clear_pid_file()
    rt.clear_pid_file()  # 第二次不抛
    assert not (tmp_runtime / "server.pid").exists()


# ==================== 下游消费方登记与同步 ====================


def test_register_consumer_upserts(tmp_runtime, tmp_path):
    rt.register_consumer("butler", str(tmp_path / "butler.json"), note="管家前端")
    rt.register_consumer("butler", str(tmp_path / "butler2.json"), note="改路径")
    items = rt.load_consumers(seed_if_missing=False)
    assert len(items) == 1, "同 id 应覆盖而不是重复登记"
    assert items[0]["path"].endswith("butler2.json")


def test_rewrite_consumers_only_touches_matching_url(tmp_runtime, tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "a": {"url": "http://127.0.0.1:8766/mcp"},
                "b": {"url": "http://127.0.0.1:8767/mcp"},  # 别的服务，不能动
                "c": {"url": "http://127.0.0.1:19080/mcp"},
                "d": {"url": "http://example.com:8766/mcp"},  # 非回环，不能动
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    rt.register_consumer("wb", str(cfg))
    reports = rt.rewrite_consumers(8766, 9808)

    assert any(r.status == "rewritten" for r in reports)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["a"]["url"] == "http://127.0.0.1:9808/mcp"
    assert data["b"]["url"] == "http://127.0.0.1:8767/mcp"
    assert data["c"]["url"] == "http://127.0.0.1:19080/mcp"
    assert data["d"]["url"] == "http://example.com:8766/mcp"
    # 改前备份必须存在（IDE 配置被改坏时能一键还原）
    assert (tmp_path / "mcp.json.bak_portrewrite").exists()


def test_rewrite_consumers_no_match_leaves_file_untouched(tmp_runtime, tmp_path):
    cfg = tmp_path / "other.json"
    original = json.dumps({"url": "http://127.0.0.1:1234/mcp"})
    cfg.write_text(original, encoding="utf-8")
    rt.register_consumer("other", str(cfg))
    reports = rt.rewrite_consumers(8766, 9808)
    assert [r.status for r in reports] == ["skipped_no_match"]
    assert cfg.read_text(encoding="utf-8") == original
    assert not (tmp_path / "other.json.bak_portrewrite").exists(), "无匹配不该产生备份"


def test_rewrite_consumers_skips_disabled_and_missing(tmp_runtime, tmp_path):
    missing = tmp_path / "nope.json"
    disabled = tmp_path / "disabled.json"
    disabled.write_text('{"url":"http://127.0.0.1:8766/mcp"}', encoding="utf-8")
    rt.register_consumer("missing", str(missing))
    rt.register_consumer("disabled", str(disabled), auto_rewrite=False)

    reports = {r.consumer_id: r.status for r in rt.rewrite_consumers(8766, 9808)}
    assert reports["missing"] == "skipped_missing"
    assert reports["disabled"] == "skipped_disabled"
    assert "8766" in disabled.read_text(encoding="utf-8"), "禁用项必须原样保留"


def test_rewrite_consumers_same_port_is_noop(tmp_runtime, tmp_path):
    cfg = tmp_path / "x.json"
    cfg.write_text('{"url":"http://127.0.0.1:8766/mcp"}', encoding="utf-8")
    rt.register_consumer("x", str(cfg))
    assert rt.rewrite_consumers(8766, 8766) == []


# ---- 连续两次顺延：只按首选端口匹配会静默漏改下游（2026-10-20 事故回归）----


def test_plan_rewrite_sources_includes_prev_port_after_two_shifts():
    # 8766（首选）→ 9808（上次）→ 8908（本次）：文件里写的是 9808
    assert rt.plan_rewrite_sources(8766, 9808, 8908) == [8766, 9808]


def test_plan_rewrite_sources_dedups_and_ignores_self():
    assert rt.plan_rewrite_sources(8766, 8766, 8908) == [8766], "上次=首选时只试一次"
    assert rt.plan_rewrite_sources(8766, 8908, 8908) == [8766], "上次=本次时无需改写"
    assert rt.plan_rewrite_sources(8766, None, 8908) == [8766], "无历史声明时只试首选"


def test_consecutive_shift_actually_rewrites_downstream(tmp_runtime, tmp_path):
    """事故复盘：下游文件里是上次顺延的 9808，按首选端口 8766 匹配会 skipped_no_match。"""
    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"url":"http://127.0.0.1:9808/mcp"}', encoding="utf-8")
    rt.register_consumer("wb", str(cfg))

    assert [r.status for r in rt.rewrite_consumers(8766, 8908)] == ["skipped_no_match"], (
        "复现缺陷：只按首选端口匹配时漏改"
    )

    for src in rt.plan_rewrite_sources(8766, 9808, 8908):
        rt.rewrite_consumers(src, 8908)
    assert json.loads(cfg.read_text(encoding="utf-8"))["url"] == "http://127.0.0.1:8908/mcp"


# ==================== 端口环境体检（动态范围被改回） ====================


def test_port_range_status_clamps_end():
    st = pb.PortRangeStatus(start=49152, count=16384)
    assert st.end == 65535, "范围上界不得超过 65535"
    assert st.healthy


def test_low_dynamic_start_is_flagged(monkeypatch):
    """动态端口被压到低位 = WinNAT 只能从低位划保留块（本次事故根因）。"""
    monkeypatch.setattr(
        pb,
        "_reg_value",
        lambda name: {"DynamicPortRangeStartPort": 1024, "DynamicPortRangeNumberOfPorts": 13977}.get(name),
    )
    st = pb.inspect_dynamic_port_range()
    assert not st.healthy
    assert any("动态端口起点" in p for p in st.problems)
    assert st.start == 1024


def test_legacy_max_user_port_is_note_not_problem(monkeypatch):
    """MaxUserPort 被 dynamicport 覆盖 ⇒ 只记录不告警。

    证据：本机 `netsh show dynamicport tcp` 报 49152/16384，同时
    `MaxUserPort=15000` 仍在注册表 —— 两者并存说明它不参与端口分配。
    若把它计入 problems，`healthy` 会恒为 False、每次启动都 WARNING
    （2026-10-19 用户指出：既不影响，就不该持续点名）。
    """
    monkeypatch.setattr(
        pb,
        "_reg_value",
        lambda name: {"MaxUserPort": 15000}.get(name),
    )
    st = pb.inspect_dynamic_port_range()
    assert st.healthy, "MaxUserPort 不该让 healthy 变 False（否则等于永久告警）"
    assert not st.problems
    assert any("MaxUserPort=15000" in n for n in st.notes)
    # 缺省值不落注册表 ⇒ 没读到 DynamicPortRange* 时应视为默认 49152，不算问题
    assert st.start == 49152
    assert "notes" in st.as_dict(), "notes 必须对外可见（/runtime/port 靠它展示）"


def test_max_user_port_joins_low_start_as_hint(monkeypatch):
    """起点偏低时 MaxUserPort 只作同源线索，不额外抬高风险数。"""
    monkeypatch.setattr(
        pb,
        "_reg_value",
        lambda name: {
            "DynamicPortRangeStartPort": 1024,
            "DynamicPortRangeNumberOfPorts": 13977,
            "MaxUserPort": 15000,
        }.get(name),
    )
    st = pb.inspect_dynamic_port_range()
    assert not st.healthy
    assert len(st.problems) == 1, f"只应有一条风险（起点偏低），实际 {st.problems}"
    assert "动态端口起点" in st.problems[0]
    assert any("同源线索" in n for n in st.notes)


def test_log_does_not_warn_when_only_notes(monkeypatch, caplog):
    """只有 note 时启动日志不得出现 WARNING（避免狼来了）。"""
    monkeypatch.setattr(pb, "_reg_value", lambda name: {"MaxUserPort": 15000}.get(name))
    # 不查真实保留区：本机 8766 可能正被保留（那会额外产生一条真 WARNING，与本节无关）
    monkeypatch.setattr(pb, "excluded_port_ranges", lambda: [])
    with caplog.at_level(logging.DEBUG, logger="parcae.port"):
        pb.log_port_environment(preferred_port=8766)
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warns, [r.getMessage() for r in warns]
    assert any("MaxUserPort" in r.getMessage() for r in caplog.records), "note 仍应留在 DEBUG 里"


def test_healthy_when_defaults(monkeypatch):
    monkeypatch.setattr(pb, "_reg_value", lambda name: None)
    st = pb.inspect_dynamic_port_range()
    assert st.healthy, st.problems
    assert (st.start, st.count) == (pb.DEFAULT_DYNAMIC_START, pb.DEFAULT_DYNAMIC_COUNT)


# ==================== 端点解析优先级 ====================


def test_env_overrides_runtime_declaration(monkeypatch):
    """显式环境变量（server/exec.py 注入 PARCAE_API）优先级最高。"""
    monkeypatch.setenv("PARCAE_API", "127.0.0.1:9808")
    assert rt.resolve_base_url(probe_health=False) == "http://127.0.0.1:9808"


def test_env_alias_parcae_base(monkeypatch):
    """workspace 脚本长期用 PARCAE_BASE，保留兼容。"""
    monkeypatch.delenv("PARCAE_API", raising=False)
    monkeypatch.setenv("PARCAE_BASE", "http://127.0.0.1:9700/")
    assert rt.resolve_base_url(probe_health=False) == "http://127.0.0.1:9700"


# ==================== 版本号单一真源 ====================


def test_opencode_version_import_does_not_load_main():
    """⛔ 回归防线：opencode 取版本不得 import server.main。

    以 `python -m server.main` 启动时，server.main 名为 __main__，
    `from server.main import VERSION` 会把整个入口模块（建 app / 挂路由 / 挂 MCP）
    再执行一遍 —— 2026-10-19 实测日志里 MCP 挂载会打印两次。
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    code = (
        "import sys;"
        "from server.llm_pool.opencode import _user_agent;"
        "ua = _user_agent();"
        "assert 'server.main' not in sys.modules, 'opencode 不应 import server.main';"
        "print(ua)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert proc.stdout.strip().startswith("parcae/")

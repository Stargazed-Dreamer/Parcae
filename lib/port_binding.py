"""端口预探测与按序顺延（启动期绑定，Windows 端口保留区兜底）

共享层：后端（8766）与 butler runtime（18766）都走这里。本模块不 import server/client（ADR-0027）。


## 为什么不能在 uvicorn 内部兜底

uvicorn 的绑定失败发生在 **lifespan startup 之后**：它先跑完应用启动钩子
（日志里能看到「Parcae API 启动完成」），再 `bind()`；失败时异常被
uvicorn 自己吃掉、打一行 stderr 然后 `sys.exit(1)`。后果是：

- 我们的文件日志里**只有「启动完成 → 关闭清理」，一条报错都没有**
  （2026-10-19 实际踩过：只看 `data/logs/server.log` 会误判成"启动正常但莫名自杀"）
- 异常捕获不到 ⇒ 没法在失败时换端口重试

所以必须在 `uvicorn.run()` **之前**自己 bind。拿到已绑定的 socket 后通过
`uvicorn.run(sockets=[sock])` 交给它，既避免 TOCTOU 竞态（探测→释放→再绑，
中间端口可能被抢），也保证日志里的端口就是实际端口。

## 失败分类（两者的处置完全不同，别混）

| WinError | 含义 | 典型原因 |
|---|---|---|
| `10013` | 访问权限不允许 | 端口落在 WinNAT/Hyper-V 的保留区（**netstat 查不到**）、或被防火墙/权限拦 |
| `10048` | 地址已在使用 | 真有进程在 listen（旧实例没退干净） |

详见 `docs/environment-constraints.md`「端口保留区」章节。
"""

from __future__ import annotations

import errno
import socket
from dataclasses import dataclass, field

# Windows 错误码（socket.error.errno 在 Windows 上就是 WSA 码）
_WSAEACCES = 10013
_WSAEADDRINUSE = 10048

# 默认顺延尝试个数：preferred, preferred+1, ... preferred+7
DEFAULT_ATTEMPTS = 8


@dataclass
class PortAttempt:
    port: int
    ok: bool
    errno: int | None = None
    message: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"port": self.port, "ok": self.ok, "errno": self.errno, "message": self.message}


@dataclass
class BindResult:
    sock: socket.socket | None
    port: int
    preferred_port: int
    host: str
    attempts: list[PortAttempt] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.port != self.preferred_port

    @property
    def reason(self) -> str:
        """首选端口不可用的原因（degraded 时才有意义）。"""
        for att in self.attempts:
            if att.port == self.preferred_port and not att.ok:
                return att.message
        return ""

    @property
    def failure_summary(self) -> str:
        return "; ".join(f"{a.port}:{a.message}" for a in self.attempts if not a.ok)


def _is_access_denied(exc: OSError) -> bool:
    """是否是"端口被系统保留/无权限"类失败。

    ⛔ 必须同时看 errno 和 winerror：Python 在 Windows 上把 WSA 错误映射成
    POSIX errno ⇒ `e.errno == 13`（EACCES），而 `e.winerror == 10013`。
    只判 errno==10013 会永远不命中（2026-10-19 实测踩过）。
    """
    return getattr(exc, "winerror", None) == _WSAEACCES or exc.errno in (_WSAEACCES, errno.EACCES)


def _error_code(exc: OSError) -> int | None:
    """记录给日志看时优先用 WinError（用户能在网搜到），其次 errno。"""
    code = getattr(exc, "winerror", None)
    return int(code) if code else exc.errno


def _describe_bind_error(exc: OSError, port: int) -> str:
    """把 errno 翻译成可执行的中文结论（不要只丢原始报错）。"""
    code = _error_code(exc)
    if _is_access_denied(exc):
        return (
            f"端口 {port} 被拒绝绑定（WinError 10013 = 被系统保留区占用或权限不足）。"
            f"注意这**不是**端口被占用：netstat 查不到也会报这个。"
            f"排查：`netsh int ipv4 show excludedportrange protocol=tcp`"
        )
    if code == _WSAEADDRINUSE or code == errno.EADDRINUSE:
        return (
            f"端口 {port} 已被其他进程监听（WinError 10048）。"
            f"排查：`netstat -ano | findstr :{port}` 后结束旧实例"
        )
    return f"端口 {port} 绑定失败（errno={code}）: {exc}"


def _create_socket(host: str) -> socket.socket:
    family = socket.AF_INET6 if ":" in host and host not in ("0.0.0.0",) else socket.AF_INET
    # ⛔ 不要设 SO_REUSEADDR：Windows 上它允许两个 socket 绑同一端口，
    # 会让"端口冲突"彻底检测不出来（uvicorn 自己也不在 Windows 用）。
    return socket.socket(family, socket.SOCK_STREAM)


def excluded_port_ranges() -> list[tuple[int, int]]:
    """读 Windows 的 TCP 保留段（`netsh int ipv4 show excludedportrange`）。

    ⛔ 为什么必须读它、而不是"每次 +1 试"：本机实测保留段是 `8708–8807`、
    `8808–8907` … 一直连到 9800 以上（WinNAT 一次划走十几段）。逐 1 顺延
    会连撞几百个端口，`attempts=8` 永远跳不出去 —— 必须整段跳过。

    数字解析不依赖中文：输出是本地 OEM 代码页，但每行只有两列 ASCII 数字，
    按正则取即可（解码错误一律忽略）。查询失败返回 []，调用方退化为逐 1 顺延。
    """
    import re
    import subprocess

    try:
        proc = subprocess.run(
            ["netsh", "int", "ipv4", "show", "excludedportrange", "protocol=tcp"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    text = proc.stdout.decode("oem", errors="ignore")
    ranges: list[tuple[int, int]] = []
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s*\*?\s*$", line)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if 0 < start <= end <= 65535:
                ranges.append((start, end))
    return sorted(ranges)


def _range_covering(port: int, ranges: list[tuple[int, int]]) -> tuple[int, int] | None:
    for start, end in ranges:
        if start <= port <= end:
            return (start, end)
    return None


def bind_first_available(
    host: str,
    preferred_port: int,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
) -> BindResult:
    """从 preferred_port 起按序试绑，返回第一个成功的**已绑定** socket。

    遇到 WinError 10013（端口被系统保留）时**整段跳过**该保留区，而不是逐 1
    硬撞 —— 否则本机上千个连号保留端口会让顺延永远失败。

    全部失败时 `result.sock is None`，调用方必须打印 `failure_summary` 并非 0 退出。
    成功时 socket 已 bind 但未 listen —— 交给 uvicorn，它会 listen。
    """
    result = BindResult(sock=None, port=preferred_port, preferred_port=preferred_port, host=host)
    if attempts < 1:
        attempts = 1

    excluded: list[tuple[int, int]] | None = None  # 惰性查询：只在真的撞到 10013 时才问系统
    candidate = preferred_port
    bound_tries = 0  # 真正发起 bind 的次数（受 attempts 限制）
    skips = 0  # 整段跳过的次数（不占配额：跳段不做 syscall，代价为零；仅防死循环）

    while bound_tries < attempts and candidate <= 65535:
        # excluded 非 None 表示"已查过"（哪怕查出来是空列表，也不再重复调 netsh）
        if excluded is not None:
            covered = _range_covering(candidate, excluded)
            if covered:
                result.attempts.append(
                    PortAttempt(
                        port=candidate,
                        ok=False,
                        errno=_WSAEACCES,
                        message=(
                            f"端口 {candidate} 落在系统保留区 {covered[0]}-{covered[1]}，整段跳过"
                        ),
                    )
                )
                candidate = covered[1] + 1
                skips += 1
                # 本机实测保留段首尾相连（8708→9807 共 11 段），若跳段占配额，
                # attempts=8 会在段中间就用完 ⇒ 永远跳不出去。
                if skips > 200:  # 理论不可达；防御异常数据导致死循环
                    break
                continue

        sock = _create_socket(host)
        try:
            sock.bind((host, candidate))
        except OSError as exc:
            sock.close()
            result.attempts.append(
                PortAttempt(
                    port=candidate,
                    ok=False,
                    errno=_error_code(exc),
                    message=_describe_bind_error(exc, candidate),
                )
            )
            bound_tries += 1
            # 被保留 ⇒ 补查保留段，下一轮直接跳到段尾，不再逐 1 硬撞
            if _is_access_denied(exc) and excluded is None:
                excluded = excluded_port_ranges()
            candidate += 1
            continue

        # 端口 0 交给内核选一个空闲端口；对外必须报告实际端口而不是 0
        actual_port = candidate
        if candidate == 0:
            try:
                actual_port = int(sock.getsockname()[1])
            except (OSError, ValueError, IndexError):
                actual_port = candidate
        result.attempts.append(PortAttempt(port=actual_port, ok=True))
        result.sock = sock
        result.port = actual_port
        if preferred_port == 0:
            # 测试专用：port 0 时首选端口就是内核给的端口，不算降级
            result.preferred_port = actual_port
        return result

    return result


def diagnose_commands(port: int) -> list[str]:
    """端口不可绑时给用户复制的诊断/解堵命令（顺序即建议顺序）。"""
    return [
        f'netsh int ipv4 show excludedportrange protocol=tcp   # 看 {port} 是否在保留区',
        f'netstat -ano | findstr :{port}                       # 看是否真有进程占用',
        "net stop winnat  →  启动后端  →  net start winnat      # 仅当在保留区（需管理员，重启后复发）",
        "netsh int ipv4 set dynamicport tcp start=49152 num=16384  # 根治（需管理员+重启）",
    ]


# ==================== 端口环境体检（每次启动做一次） ====================

# Vista+ 的默认临时端口范围：49152–65535（16384 个）。
# 起点低于它就是被"网络优化/加速器"类工具改过的典型特征：动态范围压到低位后，
# WinNAT/Hyper-V 划分的保留块也会落在低位，正好撞上 8766 / 5037 这类常用端口。
DEFAULT_DYNAMIC_START = 49152
DEFAULT_DYNAMIC_COUNT = 16384

_TCPIP_PARAMS = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"


@dataclass
class PortRangeStatus:
    """当前机器的动态端口范围 + 已知风险点。

    `problems` 与 `notes` 是**两档**，不能混：

    - `problems`：真会让端口绑不上/更容易撞保留区的配置 ⇒ 启动时 WARNING、`healthy=False`
    - `notes`：能观察到但**当前不生效**的历史残留（如 MaxUserPort）⇒ 只记录、不告警

    分档原因（2026-10-19 用户指出）：MaxUserPort 在 Vista+ 已被 dynamicport 取代，
    本机 `netsh show dynamicport` 显示 49152/16384 就已证明它被忽略。若仍计入
    `problems`，则 `healthy` 恒为 False、每次启动都 WARNING —— 真问题（起点被压回
    低位）反而会被这条无关噪音稀释掉。
    """

    start: int
    count: int
    max_user_port: int | None = None
    reserved_ports: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def end(self) -> int:
        return min(65535, self.start + self.count - 1)

    @property
    def healthy(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start,
            "count": self.count,
            "end": self.end,
            "max_user_port": self.max_user_port,
            "reserved_ports": self.reserved_ports,
            "problems": self.problems,
            "notes": self.notes,
            "healthy": self.healthy,
        }


def _reg_value(name: str) -> object | None:
    if __import__("os").name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _TCPIP_PARAMS, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, name)
            return value
    except (OSError, ImportError):
        return None


def inspect_dynamic_port_range(*, preferred_port: int | None = None) -> PortRangeStatus:
    """读动态端口范围与 TCP 参数，列出会让端口绑定变脆的配置。

    ⚠️ Windows 对**默认值不落注册表**：`netsh int ipv4 set dynamicport tcp
    start=49152 num=16384` 之后 `DynamicPortRangeStartPort` 反而查不到（等于默认
    ⇒ 删值）。所以"注册表里没有这个值"是好消息，不能当异常。

    只读、不修改、不抛异常：启动路径上任何失败都必须降级成"无告警"。
    """
    start_raw = _reg_value("DynamicPortRangeStartPort")
    count_raw = _reg_value("DynamicPortRangeNumberOfPorts")
    max_user_raw = _reg_value("MaxUserPort")
    reserved_raw = _reg_value("ReservedPorts")

    start = int(start_raw) if isinstance(start_raw, int) and start_raw > 0 else DEFAULT_DYNAMIC_START
    count = int(count_raw) if isinstance(count_raw, int) and count_raw > 0 else DEFAULT_DYNAMIC_COUNT
    max_user = int(max_user_raw) if isinstance(max_user_raw, int) and max_user_raw > 0 else None
    reserved = [str(x) for x in reserved_raw] if isinstance(reserved_raw, list) else []

    status = PortRangeStatus(
        start=start,
        count=count,
        max_user_port=max_user,
        reserved_ports=reserved,
    )

    if start < DEFAULT_DYNAMIC_START:
        status.problems.append(
            f"动态端口起点是 {start}（默认应为 {DEFAULT_DYNAMIC_START}）："
            f"低位动态范围会让 WinNAT 的保留块落到常用端口上（8766 / 5037 等），"
            f"典型来源是「网络优化 / 游戏加速器」类工具。"
            f"修复：netsh int ipv4 set dynamicport tcp start={DEFAULT_DYNAMIC_START} "
            f"num={DEFAULT_DYNAMIC_COUNT}（需管理员，重启后生效）"
        )
    if max_user is not None and max_user < DEFAULT_DYNAMIC_START:
        # 只记 note：Vista+ 由 dynamicport 决定范围，实测本值不参与分配
        # （本机 netsh 报 49152/16384 与 MaxUserPort=15000 并存即证据）。
        detail = (
            f"注册表仍有 MaxUserPort={max_user}：XP 时代遗留优化项，"
            f"Vista+ 已由 dynamicport 接管（本机范围 {start}-{status.end}），"
            f"该值不参与端口分配、不影响绑定。"
        )
        if start < DEFAULT_DYNAMIC_START:
            detail += "它与上面的起点偏低通常出自同一批「网络优化」工具，可视为同源线索。"
        detail += (
            '想在注册表里彻底清掉：reg delete "HKLM\\SYSTEM\\CurrentControlSet'
            '\\Services\\Tcpip\\Parameters" /v MaxUserPort /f'
        )
        status.notes.append(detail)
    if preferred_port is not None:
        try:
            covered = _range_covering(preferred_port, excluded_port_ranges())
        except Exception:  # noqa: BLE001 - 体检不能影响启动
            covered = None
        if covered:
            status.problems.append(
                f"首选端口 {preferred_port} 当前落在系统保留区 {covered[0]}-{covered[1]}"
                f"（本次启动会自动顺延，但 IDE 的 MCP 配置需要同步）"
            )
    return status


def log_port_environment(preferred_port: int, logger: object | None = None) -> PortRangeStatus:
    """把端口环境体检结果写进日志（有问题才 WARNING，正常则 DEBUG）。

    2026-10-19 的教训：端口问题的报错原本只走 uvicorn 的 stderr，不进
    `data/logs/server.log`，排查时会误判成"启动正常但莫名退出"。所以任何
    与端口相关的风险都要落盘。
    """
    import logging

    log = logger if isinstance(logger, logging.Logger) else logging.getLogger("parcae.port")
    status = inspect_dynamic_port_range(preferred_port=preferred_port)
    if status.healthy:
        log.debug(
            "端口环境正常：dynamicport %d-%d，MaxUserPort=%s",
            status.start,
            status.end,
            status.max_user_port,
        )
    else:
        log.warning("端口环境存在 %d 个风险点（本次启动可能顺延或更易撞端口）：", len(status.problems))
        for item in status.problems:
            log.warning("  · %s", item)
    # notes 一律不告警：它们是"能看到但当前不生效"的历史残留。
    # 想看就去 GET /runtime/port 的 port_env.notes，别污染启动日志。
    for item in status.notes:
        log.debug("  · %s", item)
    return status

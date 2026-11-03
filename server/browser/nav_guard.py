"""同域导航冷却守卫：把「反复刷新 / 用完就关再重开」这层风控信号掐掉。

问题（2026-10-23 用户提出）
--------------------------
agent 与自动化脚本有个通病：页面不合预期就 `reload`，任务换一步就关 tab 再重开，
即使当前页明明可以复用。人不会 3 秒刷 5 次页，站点风控看到的恰恰是这种机器节奏。
本项目在指纹层是「不做加法」的（见 `docs/browser-anti-detection.md`），
唯一还能补的就是**行为节奏**——这也是本文档 §3 第 3 条一直要求的。

策略（用户拍板：高额惩罚）
--------------------------
- 计数对象：**刷新 / 初始连接**，即 `reload` 与 `goto`（含 `session_create(url=...)`
  新开 tab 的首次 goto）。`back` / `forward` 是页内历史导航，不计。
- 粒度：**同一域名**（按可注册域归并，`space.bilibili.com` 与 `www.bilibili.com`
  都记到 `bilibili.com`，换子域绕不过去）。
- 窗口：30 秒内只允许一次。**首次导航立即放行**（不是令牌桶，不会让第一个请求干等）。
- 违规：**硬拒绝**——不排队、不自动 sleep、不静默降级，直接返回
  `NAV_RATE_LIMITED` + `retry_after_secs`，由 agent 自己决定等或改用复用路径。
- 窗口恒定：**不随违规次数加码**。「连续违规满 3 次窗口翻倍（30→60→120s）」
  是初版设计，2026-10-23 用户裁决删除——计数器按**域**而不是按**调用者**，
  多个 agent 并发抢同一域时会把无辜者一起锁进去，而它们被拒又继续累加，
  形成「越罚越慢、越慢越难清零」的棘轮；真正该防的脚本死循环多在进程外
  （直连 CDP），进程内加码抓不到它，只会误伤守规矩的调用者。
- `violations` 降级为**纯观测**：进 `/browser/status` 与拒绝文案，用来判断
  「单个 agent 急躁重试」还是「多个 agent 抢同一个域」，**不参与窗口计算**。
- 豁免：`localhost` / 回环 / 内网 IP / `file:` / `about:` / `chrome:` 等本地目标。
  本地开发调试刷新是刚需，不属于风控面。
- **站点级豁免**：`[browser] nav_guard_exempt_hosts`（默认 `["scnu.edu.cn"]`，
  2026-10-23 用户点名）。命中规则：host 等于该项或以「.该项」结尾，
  所以 `moodle.scnu.edu.cn`、`sso.scnu.edu.cn` 一并覆盖。
  理由：砺儒云是「一次任务内必须连续切页」的场景（扫 7 门课 = 7 次 overview
  + SSO 登录链几跳），都是不同 URL 的正常业务导航，不是死循环刷同一页。

为什么放在后端进程内
--------------------
后端 8766 是常驻进程，`browser_navigate` 与 `browser_session_create` 都要过它，
所以状态放内存即可覆盖 MCP 与 REST 两条入口。绕过方式只剩「脚本直连 CDP 自己 goto」，
那属于自动化脚本层面的事，由 `docs/browser-anti-detection.md` §3 与
`.agents/skills/browser_lessons/SKILL.md` 的规范约束（本模块管不到进程外）。

与「站点级熔断冷却」的区别
--------------------------
`docs/browser-anti-detection.md` §4 里评估过「连续被风控后冷却 N 分钟」并判定不做
（人工驱动频率低、几乎不触发）。本模块不是那个：它不看站点返回什么，
只管**我们自己**的导航频率，对是否被风控无假设，属于纯行为层节流。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger("parcae.browser_nav_guard")

# 计入「刷新 / 初始连接」的动作。back/forward 不在内（页内历史导航，不算重新连接）。
THROTTLED_ACTIONS = frozenset({"reload", "goto"})

# 默认值与配置项名（config.toml 的 [browser] 段；缺省即用这些值）
DEFAULT_ENABLED = True
DEFAULT_WINDOW_SECS = 30.0
# 连续违规达到这个次数时，在拒绝文案里补一句「可能是别人在抢同一个域」。
# 只影响文案，不影响窗口——窗口恒定是 2026-10-23 用户裁决的。
VIOLATIONS_HINT_THRESHOLD = 2
# 状态表上限：域名数量有限，超过只可能是异常输入，按最久未用丢弃
_MAX_HOSTS = 512

_EXEMPT_SCHEMES = frozenset({
    "", "file", "data", "about", "blob", "chrome", "chrome-extension",
    "devtools", "view-source", "edge", "brave",
})
_EXEMPT_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".test")

# 站点级豁免（2026-10-23 用户点名加砺儒云）。
# 匹配规则：host 等于该项、或以「.<该项>」结尾 ⇒ `moodle.scnu.edu.cn` 与
# `sso.scnu.edu.cn` 都归到 `scnu.edu.cn`（与节流 key 的可注册域口径一致）。
#
# 为什么校内平台该豁免：节流防的是「机器节奏反复刷新」；而砺儒云是**一次任务内
# 必须连续切页**的场景——扫 7 门课 = 7 次 overview 跳转 + SSO 登录链 3~4 跳，
# 全是**不同 URL 的正常业务导航**，不是"页面不合预期就 reload"。
# 30s 一跳会把一轮全量扫拖成 3.5 分钟，且被拒后脚本还得自己 sleep，纯属空转。
# 真要防"死循环刷新"，靠的是调用方不重试，不是把正常业务卡住。
DEFAULT_EXEMPT_HOSTS: tuple[str, ...] = ("scnu.edu.cn",)

# 需要取三段的二级公共后缀。刻意不引 tldextract：它默认要联网拉后缀表，
# 离线/新机器上会退化，而这里只需要够用的近似（多归并一层比少归并一层安全）。
_SECOND_LEVEL_SUFFIXES = frozenset({
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "co.uk", "org.uk", "ac.uk", "gov.uk",
    "com.hk", "com.tw", "com.mo", "com.sg", "com.au", "com.br",
    "co.jp", "co.kr", "co.nz", "co.in", "com.my",
})


def _is_ipv4_literal(host: str) -> bool:
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _is_private_ipv4(host: str) -> bool:
    """内网/回环地址：本地服务调试，不参与风控节流。"""
    if not _is_ipv4_literal(host):
        return False
    a, b, *_ = (int(p) for p in host.split("."))
    return (
        a == 10
        or a == 127
        or (a == 192 and b == 168)
        or (a == 172 and 16 <= b <= 31)
        or (a == 169 and b == 254)
    )


def registrable_domain(host: str) -> str:
    """把 host 归并到可注册域（eTLD+1 近似），用作节流 key。

    >>> registrable_domain("space.bilibili.com")
    'bilibili.com'
    >>> registrable_domain("www.scnu.edu.cn")
    'scnu.edu.cn'
    """
    host = (host or "").strip().lower().strip(".")
    if not host:
        return ""
    if host.startswith("["):  # IPv6 字面量
        return host
    if ":" in host:  # 带端口时调用方应先剥掉；这里兜底
        host = host.split(":")[0]
    if _is_ipv4_literal(host) or host in _EXEMPT_HOSTS:
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _SECOND_LEVEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def split_url(url_or_host: str) -> tuple[str, str, str]:
    """返回 (scheme, host, throttle_key)。无协议时把入参当 host 处理。"""
    raw = (url_or_host or "").strip()
    if not raw:
        return "", "", ""
    if "://" in raw:
        try:
            parsed = urlparse(raw)
        except Exception:
            return "", "", ""
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
    else:
        scheme, host = "", raw.split("/")[0].split(":")[0].lower()
    return scheme, host, registrable_domain(host)


def is_exempt(scheme: str, host: str, extra_hosts: tuple[str, ...] = ()) -> bool:
    """本地/非网络目标 + 站点级豁免名单是否豁免。

    extra_hosts 是站点级豁免（可注册域或精确 host），命中规则：
    host == 项，或 host 以「.项」结尾。
    """
    if scheme in _EXEMPT_SCHEMES:
        return True
    if not host:
        return True
    if host in _EXEMPT_HOSTS or host.startswith("["):
        return True
    if host.endswith(_LOCAL_HOST_SUFFIXES):
        return True
    if _is_private_ipv4(host):
        return True
    host = host.lower().strip(".")
    for h in extra_hosts:
        h = (h or "").strip().lower().strip(".")
        if not h:
            continue
        if host == h or host.endswith("." + h):
            return True
    return False


@dataclass
class NavVerdict:
    """一次导航准入的判定结果。"""

    allowed: bool
    action: str = ""
    host: str = ""  # 归一后的主域（节流 key）
    raw_host: str = ""
    retry_after_secs: float = 0.0
    window_secs: float = 0.0
    violations: int = 0
    reason: str = "ok"  # ok / exempt / not_throttled / disabled / throttled
    # 2026-11-01 归因：本域额度是被谁用掉的。多 agent 并发抢同一个域时，
    # 光说"还需等 22s"无法区分「自己重试太急」还是「别人刚刷过」——
    # 这两件事实的对策完全相反（前者该停手，后者该去复用它已开着的 tab）。
    holder: str = ""  # 消耗额度的 caller（lib.agent_identity.get_caller_id）
    holder_session: str = ""  # 该次导航所属 session（无 session 时为空）
    holder_url: str = ""  # 该次导航的目标 URL
    holder_ago_secs: float = 0.0  # 距离那次导航过去了多久

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "host": self.host,
            "retry_after_secs": round(self.retry_after_secs, 1),
            "window_secs": round(self.window_secs, 1),
            "violations": self.violations,
            "reason": self.reason,
            "holder": self.holder,
            "holder_session": self.holder_session,
            "holder_url": self.holder_url,
            "holder_ago_secs": round(self.holder_ago_secs, 1),
        }


@dataclass
class _HostState:
    last_nav_monotonic: float = 0.0
    last_nav_wall: float = 0.0
    violations: int = 0
    last_reject_wall: float = 0.0
    hits: int = 0  # 累计放行次数，仅用于 prune 与诊断
    # 归因（2026-11-01）：谁用掉了本域这一份额度。
    # 只用于拒绝文案，不参与任何判定——窗口恒定是 2026-10-23 的裁决。
    last_nav_by: str = ""
    last_nav_session: str = ""
    last_nav_url: str = ""


class NavRateLimiter:
    """按主域计时的导航节流器（进程内单例，见 `get_nav_limiter`）。

    时间源可注入（`clock`），测试里用假时钟，不要 sleep。
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._states: dict[str, _HostState] = {}
        self._lock = threading.Lock()

    # ---- 内部 ----

    def _prune_if_needed(self) -> None:
        if len(self._states) <= _MAX_HOSTS:
            return
        # 按最近使用时间丢掉最旧的一批，保留热度最高的一半
        ordered = sorted(self._states.items(), key=lambda kv: kv[1].last_nav_monotonic)
        for key, _ in ordered[: len(ordered) // 2]:
            self._states.pop(key, None)

    # ---- 对外 ----

    def check(
        self,
        url_or_host: str,
        action: str,
        *,
        enabled: bool = DEFAULT_ENABLED,
        window_secs: float = DEFAULT_WINDOW_SECS,
        now: float | None = None,
        exempt_hosts: tuple[str, ...] = (),
        caller: str = "",
        session_id: str = "",
    ) -> NavVerdict:
        """判定一次导航是否放行；放行时顺带记账。

        caller / session_id 仅用于**归因**（拒绝时告诉对方"额度被谁用掉了"），
        不参与窗口判定——按域计时的语义与 2026-10-23 裁决完全一致。
        """
        action_l = (action or "").lower()
        scheme, host, key = split_url(url_or_host)
        if not enabled:
            return NavVerdict(True, action_l, key, host, reason="disabled")
        if action_l not in THROTTLED_ACTIONS:
            return NavVerdict(True, action_l, key, host, reason="not_throttled")
        if is_exempt(scheme, host, exempt_hosts):
            return NavVerdict(True, action_l, key, host, reason="exempt")

        t = self._clock() if now is None else now
        wall = time.time()
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = _HostState()
                self._states[key] = state
                self._prune_if_needed()
            window = float(window_secs)
            gap = t - state.last_nav_monotonic
            if state.last_nav_monotonic <= 0 or gap >= window:
                state.last_nav_monotonic = t
                state.last_nav_wall = wall
                state.violations = 0
                state.hits += 1
                state.last_nav_by = caller or ""
                state.last_nav_session = session_id or ""
                state.last_nav_url = (url_or_host or "")[:200]
                return NavVerdict(
                    True, action_l, key, host,
                    window_secs=window, reason="ok",
                    holder=state.last_nav_by,
                    holder_session=state.last_nav_session,
                    holder_url=state.last_nav_url,
                    holder_ago_secs=0.0,
                )
            # 违规：不推进 last_nav（不延长冷却），只累计违规次数供观测。
            # ⚠ 计数不再影响窗口（2026-10-23 删掉翻倍）：它按域统计，
            # 拿它调节惩罚会连带锁死同域的其他调用者。
            state.violations += 1
            state.last_reject_wall = wall
            retry = max(window - gap, 0.1)
            verdict = NavVerdict(
                False, action_l, key, host,
                retry_after_secs=retry,
                window_secs=window,
                violations=state.violations,
                reason="throttled",
                holder=state.last_nav_by,
                holder_session=state.last_nav_session,
                holder_url=state.last_nav_url,
                holder_ago_secs=max(gap, 0.0),
            )
            logger.warning(
                "NavGuard: 拒绝 %s %s（距上次 %.1fs < 窗口 %.0fs，连续违规 %d 次，剩余 %.1fs，"
                "额度持有者=%s/%s）",
                action_l, key, gap, window, state.violations, retry,
                state.last_nav_by or "?",
                state.last_nav_session[:8] if state.last_nav_session else "-",
            )
            return verdict

    def snapshot(self) -> list[dict]:
        """当前各域名的冷却状态（供 /browser/status 展示，让 agent 知道该等多久）。"""
        t = self._clock()
        wall = time.time()
        with self._lock:
            items = []
            for key, st in self._states.items():
                if st.last_nav_monotonic <= 0:
                    continue
                items.append({
                    "host": key,
                    "last_nav_ago_secs": round(t - st.last_nav_monotonic, 1),
                    "last_nav_at": round(st.last_nav_wall, 1),
                    "violations": st.violations,
                    "hits": st.hits,
                    "last_nav_by": st.last_nav_by,
                    "last_nav_session": st.last_nav_session,
                    "last_nav_url": st.last_nav_url,
                    "last_reject_ago_secs": (
                        round(wall - st.last_reject_wall, 1) if st.last_reject_wall else None
                    ),
                })
        return sorted(items, key=lambda i: i["last_nav_ago_secs"])

    def reset(self, host: str | None = None) -> None:
        """清空冷却状态（测试与人工运维用；正常路径不需要）。"""
        with self._lock:
            if host is None:
                self._states.clear()
                return
            _, _, key = split_url(host)
            self._states.pop(key or host, None)


# ---- 配置与单例 ----

def nav_guard_config() -> tuple[bool, float]:
    """读 [browser] 段的配置；读不到就用默认值（不因配置缺失关闭防护）。"""
    try:
        from ..config import get_browser_config

        cfg = get_browser_config()
        enabled = bool(cfg.get("nav_guard_enabled", DEFAULT_ENABLED))
        window = float(cfg.get("nav_guard_window_secs", DEFAULT_WINDOW_SECS))
        if window <= 0:
            enabled = False
        return enabled, window
    except Exception as e:  # 配置层异常不能变成"导航全挂"
        logger.debug("NavGuard: 读取配置失败，用默认值 %s", e)
        return DEFAULT_ENABLED, DEFAULT_WINDOW_SECS


_limiter: NavRateLimiter | None = None
_limiter_lock = threading.Lock()


def get_nav_limiter() -> NavRateLimiter:
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = NavRateLimiter()
    return _limiter


def nav_guard_exempt_hosts() -> tuple[str, ...]:
    """站点级豁免名单：config [browser] nav_guard_exempt_hosts + 内置默认。

    ⛔ 配置里显式给**空列表**＝「我不要默认豁免」（不是"没配就用默认"）。
    """
    try:
        from ..config import get_browser_config

        cfg = get_browser_config()
        raw = cfg.get("nav_guard_exempt_hosts", None)
    except Exception as e:  # 配置层异常不能变成"豁免全丢"
        logger.debug("NavGuard: 读取豁免名单失败，用内置默认 %s", e)
        return DEFAULT_EXEMPT_HOSTS
    if raw is None:
        return DEFAULT_EXEMPT_HOSTS
    if isinstance(raw, str):
        raw = [raw]
    return tuple(str(x).strip() for x in raw if str(x).strip())


def check_nav(
    url_or_host: str | None,
    action: str,
    *,
    caller: str = "",
    session_id: str = "",
) -> NavVerdict:
    """入口埋点专用：读配置 + 判定一次性完成。"""
    enabled, window = nav_guard_config()
    return get_nav_limiter().check(
        url_or_host or "", action,
        enabled=enabled, window_secs=window,
        exempt_hosts=nav_guard_exempt_hosts(),
        caller=caller, session_id=session_id,
    )


def nav_guard_status() -> dict:
    """给 /browser/status 用的紧凑快照。"""
    enabled, window = nav_guard_config()
    return {
        "enabled": enabled,
        "window_secs": window,
        "throttled_actions": sorted(THROTTLED_ACTIONS),
        "exempt_hosts": list(nav_guard_exempt_hosts()),
        # violations 只用于看「谁在撞」，不改变窗口（窗口恒定）
        "hosts": get_nav_limiter().snapshot(),
    }


def explain_verdict(v: NavVerdict) -> str:
    """把拒绝原因写成 agent 能直接照做的中文说明（会进 error.debug_detail）。

    2026-11-01 增补归因段：说清「这一份额度是谁、什么时候、用什么 URL 用掉的」。
    多 agent 抢同域时，agent 需要的不是"再等等"，而是"这是别人在刷，去复用它开着的 tab"。
    """
    if v.allowed:
        return ""
    holder_desc = ""
    if v.holder:
        who = v.holder
        if v.holder_session:
            who += f"（session {v.holder_session[:8]}…）"
        holder_desc = f"这一份额度是 {v.holder_ago_secs:.0f}s 前被 {who} 用掉的"
        if v.holder_url:
            holder_desc += f"，目标 {v.holder_url[:120]}"
        holder_desc += "。"
    else:
        holder_desc = (
            "本次导航来自本进程之外（直连 CDP 或无 caller 标识），无法归因到具体 agent。"
        )
    advice = (
        f"同一域名 {v.window_secs:.0f} 秒内只允许一次刷新/初始连接。"
        "⛔ 不要反复 reload 硬撞——正确做法是复用当前页面继续操作"
        "（browser_snapshot / browser_action 直接在现页面上做），"
        "或带 url_pattern 走 browser_session_create 复用已打开的 tab，"
        "而不是新开一个。"
    )
    extra = ""
    if v.violations >= VIOLATIONS_HINT_THRESHOLD:
        # 仅提示，不改变窗口：连续被拒通常意味着「不止一个调用者在用这个域」
        extra = (
            f" 本域已被连续拒绝 {v.violations} 次——如果你不是唯一在操作这个域的人，"
            f"很可能是别人刚刷过：直接带 url_pattern 复用他开着的 tab（他若仍持有，"
            f"409 就带 force=true 加入共享，别自己新开）。"
            f"等待时间不会因为重试变长，但重试也不会变短。"
        )
    return (
        f"域名 {v.host} 距上次刷新/初始连接不足 {v.window_secs:.0f}s，"
        f"还需等待约 {v.retry_after_secs:.0f}s。{holder_desc}{advice}{extra}"
    )


class NavRateLimited(ValueError):
    """导航被节流拒绝（继承 ValueError，老调用方按 ValueError 处理也不会崩）。"""

    def __init__(self, verdict: NavVerdict) -> None:
        self.verdict = verdict
        super().__init__(f"NAV_RATE_LIMITED: {explain_verdict(verdict)}")

    @property
    def retry_after_secs(self) -> float:
        return self.verdict.retry_after_secs


def enforce_nav(
    url: str | None,
    action: str,
    *,
    caller: str = "",
    session_id: str = "",
) -> NavVerdict:
    """非 HTTP 端点路径（如 SessionManager.create_session）用：不允许就抛。"""
    verdict = check_nav(url, action, caller=caller, session_id=session_id)
    if not verdict.allowed:
        raise NavRateLimited(verdict)
    return verdict

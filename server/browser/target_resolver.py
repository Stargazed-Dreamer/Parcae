"""冷路径目标消歧：没传 session_id / tab_id / url_pattern 时，别再"抓第一个 tab"。

G4（2026-11-01）
----------------
`playwright_executor._build_playwright_header()` 里的 `_find_page` 原先最后一句是
``return context.pages[0]``：**没指定目标就用第一个标签页**。这在单 agent 时代是
"操作浏览器"的合理默认值，在多 agent + 用户自己也开着页的现在变成了隐患：

- 谁排在第一就操作谁，调用方甚至不知道打到了哪个页面（响应里才有 url_after）；
- 命中的可能是**用户真实在看的 tab**，也可能是另一个 agent 的会话页；
- 出错的形态不是报错，而是"点了、填了、截了别人页面的图"——最难排查的一类。

本模块把这件事拦在**后端**（知道有哪些 tab、也知道谁持有），给调用方一份可照做的
候选清单；`_find_page` 侧再兜一道（多 tab 且无目标 ⇒ TAB_REQUIRED），双保险。

判定规则（仅当三个定位参数都没给时生效）
--------------------------------------
- 0 个 tab        ⇒ TAB_NOT_FOUND（先开一个）
- 1 个可信 tab    ⇒ **放行**，并把这个 tab_id 返回给调用方（见下"为什么要返回"）
  但若它正被某个 session 持有 ⇒ TAB_REQUIRED（那是别人的会话页，请用 session_id）
- ≥2 个可信 tab   ⇒ TAB_REQUIRED + 候选清单（含 tab_id / 标题 / URL / 持有者）

显式给了 tab_id 或 url_pattern 就完全走原逻辑，本模块不介入。

外来者不计入判定（2026-11-01 用户裁决）
--------------------------------------
``workspace/``、``tools/`` 下约 35 个脚本直连 CDP 不登记归属（见 ``tab_origins.py``）。
用户明确：脚本当**不受约束的外来者**——它们开的 tab 「就当我们没看见」，
不能因为"现在有 2 个 tab"就把 agent 卡在 TAB_REQUIRED 上。

所以歧义判定只看 ``origin != "unknown"`` 的 tab（session / preexisting）。
全部都是 unknown 时仍然拒绝（只有外来者的页面，绝不自动动手），但会说明原因。

为什么要返回 tab_id
-------------------
放行之后还有子进程 ``_find_page`` 这一层。它按**物理 tab 数量**判断（≥2 就报
TAB_REQUIRED），并不知道哪些是外来者——后端放行、子进程照样拦，就成了双重拦截。
把选定的 tab_id 交回调用方、由它塞进请求，子进程就走"精确匹配"分支，不再按数量拒绝。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .tab_origins import ORIGIN_PREEXISTING, ORIGIN_UNKNOWN

logger = logging.getLogger("parcae.browser_target_resolver")


@dataclass
class ColdTargetIssue:
    """冷路径目标不明确时的结构化说明（端点转成各自的 error 响应）。"""

    error_code: str
    detail: str
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_error(self, phase: str, elapsed_ms: int):
        """构造统一的 BrowserErrorResponse（延迟 import，避免循环依赖）。"""
        from .error_model import BROWSER_ERROR_CODES, BrowserErrorResponse

        return BrowserErrorResponse(
            error_code=self.error_code,
            error_message=BROWSER_ERROR_CODES.get(self.error_code, ""),
            phase=phase,
            debug_detail=self.detail,
            elapsed_ms=elapsed_ms,
        )


def _fmt_candidate(idx: int, cand: dict[str, Any]) -> str:
    tab_id = cand.get("tab_id") or "?"
    title = (cand.get("title") or "").strip() or "(无标题)"
    url = (cand.get("url") or "").strip() or "(空白页)"
    line = f"{idx}) tab_id={tab_id} | {title} | {url}"
    if cand.get("origin") == ORIGIN_PREEXISTING:
        line += "\n     · 观测开始时就存在（可能是你自己开的页，也可能是更早的脚本残留）"
    holders = cand.get("holders") or []
    if holders:
        who = "、".join(
            f"{h.get('session_id', '')[:8]}(owner={h.get('owner') or '?'})" for h in holders
        )
        line += f"\n      ⚠ 已被会话持有：{who} —— 那是别人的操作现场，请用该 session_id"
    return line


def decide(
    pages: list[dict[str, Any]],
    sessions: list[dict[str, Any]] | None = None,
) -> ColdTargetIssue | str | None:
    """纯判定（不碰网络，便于单测）。

    返回三种之一：
    - ``ColdTargetIssue``：拒绝，附带候选清单
    - ``str``：放行，值是**后端替你选定的 tab_id**（原本没指定目标时）
    - ``None``：没有 page 可判（理论上被 TAB_NOT_FOUND 覆盖，保留兼容）

    pages: [{tab_id, url, title, origin?}]；origin 缺省按"可信"处理（向后兼容）
    sessions: [{session_id, tab_id, owner}]（用于标注候选被谁持有）
    """
    sessions = sessions or []
    by_tab: dict[str, list[dict[str, Any]]] = {}
    for s in sessions:
        tid = s.get("tab_id") or ""
        if tid:
            by_tab.setdefault(tid, []).append(s)

    if not pages:
        return ColdTargetIssue(
            error_code="TAB_NOT_FOUND",
            detail=(
                "浏览器当前没有任何可操作的标签页（page target）。"
                "请先 browser_session_create(url='https://...') 开一个，"
                "或 browser_navigate(action='goto', url='https://...')。"
            ),
        )

    # 外来者（脚本直连 CDP 开的页）不参与歧义判定——用户裁决见模块 docstring
    trusted = [p for p in pages if (p.get("origin") or "") != ORIGIN_UNKNOWN]
    foreign_n = len(pages) - len(trusted)
    foreign_note = (
        f"\n（另有 {foreign_n} 个未知来源的标签页——可能是脚本开的——未计入本次判定）"
        if foreign_n else ""
    )

    if not trusted:
        return ColdTargetIssue(
            error_code="TAB_REQUIRED",
            detail=(
                f"浏览器当前只有 {len(pages)} 个**未知来源**的标签页，没有后端建的、"
                "也没有观测开始时就存在的页面。这些多半是脚本或外部进程开的，"
                "后端不自动操作它们（可能是别人的采集现场）。\n"
                "请二选一：\n"
                "  • browser_session_create(url='https://...') 自己开一个再操作\n"
                "  • 或显式传 tab_id 表示你确实要在这个外来页面上操作\n"
                + "\n".join(_fmt_candidate(i + 1, p) for i, p in enumerate(pages))
            ),
            candidates=list(pages),
        )

    if len(trusted) == 1:
        only = trusted[0]
        holders = by_tab.get(only.get("tab_id") or "", [])
        if not holders:
            # 唯一可信 tab 且无主：放行，并交回 tab_id 让子进程走精确匹配
            return only.get("tab_id") or ""
        who = "、".join(
            f"{h.get('session_id', '')[:8]}(owner={h.get('owner') or '?'})" for h in holders
        )
        return ColdTargetIssue(
            error_code="TAB_REQUIRED",
            detail=(
                f"浏览器只有一个可信标签页，但它正被会话 {who} 持有。"
                "冷路径（不传 session_id）不再默认操作别人的会话页——"
                "对方可能正在等某个元素或用着页面上的状态。"
                f"请改用该 session_id，或显式传 tab_id={only.get('tab_id', '')} 表示"
                "你确实要在这个页面上操作。\n"
                + _fmt_candidate(1, {**only, "holders": holders})
                + foreign_note
            ),
            candidates=[{**only, "holders": holders}],
        )

    cands = [
        {**p, "holders": by_tab.get(p.get("tab_id") or "", [])}
        for p in trusted
    ]
    listing = "\n".join(_fmt_candidate(i + 1, c) for i, c in enumerate(cands))
    return ColdTargetIssue(
        error_code="TAB_REQUIRED",
        detail=(
            f"未指定操作目标（没有 session_id / tab_id / url_pattern），而浏览器当前有 "
            f"{len(cands)} 个可信标签页。冷路径不再默认挑第一个——那会静默操作别人的 tab。"
            "请任选其一：\n"
            "  • browser_session_create(url_pattern='<域名>') 建立会话后传 session_id（推荐，热路径更快）\n"
            "  • 或直接传 tab_id 精确指定下面某一个\n"
            f"候选：\n{listing}{foreign_note}"
        ),
        candidates=cands,
    )


async def _list_pages_via_cdp() -> list[dict[str, Any]]:
    """CDP /json/list 取 page target（不依赖 Playwright 连接）。"""
    import httpx

    from ..config import get_browser_config

    port = get_browser_config()["debug_port"]
    async with httpx.AsyncClient(timeout=3) as client:
        resp = await client.get(f"http://127.0.0.1:{port}/json/list")
        targets = resp.json()
    return [
        {
            "tab_id": t.get("id", ""),
            "url": t.get("url", ""),
            "title": t.get("title", ""),
        }
        for t in targets
        if t.get("type") == "page"
    ]


async def check_cold_target(
    tab_id: str | None, url_pattern: str | None
) -> ColdTargetIssue | str | None:
    """冷路径入口调用：显式指定了目标就返回 None（不介入）。

    否则返回 ``str``（放行 + 后端选定的 tab_id）或 ``ColdTargetIssue``（拒绝）。
    """
    if tab_id or url_pattern:
        return None
    try:
        pages = await _list_pages_via_cdp()
    except Exception as e:  # 浏览器没起/端口不通：交回原路径（它会报 BROWSER_DISCONNECTED）
        logger.debug("check_cold_target: 取标签页列表失败，跳过消歧: %s", e)
        return None
    try:
        from .session.manager import get_session_manager

        mgr = await get_session_manager()
        sessions = [
            {"session_id": s["session_id"], "tab_id": s.get("tab_id", ""), "owner": s.get("owner", "")}
            for s in mgr.list_sessions()
        ]
    except Exception as e:
        logger.debug("check_cold_target: 取会话列表失败（按无主处理）: %s", e)
        sessions = []
    # 标注来源：脚本直连 CDP 开的页会在这里被打上 unknown，从而不参与歧义判定
    try:
        from .tab_origins import get_tab_origins

        pages = get_tab_origins().observe(pages, sessions)
    except Exception as e:
        logger.debug("check_cold_target: 来源标注失败（按无外来者处理）: %s", e)
    decision = decide(pages, sessions)
    if isinstance(decision, ColdTargetIssue):
        logger.info(
            "Browser: 冷路径目标不明确（%s，%d 个 tab），已拒绝默认兜底",
            decision.error_code, len(pages),
        )
    elif decision:
        logger.debug("Browser: 冷路径自动选定 tab_id=%s（其余为外来者/无主）", decision)
    return decision


async def cold_guard(
    tab_id: str | None, url_pattern: str | None, start: float | None = None,
    phase: str = "locate",
):
    """端点用的一步式入口。

    返回值三态，调用方必须区分处理：
    - ``BrowserErrorResponse``：拒绝，直接塞进响应体返回
    - ``str``：放行，但**没有指定目标**，后端替你选定了这个 tab_id（塞回请求走精确匹配）
    - ``None``：放行且无需干预（调用方自己给了 tab_id / url_pattern）

    start 传 `time.perf_counter()` 的起点，elapsed_ms 才算得准（不传就 0）。
    """
    decision = await check_cold_target(tab_id, url_pattern)
    if decision is None or isinstance(decision, str):
        if isinstance(decision, str):
            return decision
        return None
    elapsed_ms = 0
    if start is not None:
        import time as _time

        elapsed_ms = int((_time.perf_counter() - start) * 1000)
    return decision.to_error(phase, elapsed_ms)

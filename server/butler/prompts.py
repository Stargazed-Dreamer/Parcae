"""P5 T7：三份提示词渲染器（P-A 常驻 / P-B 外层 / P-C 检查点）。

P-A 只服务自建循环（本期不接线）；P-B/P-C 服务外部 harness。
"""

from __future__ import annotations

import os
import platform
import sys
from datetime import date
from typing import Any

PROMPT_VERSION = "p1-20261028"
TEMPLATE_VERSION = "pb-20261028.6"

PA_MAX_CHARS = 4_500
PC_MAX_CHARS = 300

_PA_IDENTITY = (
    "你是 Parcae 的「事务管家」（任务级执行者），只负责被委托的这一件事务；"
    "中枢管家负责分配与对用户沟通。不改管家配置、不碰密钥、不删除用户文件；"
    "能自己拍板的不要反复问，不可逆/花钱/对外发言先停下问。"
)
_PA_RED_LINES = (
    "密钥只经 lib/secret 读取，禁止硬编码或回显；删除走专用通道，不物理删；"
    "用户专用区不动；高风险动作先走审批；不得改测试来绕过闸门。"
)
_PA_TOOLS = (
    "默认入口：butler_session / butler_context / butler_wrapup / memory_*。"
    "同一轮多条互不依赖的读取，一次全部发出多个 tool_call；不要发一条等一条。"
    "结果按 ok/error_code 判断；截断看 truncated/next_cursor；不确定标 unknown。"
)
_PA_RISK = (
    "先问可逆性、爆炸半径、是否动共享状态。不可逆清单：删除、覆盖远端、发消息、"
    "改权限、动他人数据。不可逆或高影响动作必须先审批。"
)
_PA_STYLE = "简洁；引用文件用 path:line；不贴长文；不用 emoji。"
_PA_REPORT = (
    "每轮结束必须调 butler_wrapup(action=\"report\")：outcome + summary 必填，"
    "其余按需；报告不关闭会话，下一轮继续用同一码。"
)


def _environment_block() -> str:
    return (
        f"cwd={os.getcwd()}｜OS={platform.system()} {platform.release()}"
        f"｜python={sys.version.split()[0]}｜date={date.today().isoformat()}"
    )


def render_pa(*, environment: str | None = None,
              red_lines: str | None = None) -> dict[str, Any]:
    """P-A：7 段，总量 ≤4,500；裁剪顺序 环境→语气→风险→工具。"""
    segments: list[dict[str, Any]] = [
        {"name": "身份", "text": _PA_IDENTITY, "budget": 300, "trim": "none"},
        {"name": "硬红线", "text": red_lines or _PA_RED_LINES,
         "budget": 900, "trim": "none"},
        {"name": "工具使用", "text": _PA_TOOLS, "budget": 700, "trim": "short"},
        {"name": "风险判据", "text": _PA_RISK, "budget": 500, "trim": "half"},
        {"name": "输出与语气", "text": _PA_STYLE, "budget": 300, "trim": "drop"},
        {"name": "每轮报告义务", "text": _PA_REPORT, "budget": 400, "trim": "none"},
        {"name": "环境块", "text": environment if environment is not None
         else _environment_block(), "budget": 400, "trim": "clear"},
    ]
    truncated: list[str] = []

    def assemble() -> str:
        return "\n".join(f"【{s['name']}】\n{s['text']}" for s in segments)

    if len(assemble()) > PA_MAX_CHARS:
        for s in segments:
            if s["trim"] == "clear":
                s["text"] = "CLEARED"
                truncated.append(s["name"])
        if len(assemble()) > PA_MAX_CHARS:
            for s in segments:
                if s["trim"] == "drop":
                    s["text"] = ""
                    truncated.append(s["name"])
        if len(assemble()) > PA_MAX_CHARS:
            for s in segments:
                if s["trim"] == "half":
                    s["text"] = s["text"][:250]
                    truncated.append(s["name"])
        if len(assemble()) > PA_MAX_CHARS:
            for s in segments:
                if s["trim"] == "short":
                    s["text"] = s["text"][:350]
                    truncated.append(s["name"])
    text = assemble()
    return {
        "prompt": text,
        "chars": len(text),
        "truncated": sorted(set(truncated)),
        "prompt_version": PROMPT_VERSION,
    }


_PB_IDENTITY_FULL = (
    "你是本事务的执行者（事务管家），由中枢管家派单并与用户对话："
    "只处理这一件事务，跨轮保持上下文，如实回报。\n"
    "不改管家配置、不碰密钥、不删用户文件、不接别的事务；超出范围的请求先停下问清。"
)
_PB_IDENTITY_BRIEF = "你是本事务的执行者，只做这一件事并如实回报。"

_PB_WORK_FULL = (
    "只做完成判据覆盖的事。能自行决定的不要反复确认；不可逆、花钱、对外发言、删除须先问。\n"
    '出现阻塞、被纠正或范围变化时记一条：butler_session(action="note", code="{code}", text="...")\n'
    "不记录用户原话；必须逐字保留的用户决策走收尾的 quotes。"
)
_PB_WORK_BRIEF = (
    "只做完成判据覆盖的事。能自行决定的不要反复确认；不可逆、花钱、对外发言、删除须先问。\n"
    '留痕用 butler_session(action="note", code="{code}", text="...")'
)

_FIELD_TABLE = """report   | 必发 | outcome; summary(≤200字); next_run_hint; done 时 evidence; 首次报告带 harness/model
task     | 有变化时 | task_updates{progress; next_steps; current_state} —— 写的是**本路**状态（同一事务可能并行多路，各写各的）；title?/done_when? 只在首轮补全
memory   | 有事实要记时 | memory_candidates[{content; kind; scope; title; summary}]; quotes[{text}]
feedback | 召回无效时 | recall_feedback[{bundle; used; useful; note}]"""


def _pb_calls(code: str, pages: list[dict[str, Any]] | None,
              with_context: bool) -> str:
    """取上下文的调用块；不取上下文时返回空串。"""
    if not with_context:
        return ""
    ready_cursors = [
        str(p.get("cursor"))
        for p in (pages or [])
        if p.get("status") == "ready" and p.get("cursor")
    ]
    if ready_cursors:
        return "\n".join(
            f'butler_context(action="next", code="{code}", cursor="{c}")'
            for c in ready_cursors
        )
    return f'butler_session(action="open", code="{code}")'


def render_pb(*, code: str, task_title: str = "", done_when: str = "",
              harness: str = "", checkpoint_minutes: int = 10,
              user_request: str = "", pages: list[dict[str, Any]] | None = None,
              terse: bool = False, with_context: bool = True) -> dict[str, Any]:
    """P-B：外部 harness 的复制文本。

    两个独立开关，默认全开（2026-10-28 用户裁定：不设「档位」，只给开关）：
    - ``terse``：减少行为说明与字段说明。默认 False = 讲全；True = 只留最小闭环。
      它不是「另一种提示词」，是同一份模板少写解释。
    - ``with_context``：是否要求执行者开工时读入上一轮上下文（取页面）。

    2026-10-28 二次裁定：删去解释性废话（会话码绑定了什么、要不要换人、
    中途留言查询），交接（handoff）改由独立的交接提示词承担，不进首轮。
    """
    title = (task_title or "").strip()
    done = (done_when or "").strip()
    # 标题/判据缺不缺，生成提示词这边就知道，不要把这个判断推给 agent：
    # 缺就写明确的补全要求，不缺就整段不出现（2026-10-28 用户裁定）。
    # 前端未填标题时会存一个 "待命名 · ..." 占位，按前缀识别即可。
    title_missing = (not title) or title.startswith("待命名")
    done_missing = not done
    task_line = (
        f"【任务】{title if not title_missing else '（未命名）'}"
        f"｜完成判据：{done if not done_missing else '（未定）'}"
    )
    # 【页面】和【第一步】必须来自同一份页面清单，否则会出现「列了 4 页、只让取 3 页」
    # 或「让取一页根本没列出来」的矛盾（2026-10-28 用户发现 memory.must_read 就是这样漏出来的）。
    call_block = _pb_calls(code, pages, with_context)

    # 开工那一条要补什么，按**实际缺口**生成；什么都不缺就只报身份（2026-10-28 用户口径：
    # 不要写死「每次都要填」，系统自己知道缺什么）。
    fill_lines: list[str] = []
    if title_missing:
        fill_lines.append("   - 缺标题：用 task_updates.title 补一个 ≤20 字的短标题。")
    if done_missing:
        fill_lines.append(
            "   - 缺判据（可选）：能按原文确定就用 task_updates.done_when 补，定不出来留空。"
        )
    if not fill_lines:
        fill_lines.append("   - 标题与判据均已就绪，此条仅报身份。")
    fill_block = "\n".join(fill_lines)
    updates_arg = ", task_updates={...}" if (title_missing or done_missing) else ""
    identity_call = (
        f'butler_wrapup(action="task", code="{code}", '
        f'harness="<工具名>", model="<模型名>"{updates_arg})'
    )

    head = ("【开工前｜同一轮一次发完】" if terse
            else "【开工前｜同一轮一次发完，不要逐条等待】")
    if not with_context:
        head = "【开工前｜本轮不取上下文】"
    parts = [head]
    if with_context:
        parts.append(call_block)
    parts.append(identity_call)
    parts.append("   - harness / model 必填（标识本轮执行者），后续轮次不必重复。")
    if not terse or title_missing or done_missing:
        parts.append(fill_block)
    before = "\n".join(parts)

    if terse:
        template = f"""{_PB_IDENTITY_BRIEF}

{task_line}｜会话码 {code}
沿用此码：不新建 run、不自行换码；404 = 码失效，停止并原样回报。

{before}

【干活】
{_PB_WORK_BRIEF.format(code=code)}

【每轮收尾】
一轮结束或需用户回答时必发：
butler_wrapup(action="report", code="{code}", outcome="...", summary="...", next_run_hint="...")
done 时带 evidence；其余动作按需：task（任务变化）｜memory（有事实要记）｜feedback（召回无效）。
收尾不关闭会话，下一轮沿用同一码。"""
    else:
        template = f"""{_PB_IDENTITY_FULL}

{task_line}

【会话码】{code}
沿用此码：不新建 run、不自行换码。404 = 码失效，停止并原样回报。

{before}

【干活】
{_PB_WORK_FULL.format(code=code)}

【每轮收尾】
一轮结束或需用户回答时收尾一次。可一次交完，也可按项分开发送。

一次交完：
butler_wrapup(action="report", code="{code}", outcome="done", summary="...", evidence=["path:line"], next_run_hint="...", task_updates={{...}}, memory_candidates=[...], recall_feedback=[...])

按项分开发送（一条一件事）：
butler_wrapup(action="report", code="{code}", outcome="partial", summary="...", next_run_hint="...")
butler_wrapup(action="task", code="{code}", task_updates={{...}})
butler_wrapup(action="memory", code="{code}", memory_candidates=[...], quotes=[...])
butler_wrapup(action="feedback", code="{code}", recall_feedback=[...])

各动作收取的字段（竖线分隔；只写用得上的）：
{_FIELD_TABLE}

outcome：progress 在做 | partial 部分完成 | done 完成且有证据 | blocked 卡住（附 blocked_on_user）| failed 失败
kind：norm / pitfall / procedure / preference / reference / current_state
收尾不关闭会话，下一轮沿用同一码；未交 report 视为本轮未结束。"""

    user = (user_request or "").strip()
    user_block = f"【用户原文】\n{user}\n\n" if user else ""
    text = user_block + template
    return {
        "prompt": text,
        "chars": len(text),
        "template_chars": len(template),
        "user_request_chars": len(user),
        "template_version": TEMPLATE_VERSION,
        "terse": bool(terse),
        "with_context": bool(with_context),
        "harness": harness,
        "checkpoint_minutes": checkpoint_minutes,
    }


def render_handoff(*, code: str, task_title: str = "", done_when: str = "",
                   progress: int | None = None, next_steps: list[str] | None = None,
                   last_outcome: str = "", last_summary: str = "") -> dict[str, Any]:
    """交接指令：用户需要换人时自己复制，粘给当前的执行 agent。

    2026-10-28 用户裁定：交接不该写在首轮提示词里——首轮的执行者根本用不到它，
    写进去只是占上下文。改为「用户提出要交接 → 到面板复制这段 → 粘给 agent」。
    agent 拿到后只做一件事：落一条交接记录，然后停。

    ★ 2026-10-28 用户第三条指令：把 `.agents/skills/dev/handoff/SKILL.md` 的方法论
    融进这段文本。它是「怎么写一份别人能接手的稿」的既有规矩，与派单提示词同源。
    逐条落点：零上下文自检 / 不复制产物只给 path:line / 已完成要带验证方式 /
    脱敏 / 卡点必须是真卡点 / 反模式（空话）。
    """
    steps = "；".join(str(s) for s in (next_steps or [])[:5]) or "（未列，需在 reason 里写清）"
    pct = f"{progress}%" if progress is not None else "未报"
    state = f"{last_outcome or '未报'}：{last_summary or '（无摘要）'}"
    text = f"""【交接】会话码 {code}

现在开始交接，交给下一位执行者。只做以下三步，不再推进任务。

1) 本轮还没报告的话，先补一条：
butler_wrapup(action="report", code="{code}", outcome="partial", summary="...", next_run_hint="...")

2) 写交接记录，一次交完：
butler_wrapup(action="handoff", code="{code}", handoff={{"needed": true, "title": "<≤20 字>", "reason": "<卡在哪、缺什么>", "done": [{{"item": "<做完的事>", "evidence": "<命令，或 path:line + 结果>"}}], "next_steps": ["<下一任可直接执行的动作>"]}})

3) 到此结束。不再调用工具、不再干活，等用户安排下一位执行者。

写之前自查——读这份交接单的人没看过本对话：
- 每句都要脱离本对话能读懂：不写「上面」「刚才」「之前说的」，改成具体文件、命令、结论。
- 引用文件写 path:line 或 commit，不贴正文；要细节让对方自己打开原文。
- done 里每条都带验证方式（跑了什么、看到什么）；没验证的不要写进 done。
- reason 写真卡点（缺权限、缺决策、卡在外部条件）；写不出这一句，说明还没到该交接的时候。
- 不写「按上面的方案继续」「测试基本都过了」这类空话。
- 不写 key、token、密码、个人信息；需要密钥走 lib/secret，不落到交接单里。

当前状态：{task_title or '（未命名）'}｜判据 {done_when or '（未定）'}｜进度 {pct}｜上一轮 {state}
已知待办：{steps}"""
    return {
        "prompt": text, "chars": len(text),
        "template_version": TEMPLATE_VERSION, "variant": "handoff",
    }


def render_pb_continue(*, code: str, report_count: int = 1,
                       task_title: str = "", done_when: str = "",
                       progress: int | None = None,
                       next_steps: list[str] | None = None,
                       last_outcome: str = "", last_summary: str = "",
                       fresh_pages: list[str] | None = None) -> dict[str, Any]:
    """P-B 的「续跑」变体：同一个码、同一个执行者接着做。

    2026-10-28 用户裁定：只有真正接手（handoff）才需要完整提示词；有上下文续跑时，
    页面清单、身份要求、开工步骤都不该再占上下文——上一轮已经取过的东西不重复给。
    """
    steps = "；".join(str(s) for s in (next_steps or [])[:5]) or "（没有显式下一步，按完成判据自己推）"
    pct = f"{progress}%" if progress is not None else "未报"
    page = (fresh_pages or ["task/0"])[0]
    text = f"""【续跑】会话码 {code}，第 {report_count + 1} 轮；不新建 run、不换码。
上一轮：{last_outcome or '未报'}｜{last_summary or '（无摘要）'}
任务：{task_title or '（未命名）'}｜完成判据：{done_when or '（未定）'}｜进度：{pct}
待办：{steps}

上下文上一轮已给过，不重复取。需要某页再取，例如 {page}：
butler_context(action="next", code="{code}", cursor="{page}")

【收尾】每轮必发：
butler_wrapup(action="report", code="{code}", outcome="...", summary="...", next_run_hint="...")
done 时带 evidence；其余动作按需。收尾不关闭会话。"""
    return {"prompt": text, "chars": len(text),
            "template_version": TEMPLATE_VERSION, "variant": "continue"}


def render_takeover(*, code: str = "", task_title: str = "", done_when: str = "",
                    progress: int | None = None, reason: str = "",
                    done_items: list[dict[str, Any]] | None = None,
                    next_steps: list[str] | None = None,
                    last_outcome: str = "", last_summary: str = "",
                    created_at: str = "") -> dict[str, Any]:
    """接手说明：上一任写好交接之后，给下一个执行者看的**背景**。

    2026-10-28 用户裁定：交接与派单是两个独立系统，不耦合。
    这份只回答「这件事做到哪了、为什么换人、接下来该做什么」。

    2026-10-29 用户否决了「交接提示词不含会话码」：**必须含**。所以这里收
    `code`（新 run 的会话码）——它由面板「用这份交接开新一轮」时建出来，
    不在渲染阶段凭空生成。没有码就说明还没开新一轮，本函数不猜。

    ★ 老 skill（`.agents/skills/dev/handoff`）的**读方**规矩落在这里：
    「只信证据不信叙述」「交接稿与仓库冲突以仓库为准」「只给路径不复制正文」「已脱敏」。
    """
    steps = next_steps or []
    step_lines = (
        "\n".join(f"  {i}. {s}" for i, s in enumerate(steps[:6], 1))
        if steps else "  （上一任没有留下具体动作，按完成判据自己拆）"
    )
    done_lines = _format_done_items(done_items or [])
    pct = f"{progress}%" if progress is not None else "未报"
    stamp = created_at or "—"
    code_line = code or "（未分配：先在面板点「用这份交接开新一轮」）"
    text = f"""【交接】上一任执行者留下的交接单
会话码：{code_line}
任务：{task_title or '（未命名）'}
完成判据：{done_when or '（未定）'}
进度：{pct}
为什么交接：{reason or '（未填原因）'}
已完成（每条带验证方式，接手前自己核一遍）：
{done_lines}
接下来要做：
{step_lines}
上一轮结论：{last_outcome or '未报'}：{last_summary or '（无摘要）'}
交接时间：{stamp}

这是背景资料，不是派单指令：怎么做、每轮收尾报什么，按随后给你的派单提示词。
读法：① 「已完成」按它给的命令或 path:line 自己核一遍，交接单与仓库冲突时以仓库为准；
② 引用只给路径，不复制正文，要细节就打开原文；③ 交接单不含凭据，需要密钥走 lib/secret，不要向用户索要明文。"""
    return {
        "prompt": text, "chars": len(text),
        "template_version": TEMPLATE_VERSION, "variant": "takeover",
    }


def _format_done_items(items: list[Any]) -> str:
    """把交接单的 done 列表排成「做了什么 —— 验证：…」。

    兼容三种脏输入（agent 手写的 JSON 什么形状都可能）：纯字符串、缺 evidence 的 dict、
    以及 evidence 是列表的 dict。没写验证方式的条目要显式标出来，不能看起来像已核实。
    """
    lines: list[str] = []
    for raw in items[:8]:
        if isinstance(raw, dict):
            what = str(raw.get("item") or raw.get("what") or raw.get("text") or "").strip()
            ev = raw.get("evidence") or raw.get("verify") or ""
            if isinstance(ev, (list, tuple)):
                ev = "；".join(str(x) for x in ev)
            ev = str(ev).strip()
        else:
            what, ev = str(raw).strip(), ""
        if not what:
            continue
        lines.append(f"  {len(lines) + 1}. {what} —— 验证：{ev or '（上一任未给验证方式，需自己核）'}")
    return "\n".join(lines) if lines else "  （上一任没有列已完成项，按完成判据自己核）"


def render_pc(*, code: str, report_count: int = 0) -> dict[str, Any]:
    """P-C：检查点消息，≤300 字符。"""
    text = (
        f"【检查点】这一轮还没收尾（已收尾 {report_count} 轮）。先交 report："
        f"butler_wrapup(action=\"report\", code=\"{code}\", outcome=\"...\", summary=\"...\")"
        "（其余按需）。收尾不关闭会话，下一轮继续用同一个码。"
    )
    if len(text) > PC_MAX_CHARS:
        text = text[:PC_MAX_CHARS]
    return {"prompt": text, "chars": len(text), "template_version": TEMPLATE_VERSION}

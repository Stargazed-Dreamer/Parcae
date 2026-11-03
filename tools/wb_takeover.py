"""列出「因限流/报错被打断、值得接手」的 WorkBuddy 会话，并导出可交接的接手包。

背景：WorkBuddy 账号会被限流（`429 您的使用量已超出频率限制`），一场长任务因此断在半路。
换号之后新会话的上下文是空的，需要①快速找出「哪几场被截断了、断在哪」②把某场导出成
新 agent 自己读完就懂的材料。本脚本干这两件事。

用法：
    uv run python -m tools.wb_takeover scan                     # 近 24h，按最后活动倒序
    uv run python -m tools.wb_takeover scan --hours 24 --only 429
    uv run python -m tools.wb_takeover scan --all               # 不看时间窗（全库，约 12s）
    uv run python -m tools.wb_takeover export --pick 1,3        # 导 scan 编号 1 和 3
    uv run python -m tools.wb_takeover export 6f4880fe --note "断在解析脚本"
    uv run python -m tools.wb_takeover accounts                 # 看账号与手机号登记
    uv run python -m tools.wb_takeover accounts --set 138xxxx=988245f8

产物：
    temp/wb_takeover/<日期>/<会话8位>/
        HANDOFF.md     事实头（元数据 / 尾部原文 / 改动过的文件）
        chat_full.md   全量对话（U=用户 A=agent，编号=全量条目序号）
        source.jsonl   原始事件流拷贝（沙箱不许读家目录时也读得到）
        PROMPT.md      派发提示词（贴给接手的 agent）
    temp/wb_takeover/last_scan.json  最近一次 scan 的编号表（供 export --pick）

⛔ 判读铁律（别让脚本替你下结论）：
  本脚本只输出**事实与原文**（谁在何时说了什么、收尾是哪条错误），不输出「这场该不该接手」
  这种判断。`end` 只是对**最后一条 assistant 消息的形态分类**，不是结论——一场 `completed`
  的会话完全可能活没干完，一场 `user-interrupt` 也可能是用户自己想停。要不要接、接什么，
  由调用方读完 `末用户 / 末agent / 末工具` 自己判断。

实现要点（踩过的坑，别自作聪明改）：
  1. 收尾原因**只能从 jsonl 的尾条判断，不能看 sessions.status**：实测 19 场非正常收尾里
     只有 4 场 status='error'，其余散在 archived / 已删除。DB 的 status 是 UI 状态不是运行结果。
  2. 错误横幅**必须从头锚定**：真实的 429 横幅就是整条 assistant 消息的全部内容
     （`^429 您的使用量…`）。曾用「子串包含」判定，把**上一场 agent 正文里引用过那句话**的
     会话也判成限流（本机实测 44f8ef91 就是这么被误判的）。⇒ 一律 `re.match` 从行首先锚。
  3. 用户话术要剥注入块（`<system-reminder>` / `<cb_summary>` 等），直接复用
     `tools.wb_history._clean`——那 9 个坑已经在那儿踩完了，别另写一份。
  4. 全库扫 1.4 GB 约 12s，所以默认按 `last_activity_at` 时间窗在 SQL 里预筛（几毫秒），
     只解析命中的少数 jsonl。`--all` 才全量。
  5. 沙箱会拦命令行里的家目录路径（`~/.workbuddy/...`）⇒ 路径一律在脚本内拼，export 还会
     把 jsonl 复制进包内，接手方不必碰家目录。
  6. 手机号 ↔ user_id 无法从库里反查（`~/.workbuddy/*.json` 里没有 account/phone 字段）。
     映射只落 `data/wb_accounts.json`（`.gitignore:27` 的 `data/*`，不入库）；**skill 与日志
     正文都不写明文手机号**。没有映射也照常工作——「当前号」退化为「最近还在动的那个号」。
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# 复用 wb_history 的清洗与工具调用抽取：注入块剥离、cb_summary 摘要块、function_call 解析
# 都在那边，复制一份必然两处漂移（见 wb_history.py 文件头「实现要点」）。
from tools.wb_history import (
    DB_PATH,
    PROJECTS_DIR,
    _clean,
    _collect_files,
    _iter_messages,
    _iter_tool_calls,
)

OUT_ROOT = Path("temp") / "wb_takeover"
ACCOUNTS_FILE = Path("data") / "wb_accounts.json"
LAST_SCAN = OUT_ROOT / "last_scan.json"

# ---------- 收尾形态：一律从行首锚定（坑 2） ----------
_RE_429 = re.compile(r"^\s*429\s+您的使用量已超出频率限制[^\n]*?将在\s*(?P<reset>[^\n，,。]+?)\s*重置")
_RE_429_QUEUE = re.compile(r"^\s*429\s+queue\.")
_RE_429_ANY = re.compile(r"^\s*429\b")
_RE_401 = re.compile(r"^\s*401\b")
_RE_5XX = re.compile(r"^\s*5\d\d\b")
_RE_USER_INT = re.compile(r"^\s*Interrupted by user")
# 「不是正常工作内容」的横幅：找 last_real_asst 时要跳过
_RE_BANNER = re.compile(r"^\s*(4\d\d|5\d\d)\b|^\s*(API Error|Error from provider)", re.I)

# 「把球踢回给用户」的收尾话术，分两类：
#   ASK  = 问句型（要不要我 / 需要我 / 是否要…）。⛔ 光有词不算，得是**同一句里以问号收尾**，
#          否则「…（街拍路人是否要更严…）。B0047 起可直接抢」这种引用会被误判（实测漏过一个）。
#   PASS = 陈述型（你说一声 / 等你上传 / 你叫我 / 你确认…）。这类把动作直接指派给你，不用问号。
_RE_ASK = re.compile(
    r"(要不要|需要我|要我|是否(要|需要|继续|由)|还需不需要|可以吗|行吗|好不好|"
    r"shall I|want me to|would you like)",
    re.I,
)
_RE_PASS = re.compile(
    r"(你说一声|你说下|你说一下|你说清|你说就|你叫我|等你|你(先)?确认|你(来)?定|"
    r"由你(决定|定|拍)|你按|你看着办|你那边|你上传|记得上传|别忘了|请确认|let me know)",
    re.I,
)

END_LABELS = {
    "429": "限流中断",
    "429-queue": "排队未完成",
    "429-other": "429(其他)",
    "401": "鉴权失败",
    "5xx": "上游不可用",
    "user-interrupt": "用户中断",
    "completed": "正常结束",
    "incomplete": "未完成(其他)",
    "no-reply": "用户发了但无回应",
    "empty": "无对话内容",
}


def _ts(v: object) -> str:
    if isinstance(v, (int, float)) and v:
        return datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d %H:%M")
    return "-"


def _mask(phone: str) -> str:
    """手机号只露前 3 后 4：够区分账号，又不把明文留在终端/日志/截图里。"""
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 8 else "***"


def _one_line(s: str, n: int) -> str:
    return " ".join(s.split())[:n]


def _arg_brief(args: dict, n: int = 90) -> str:
    """工具调用的关键参数，一句话。"""
    for key in ("command", "file_path", "path", "pattern", "url", "query"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return _one_line(v, n)
    return ""


def _clip_arg(name: str, args: dict) -> str:
    """一句话说清最后一次工具调用在干什么。"""
    brief = _arg_brief(args, 70)
    return f"{name}: {brief}" if brief else name


def _sentence_at(seg: str, pos: int) -> str:
    """截 pos 所在那一句：从上一个句号/分号/冒号之后起，到下一个句末标点为止。"""
    start = max(seg.rfind("。", 0, pos), seg.rfind("；", 0, pos), seg.rfind("：", 0, pos)) + 1
    rest = seg[start:]
    end = len(rest)
    for ch in "。？！?!":
        i = rest.find(ch)
        if 0 <= i < end:
            end = i + 1
    return " ".join(rest[:end].split())[:140]


def _handoff_sentence(text: str) -> str:
    """末条 agent 话是否在「把下一步交回给用户」。命中返回那句话，否则空串。

    只认**收尾处**：正文中间提一句「要不要」不算。整条以问号结束，或最后 600 字里
    有陈述型指派，或问句型且同一句以问号收尾。代码块先剥掉——里面 `if 你要不要`
    之类会把整场判歪。
    """
    if not text:
        return ""
    tail = re.sub(r"```.*?```", " ", text[-1500:], flags=re.S)
    tail = " ".join(tail.split())
    if not tail:
        return ""
    if tail.rstrip().endswith(("？", "?")):
        return _sentence_at(tail, len(tail) - 1)
    seg = tail[-600:]
    cands: list[tuple[int, str]] = []
    for m in _RE_PASS.finditer(seg):
        cands.append((m.start(), _sentence_at(seg, m.start())))
    for m in _RE_ASK.finditer(seg):
        rest = seg[m.start():]
        qs = [i for i in (rest.find("？"), rest.find("?")) if i >= 0]
        if not qs:
            continue                      # 后面根本没问号 ⇒ 不是问句
        if rest.find("。", 0, min(qs)) >= 0:
            continue                      # 同句里先出现句号 ⇒ 已经收尾了，问号在下一句
        cands.append((m.start(), _sentence_at(seg, m.start())))
    if not cands:
        return ""
    # 取**最靠后**的那个：`## 两件需要你确认的 … 要不要你重启后端？` 这种小标题里就有
    # 「你确认」，取第一个会截成标题，真正的球在最后一句。
    return max(cands, key=lambda x: x[0])[1]


def _classify(text: str, status: str | None) -> tuple[str, str]:
    """对最后一条 assistant 消息分型。返回 (code, 429重置时间)。"""
    m = _RE_429.match(text)
    if m:
        return "429", m.group("reset")
    if _RE_429_QUEUE.match(text):
        return "429-queue", ""
    if _RE_429_ANY.match(text):
        return "429-other", ""
    if _RE_401.match(text):
        return "401", ""
    if _RE_5XX.match(text):
        return "5xx", ""
    if _RE_USER_INT.match(text):
        return "user-interrupt", ""
    return ("completed" if status == "completed" else "incomplete"), ""


@dataclass
class Tail:
    """一场会话的尾部事实。字段全是观测，不含判断。"""

    n_msg: int = 0
    n_call: int = 0
    n_429: int = 0
    a429_at: list = field(default_factory=list)  # 整场 429 的时间点
    last_user: str = ""
    last_user_at: object = None
    last_asst: str = ""
    last_asst_st: str | None = None
    last_real_asst: str = ""
    last_tool: str = ""
    end: str = "empty"
    end_reset: str = ""
    handoff: str = ""  # 末条 agent 话里「把球踢回给用户」的那句（空=没踢）

    @property
    def end_label(self) -> str:
        return END_LABELS.get(self.end, self.end)

    @property
    def is_done(self) -> bool:
        """看着正常收尾、也没在等回话——只是「agent 说完了」，不等于活干完了。"""
        return self.end == "completed" and not self.handoff


def _read_tail(path: Path) -> Tail:
    """单遍读完：最后一条用户话、最后一条 agent 话、最后一个工具调用、收尾形态。"""
    t = Tail()
    last_role = ""
    for role, ts, raw in _iter_messages(path):
        text = _clean(raw) if role == "user" else raw.strip()
        if not text:
            continue
        t.n_msg += 1
        last_role = role
        if role == "user":
            t.last_user, t.last_user_at = text, ts
        else:
            t.last_asst = text
            if _RE_429_ANY.match(text):
                t.n_429 += 1
                t.a429_at.append(ts)
            if not _RE_BANNER.match(text):
                t.last_real_asst = text
    t.last_asst_st = _last_assistant_status(path)
    for _, name, args in _iter_tool_calls(path):
        t.n_call += 1
        t.last_tool = _clip_arg(name, args)

    if last_role == "user" and t.last_user:
        t.end = "no-reply"
    elif t.last_asst:
        t.end, t.end_reset = _classify(t.last_asst, t.last_asst_st)
    # 只在「不是被系统打断」时才看有没有踢球：429/401/中断那几条的 last_real_asst
    # 是打断前的最后一句真话，那句话可能正好是「要不要我…」——但这场显然不是等你回话。
    if t.end in ("completed", "incomplete"):
        t.handoff = _handoff_sentence(t.last_real_asst)
    return t


def _last_assistant_status(path: Path) -> str | None:
    """最后一条非空 assistant 消息的 status（completed / incomplete）。

    `_iter_messages` 不带 status，而 `incomplete` 是「被截断」的另一个独立信号，
    值得多扫一遍——只在被 scan 命中的少数文件上跑，成本可忽略。
    """
    st = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "message" or d.get("role") != "assistant":
                continue
            parts = [c.get("text") or "" for c in (d.get("content") or [])
                     if isinstance(c, dict) and c.get("type") in ("input_text", "text", "output_text")]
            if any(p.strip() for p in parts):
                st = d.get("status")
    return st


# ---------------------------------------------------------------- 索引 / 账号

def _jsonl_index(root: Path | None = None) -> dict[str, Path]:
    """sid8 → jsonl 路径，只建一次（370 个文件 glob 一次，比每行 glob 一次省事）。"""
    root = root or PROJECTS_DIR
    idx: dict[str, Path] = {}
    for p in root.glob("*/*.jsonl"):
        idx.setdefault(p.stem[:8], p)
    return idx


def _load_accounts() -> dict[str, str]:
    if not ACCOUNTS_FILE.exists():
        return {}
    try:
        d = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}


def _resolve_account(spec: str, uids: list[str]) -> str | None:
    """手机号或 uuid 前缀 → 完整 user_id。解析不了返回 None（不猜）。"""
    if not spec:
        return None
    s = spec.strip()
    if s.isdigit():
        s = _load_accounts().get(s, "")
        if not s:
            return None
    hits = [u for u in uids if u.startswith(s)]
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------- scan

def _is_hard_break(end: str) -> bool:
    """系统/网络把你打断，不是你自己按停的。只用于分型（决定进第一段还是第三段）。

    别拿它去判「该不该列」：归档与否只看 `sessions.status`，见 `cmd_scan` 里的过滤。
    """
    return end.startswith("429") or end in ("401", "5xx", "no-reply", "empty", "incomplete")


def _query_rows(args: argparse.Namespace) -> list[dict]:
    if not DB_PATH.exists():
        raise SystemExit(f"[ERROR] 找不到 {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    where = ["1=1"]
    params: list[object] = []
    if not args.include_deleted:
        where.append("s.deleted_at IS NULL")
    if not args.all:
        cutoff = int((datetime.now().timestamp() - args.hours * 3600) * 1000)
        where.append("COALESCE(s.last_activity_at, s.updated_at) >= ?")
        params.append(cutoff)
    sql = f"""
        SELECT s.id, s.user_id, COALESCE(s.custom_title, s.title) AS title, s.status, s.cwd,
               s.created_at, COALESCE(s.last_activity_at, s.updated_at) AS act,
               s.is_playground, s.is_background_automation, s.mode, s.model,
               u.used, u.size
        FROM sessions s LEFT JOIN session_usage u ON u.session_id = s.id
        WHERE {' AND '.join(where)}
        ORDER BY act DESC
    """
    return [dict(r) for r in conn.execute(sql, tuple(params))]


def cmd_scan(args: argparse.Namespace) -> int:
    rows = _query_rows(args)
    uids = sorted({r["user_id"] for r in rows if r["user_id"]})
    accounts = _load_accounts()
    phone_of = {v: k for k, v in accounts.items()}

    # 当前号：显式指定优先；否则取「最近还在动的那个号」，并明说是推断
    current, source = None, ""
    if args.account:
        current = _resolve_account(args.account, uids)
        source = "指定"
        if not current:
            print(f"[WARN] --account {args.account} 解析不了（手机号没登记就 accounts --set，"
                  f"或直接给 uuid 前缀）；本次退回推断", file=sys.stderr)
    if not current and rows:
        current, source = rows[0]["user_id"], "推断=最近活动的号"

    index = _jsonl_index()
    picked: list[dict] = []
    n_arch = 0
    for r in rows:
        path = index.get(r["id"][:8])
        if path is None:
            continue
        tail = _read_tail(path)
        if args.only:
            v = args.only
            if v == "429":
                if not tail.end.startswith("429"):
                    continue
            elif v == "pending":          # 正常收尾但末句在等你回话
                if not tail.handoff:
                    continue
            elif v == "done":             # 真收尾了（completed 且没踢球）
                if not tail.is_done:
                    continue
            elif tail.end != v:
                continue
        if args.skip_current and current and r["user_id"] == current and r["status"] == "working":
            continue
        # 归档 = 用户明确不再理会的，一律不列（连 429 也不列）。
        # 用户 2026-11-01 原话：「归档确实不是活干完了，但是是我明确不再理会的，你不用自作多情再掏出来问我。
        # 大概率都是有人接手或者情况有变，归档一定是我明确不再管的东西。」
        if r["status"] == "archived" and not args.include_archived:
            n_arch += 1
            continue
        picked.append({"row": r, "tail": tail, "path": path})

    # 分三段再统一编号：① 被系统打断（逐场看）② 正常收尾但末句把球踢给你（中等）③ 收了的尾
    # 编号在分段之后才给 ⇒ 打印出来的序号连续，`export --pick` 才不会跳号。
    hard, pend, rest = [], [], []
    for it in picked:
        e = it["tail"].end
        if _is_hard_break(e):
            hard.append(it)
        elif it["tail"].handoff:
            pend.append(it)
        else:
            rest.append(it)  # completed（真收了尾）或 user-interrupt（你自己按停的）
    items = hard + pend + rest
    for i, it in enumerate(items, 1):
        it["n"] = i

    # 落编号表：让 export --pick 1,3 能把数字翻回会话（别让人肉抄 uuid）
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    LAST_SCAN.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_hours": None if args.all else args.hours,
        "current_uid": current,
        "items": [{
            "n": it["n"], "id": it["row"]["id"], "account": it["row"]["user_id"][:8],
            "title": it["row"]["title"], "cwd": it["row"]["cwd"], "status": it["row"]["status"],
            "act": _ts(it["row"]["act"]), "end": it["tail"].end,
            "end_reset": it["tail"].end_reset, "handoff": it["tail"].handoff,
            "n_msg": it["tail"].n_msg,
            "n_call": it["tail"].n_call, "path": str(it["path"]),
        } for it in items],
    }, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")

    n429 = sum(1 for it in items if it["tail"].end.startswith("429"))
    head = f"窗口 {'全库' if args.all else f'{args.hours:g}h'} · 共 {len(items)} 场"
    if n429:
        head += f" · 限流 {n429}"
    if n_arch:
        head += f" · 另 {n_arch} 场已归档未列"
    print(f"### {head}（段内按最后活动倒序）")
    if current:
        phone = phone_of.get(current) or phone_of.get(current[:8]) or ""
        others = ", ".join(u[:8] for u in uids if u != current) or "无"
        print(f"### 当前号({source}) = {current[:8]}{(' / ' + _mask(phone)) if phone else ''} ｜ 另一号 = {others}")
    print()

    if not items:
        print("（没有命中。放宽 --hours，或加 --all 看全库；也可能真的没有待接手会话）")
        return 0

    if hard:
        print(f"── 一、被系统打断的 {len(hard)} 场（断在半路，逐场看）" + "─" * 10)
        print()
        for it in hard:
            _print_detail(it, current)
    if pend:
        print(f"── 二、正常收尾、但末句把球踢给你的 {len(pend)} 场"
              f"（换号后你没回，它就停在那儿了）" + "─" * 2)
        print()
        for it in pend:
            _print_pend(it, current)
    if rest:
        show = rest if args.brief <= 0 else rest[: args.brief]
        hidden = len(rest) - len(show)
        print(f"── 三、其余 {len(rest)} 场（agent 说完了 / 你自己按停的；扫标题看你还有没有想追加的）"
              + "─" * 2)
        print()
        for it in show:
            _print_brief(it, current)
        if hidden:
            print(f"   …另有 {hidden} 场更早的没列（加 --brief 0 全看）")
        print()

    print('导出指定场次：uv run python -m tools.wb_takeover export --pick 1,3 --note "断在哪"')
    return 0


def _who_flag(row: dict, tail: Tail, current: str | None) -> str:
    """账号归属 + 收尾标记。标记只描述观测到的形态，不说该不该接。"""
    if tail.end.startswith("429"):
        flag = "●限流"
    elif tail.end == "user-interrupt":
        flag = "· 用户中断"
    elif tail.handoff:
        flag = "◇ 等回话"
    elif tail.end == "completed":
        flag = "· 已完成"
    else:
        flag = f"· {tail.end_label}"
    who = ("当前号" if row["user_id"] == current else "另一号") if current else row["user_id"][:8]
    if row["status"] == "archived":
        who += "/已归档"
    elif row["status"] == "working":
        who += "/运行中"   # 用户正在里头敲字，别当待接手
    return f"{who}  {flag}"


def _print_detail(it: dict, current: str | None) -> None:
    r, t = it["row"], it["tail"]
    extra = []
    if r["is_playground"]:
        extra.append("playground")
    if r["is_background_automation"]:
        extra.append("后台自动化")
    if r["used"] and r["size"]:
        extra.append(f"ctx {r['used'] / 1000:.0f}k/{r['size'] / 1000:.0f}k")
    size_mb = it["path"].stat().st_size / 1e6

    print(f"[{it['n']}] {r['id'][:8]}  {_who_flag(r, t, current)}  {_ts(r['act'])}  "
          f"{size_mb:.1f}MB  {t.n_msg}msg/{t.n_call}call"
          + (f"  {', '.join(extra)}" if extra else ""))
    if t.end_reset:
        print(f"    重置  {t.end_reset}")
    print(f"    标题  {r['title'] or '(无标题)'}")
    print(f"    目录  {r['cwd'] or '-'}")
    if t.last_user:
        print(f"    末用户 [{_ts(t.last_user_at)}] {_one_line(t.last_user, 150)}")
    print(f"    末agent {(_one_line(t.last_real_asst, 200)) if t.last_real_asst else '（无）'}")
    if t.last_tool:
        print(f"    末工具  {t.last_tool}")
    print()


def _print_pend(it: dict, current: str | None) -> None:
    """正常收尾但末句在等你回话：三行——谁、什么活、它要你接哪一句。"""
    r, t = it["row"], it["tail"]
    print(f"[{it['n']}] {_ts(r['act'])}  {_who_flag(r, t, current)}  "
          f"{t.n_msg}msg/{t.n_call}call  {r['title'] or '(无标题)'}")
    print(f"     球在你 → {t.handoff}")
    if t.last_user:
        print(f"     末用户 [{_ts(t.last_user_at)}] {_one_line(t.last_user, 120)}")
    print()


def _print_brief(it: dict, current: str | None) -> None:
    """正常收尾的压成两行：一行认身份，一行给你看末句（◇ 那句就是球在你手上）。"""
    r, t = it["row"], it["tail"]
    print(f"[{it['n']}] {_ts(r['act'])}  {_who_flag(r, t, current)}  "
          f"{t.n_msg}msg/{t.n_call}call  {r['title'] or '(无标题)'}")
    tail_line = t.handoff or _one_line(t.last_real_asst, 110)
    print(f"     ↳ {tail_line or '（无）'}")


# ---------------------------------------------------------------- export

_PROMPT_TMPL = """接手一场被中途打断的 WorkBuddy 会话。

会话标题：{title}
原会话：{sid8}（账号 {account}；{when} 收尾形态 = {end_label}{reset}）
工作目录：{cwd}

材料在 {pkg} ，按顺序读：
1. HANDOFF.md   —— 事实头：元数据 / 尾部原文 / 改动过的文件。先看这个，2 分钟。
2. chat_full.md —— 该会话全量对话（U=用户，A=agent，`[n]` 是全量条目序号）。这是主要上下文，**读完再动手**。
3. source.jsonl —— 原始事件流，只在需要逐字核对工具参数/回显时才翻。

断点（派发人转述，可能与材料不符，以材料为准）：
{note}

交付方式（按这个顺序，别跳步）：
1. 先重述**我要回答的问题**：逐条写「要我定什么 / 为什么非我不可 / 可选项或你的建议」。
   我是限流换号过来的，记不清当时被问了什么 —— 别只说「球在你那儿」，把问题本身摊开给我看。
2. 如果材料里确实**没有遗留问题**（活已经干完，或不需要我拍板）：只做情况汇报，3-5 行讲清现状，
   然后停下等我指示。⛔ 不要自己另找活干，也不要「顺手」加码。
3. 有遗留问题：直接接着做，动手前先用 3-5 行说明你读到的当前状态 + 打算怎么做，然后再改文件。
   不要重新采访我；除非两份材料互相矛盾，才问一句。
4. 以 chat_full.md 里**时间靠后**的内容为准（我可能中途改过方向、推翻过前面的结论）。
"""


_PROMPT_PEND = """接手一场「正常收尾、但末句在等用户回话」的 WorkBuddy 会话。

会话标题：{title}
原会话：{sid8}（账号 {account}；{when} 最后一条是 agent 的回话，用户换号前没来得及答）
工作目录：{cwd}

材料在 {pkg} ，按顺序读：
1. HANDOFF.md   —— 事实头：元数据 / 尾部原文 / 改动过的文件。先看这个，2 分钟。
2. chat_full.md —— 该会话全量对话（U=用户，A=agent，`[n]` 是全量条目序号）。这是主要上下文，**读完再动手**。
3. source.jsonl —— 原始事件流，只在需要逐字核对工具参数/回显时才翻。

原会话末句停在（原文）：
{handoff}

派发人补充（可能与材料不符，以材料为准）：
{note}

交付方式（按这个顺序，别跳步）：
1. 这场**不是被报错打断的**，是干完一段、把下一步交回给我了。先判断：悬着的那件事到底做完没有？
   别默认「已完成」就整段重做。
2. 然后先重述**我要回答的问题**：逐条写「要我定什么 / 为什么非我不可 / 可选项或你的建议」。
   我记不清当时被问了什么 —— 别只说「在等你回话」，把问题本身摊开给我看。
3. 如果判断下来**没有遗留问题**（活已经干完，或不需要我拍板）：只做情况汇报，3-5 行讲清现状，
   然后停下等我指示。⛔ 不要自己另找活干，也不要「顺手」加码。
4. 有遗留问题：动手前先用 3-5 行说明当前状态 + 打算怎么做，再把悬着的那件事接着做完。
   不要重新采访我；除非两份材料互相矛盾，才问一句。
5. 以 chat_full.md 里**时间靠后**的内容为准（我可能中途改过方向、推翻过前面的结论）。
"""


def _pkg_dir(sid: str, act: object) -> Path:
    ts = act if isinstance(act, (int, float)) and act else 0
    day = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else "0000-00-00"
    return OUT_ROOT / day / sid[:8]


def _write_chat(path: Path, out: Path) -> int:
    """全量对话（与 wb_history dump 同口径：全量编号、U/A 双方、注入块剥净）。"""
    items = []
    for role, ts, raw in _iter_messages(path):
        text = _clean(raw) if role == "user" else raw.strip()
        if not text:
            continue
        items.append((len(items) + 1, role, ts, text))
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# 全量对话 · {path.stem}\n\n源：`{path}`\n\n共 {len(items)} 条（编号为全量序号）\n\n")
        for idx, role, ts, text in items:
            f.write(f"## [{idx}] {role} {_ts(ts)}\n\n{text}\n\n---\n\n")
    return len(items)


def _export_one(prefix: str, note: str, current: str | None) -> Path | None:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT s.*, u.used, u.size FROM sessions s LEFT JOIN session_usage u ON u.session_id = s.id "
        "WHERE s.id LIKE ? ORDER BY COALESCE(s.last_activity_at, s.updated_at) DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    if row is None:
        print(f"[ERROR] 库里没有会话 {prefix}*", file=sys.stderr)
        return None
    sid = row["id"]
    path = _jsonl_index().get(sid[:8])
    if path is None:
        print(f"[ERROR] 找不到 {sid[:8]} 的 jsonl", file=sys.stderr)
        return None

    act = row["last_activity_at"] or row["updated_at"]
    tail = _read_tail(path)
    files = _collect_files(path)
    pkg = _pkg_dir(sid, act)
    pkg.mkdir(parents=True, exist_ok=True)

    # ① 源文件拷贝（沙箱不许读家目录时，接手方仍拿得到真相）
    shutil.copy2(path, pkg / "source.jsonl")
    # ② 全量对话
    n_items = _write_chat(path, pkg / "chat_full.md")
    # ③ 事实头
    title = row["custom_title"] or row["title"] or "(无标题)"
    reset = f"，重置 {tail.end_reset}" if tail.end_reset else ""
    h = io.StringIO()
    h.write(f"# 接手包 · {sid[:8]} · {title}\n\n")
    h.write("> 由 `uv run python -m tools.wb_takeover export` 生成。**上半是观测事实，下半是原文**；\n")
    h.write("> 本文件不含「该怎么办」的判断，读完自己定。重跑 export 会整份覆盖。\n\n")
    h.write("## 一、事实\n\n| 项 | 值 |\n|---|---|\n")
    h.write(f"| 会话 id | `{sid}` |\n")
    h.write(f"| 账号 user_id | `{row['user_id']}`（8 位 `{row['user_id'][:8]}`）|\n")
    h.write(f"| 标题 | {title} |\n")
    h.write(f"| 工作目录 | `{row['cwd'] or '-'}` |\n")
    h.write(f"| 创建 / 最后活动 | {_ts(row['created_at'])} → {_ts(act)} |\n")
    h.write(f"| DB status | `{row['status']}`（⛔ UI 状态，不代表运行结果）|\n")
    h.write(f"| 模式 / 模型 | `{row['mode'] or '-'}` / `{row['model'] or '-'}` |\n")
    if row["used"] and row["size"]:
        h.write(f"| 上下文用量 | {row['used']:,} / {row['size']:,}（{row['used'] / row['size'] * 100:.0f}%）|\n")
    h.write(f"| 收尾形态 | **{tail.end_label}**（`{tail.end}`）{reset} |\n")
    if tail.handoff:
        h.write(f"| 末句在等你回话 | {tail.handoff} |\n")
    h.write(f"| 消息 / 工具调用 | {tail.n_msg} / {tail.n_call}（429 在整场出现 {tail.n_429} 次）|\n")
    if len(tail.a429_at) > 1:
        h.write("| 429 时间点 | " + " / ".join(_ts(x) for x in tail.a429_at) + " |\n")
    h.write(f"| 源 jsonl | `{path}`（{path.stat().st_size / 1e6:.1f} MB，已复制到 `source.jsonl`）|\n")
    if row["is_playground"]:
        h.write("| 备注 | `is_playground=1`（在 WorkBuddy 临时工作区里跑的）|\n")
    h.write("\n")

    h.write("## 二、尾巴（最近 6 条原文）\n\n")
    tail_items = []
    for role, ts, raw in _iter_messages(path):
        text = _clean(raw) if role == "user" else raw.strip()
        if text:
            tail_items.append((role, ts, text))
    for role, ts, text in tail_items[-6:]:
        shown = text if len(text) <= 1200 else text[:1200].rstrip() + "…（截断）"
        h.write(f"**{role} · {_ts(ts)}**\n\n{shown}\n\n")

    h.write("## 三、断点前的工具调用（最近 25 条，参数截断）\n\n")
    h.write("> `source.jsonl` 里能拿到完整参数与回显；这里只给「它在干什么」的流水，用来判断断在哪一步。\n\n")
    calls = list(_iter_tool_calls(path))
    if calls:
        h.write("```\n")
        for ts, name, args in calls[-25:]:
            h.write(f"{_ts(ts)[11:]}  {name:<14s} {_arg_brief(args)}\n")
        h.write("```\n\n")
    else:
        h.write("（无工具调用）\n\n")

    h.write("## 四、改动过的文件（Edit/Write，全量口径）\n\n")
    if files:
        h.write(f"共 {len(files)} 个（按写入次数倒序；⚠ 是次数不是最终状态）\n\n")
        for fp, cnt, first, last in files:
            h.write(f"- `{fp}` ×{cnt}" + (f"　首 {_ts(first)}" if first else "")
                    + (f"　末 {_ts(last)}" if last else "") + "\n")
    else:
        h.write("（无 Edit/Write 记录）\n")
    h.write("\n## 五、往下读\n\n")
    h.write(f"- `chat_full.md`：全量 {n_items} 条对话（主要上下文；工具调用的参数/回显不在这里，在 source.jsonl）\n")
    h.write(f"- `source.jsonl`：原始事件流 {(pkg / 'source.jsonl').stat().st_size / 1e6:.1f} MB"
            "（`function_call` / `function_call_result` 在这里，不在 message 里）\n")
    h.write("- 编号规矩：`[n]` = 全量条目序号，跨次运行稳定；引用时连 session id + 时间一起写\n")
    (pkg / "HANDOFF.md").write_text(h.getvalue(), encoding="utf-8", newline="\n")

    # ④ 派发提示词
    if current is None:
        acct = row["user_id"][:8]
    else:
        acct = row["user_id"][:8] + ("（当前号）" if row["user_id"] == current else "（另一号）")
    tmpl = _PROMPT_PEND if tail.handoff else _PROMPT_TMPL
    prompt = tmpl.format(
        title=title, sid8=sid[:8], account=acct, when=_ts(act), end_label=tail.end_label,
        reset=reset, cwd=row["cwd"] or "-", pkg=str(pkg.resolve()),
        handoff=tail.handoff,
        note=note.strip() or "（未填；自己从 HANDOFF.md 的尾部原文判断断点）",
    )
    (pkg / "PROMPT.md").write_text(prompt, encoding="utf-8", newline="\n")

    print(f"[OK] {pkg.resolve()}")
    print(f"     收尾={tail.end_label}{reset}  消息={tail.n_msg}  工具={tail.n_call}  改动文件={len(files)}")
    print("--- PROMPT.md ---")
    print(prompt)
    return pkg


def cmd_export(args: argparse.Namespace) -> int:
    targets = list(args.session)
    current: str | None = None
    if args.pick:
        if not LAST_SCAN.exists():
            print("[ERROR] 没有 scan 记录，先跑 scan，或直接给会话 id", file=sys.stderr)
            return 2
        scan = json.loads(LAST_SCAN.read_text(encoding="utf-8"))
        by_n = {str(it["n"]): it["id"] for it in scan.get("items", [])}
        for piece in args.pick.replace(" ", "").split(","):
            if piece not in by_n:
                print(f"[ERROR] scan 里没有编号 {piece}（{scan.get('generated_at')} 那次共 "
                      f"{len(by_n)} 项；重跑 scan 再试）", file=sys.stderr)
                return 2
            targets.append(by_n[piece])
        current = scan.get("current_uid")
    if not targets:
        print("[ERROR] 给会话 id，或用 --pick 1,3 指定 scan 编号", file=sys.stderr)
        return 2

    ok = 0
    for t in targets:
        if _export_one(t, args.note, current):
            ok += 1
        print()
    print(f"完成 {ok}/{len(targets)}")
    return 0 if ok == len(targets) else 1


def cmd_accounts(args: argparse.Namespace) -> int:
    if args.set:
        spec = args.set.strip()
        if "=" not in spec:
            print("[ERROR] 格式：accounts --set 手机号=uuid8", file=sys.stderr)
            return 2
        phone, uid = (x.strip() for x in spec.split("=", 1))
        data = _load_accounts()
        data[phone] = uid
        ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACCOUNTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        print(f"[OK] 已登记 → {ACCOUNTS_FILE}（该文件不入库）")
        return 0

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    phone_of = {v: k for k, v in _load_accounts().items()}
    print(f"{'user_id':40s} {'手机号':13s} {'会话':4s} 最后活动")
    for uid, cnt, act in conn.execute(
        "SELECT user_id, COUNT(*), MAX(COALESCE(last_activity_at, updated_at)) FROM sessions "
        "WHERE deleted_at IS NULL GROUP BY user_id ORDER BY 3 DESC"
    ):
        ph = phone_of.get(uid) or phone_of.get(uid[:8]) or ''
        print(f"{uid:40s} {(_mask(ph) if ph else '-'):13s} {cnt:<4d} {_ts(act)}")
    if not phone_of:
        print(f"\n（还没有手机号登记。需要时：accounts --set 手机号=uuid8；只落 {ACCOUNTS_FILE}，不入库）")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wb_takeover", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="列出候选会话（按最后活动倒序，标注收尾形态）")
    s.add_argument("--hours", type=float, default=24, help="时间窗（小时，默认 24）")
    s.add_argument("--all", action="store_true", help="忽略时间窗，扫全库（约 12s）")
    s.add_argument("--only", default="",
                   help="只看某种收尾：429 / pending(等回话) / done(真收尾) / "
                        "user-interrupt / completed / no-reply …")
    s.add_argument("--brief", type=int, default=20,
                   help="第二段最多列几场（默认 20，0=不限）")
    s.add_argument("--account", default="", help="当前账号（手机号或 uuid 前缀）；不给则取最近活动的号")
    s.add_argument("--include-deleted", action="store_true", help="含已在 UI 删除的会话")
    s.add_argument("--include-archived", action="store_true",
                   help="连已归档的也列（默认一律排除：归档＝用户明确不再理会）")
    s.add_argument("--skip-current", action="store_true", help="跳过当前账号正在运行的那场")
    s.set_defaults(func=cmd_scan)

    e = sub.add_parser("export", help="导出接手包（HANDOFF/chat_full/source/PROMPT）")
    e.add_argument("session", nargs="*", help="会话 id 前缀（可多个）")
    e.add_argument("--pick", default="", help="用上次 scan 的编号，逗号分隔，如 1,3")
    e.add_argument("--note", default="", help="断点说明，写进派发提示词（1-3 句）")
    e.set_defaults(func=cmd_export)

    a = sub.add_parser("accounts", help="列出账号；--set 手机号=uuid8 登记")
    a.add_argument("--set", default="", help="登记映射，如 --set 13800138000=988245f8")
    a.set_defaults(func=cmd_accounts)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

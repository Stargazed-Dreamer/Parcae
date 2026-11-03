"""从 WorkBuddy 本地会话库还原历史对话（用户 + agent 双方，不只用户一侧）。

背景：用户不自己存档需求，遇到「这条要求是不是丢了 / 当时到底怎么定的」时，
证据只在 WorkBuddy 的会话库里。本脚本把它捞出来，输出成可读的 markdown。

⛔ 默认**同时抽用户原话与 agent 回复**，别只看用户说了什么。只抽用户一侧会产生倾向性：
  - 用户的话大多是**回应**（选方案、否掉提议、反问），脱离 agent 那一轮就看不懂在指什么；
  - 「当时给了哪些选项 / 实际怎么做的 / 踩了什么坑」只存在于 agent 侧；
  - 只拿用户单方面的话当证据，容易得出「用户改主意了」这种错误结论。
要引「用户逐字原话」时仍可用 `--user-only` 收窄，但**判断语境时必须回来看 agent 侧**。

用法：
    uv run python -m tools.wb_history list                     # 全部会话（创建时间倒序）
    uv run python -m tools.wb_history list --sort activity      # 按最后活动时间倒序（找「最近还在动的那场」）
    uv run python -m tools.wb_history list -k 提示词,交接       # 按标题关键词筛
    uv run python -m tools.wb_history dump <session_id前缀>     # 还原该会话双方发言（默认）
    uv run python -m tools.wb_history dump <前缀> -k 交接,版本   # 只留命中关键词的条目（双方都筛）
    uv run python -m tools.wb_history dump <前缀> -k 版本 -c 2   # 命中条目各带前后 2 条上下文（补指代链）
    uv run python -m tools.wb_history dump <前缀> --tail 40     # 只看最后 40 条（接手/续做定位尾部）
    uv run python -m tools.wb_history dump <前缀> --files       # 附加「改过哪些文件」（Edit/Write 的 file_path）
    uv run python -m tools.wb_history dump <前缀> --user-only   # 只要用户原话（逐字取证用）
    uv run python -m tools.wb_history dump <前缀> --clip 300    # agent 回复截到 300 字（默认不截）

产出默认写到 temp/wb_hist/<session_id>_chat.md（`--user-only` 时写 _user.md；temp/ 不入库）。

编号规矩：`[n]` **恒为该会话全量条目序号**，不因 -k / -c / --tail / --user-only 重排。
过滤后编号会跳号，那是被过滤掉的条目，引用时直接用这个号（跨次运行稳定）。

实现要点（踩过的坑，别自作聪明改）：
  1. 会话索引在 ~/.workbuddy/workbuddy.db 的 sessions 表；正文在同目录
     projects/<workspace-slug>/<session-id>.jsonl。
  2. jsonl 每行是一个事件：{"type":"message","role":"user","content":[{"type":"input_text",
     "text":...}]}。真实用户话术藏在 <user_query> 里，外面裹着 system-reminder /
     identity_context / project_context 等注入块，必须剥掉，否则抽出来全是噪音。
  3. 会话标题是自动生成的，可能和用户记忆里的叫法不一致（用户说「审查…开关什么的」，
     实际标题是「审查本地代理提示词开关文件」）。按标题搜不到就按正文关键词搜。
  4. 时间戳是毫秒 epoch。
  5. 压缩有两层痕迹，**都不是原话**：
     - role=user 的 <conversation_history_summary>（旧的摘要字段）；
     - role=user 的 <cb_summary>…</cb_summary> 整块——它**内部还包着若干 <user_query>**，
       那些是历史原话的回放。本机实测 521 条 cb_summary 里 520 条整条只是摘要（没有本轮输入），
       只有 1 条在摘要之后另跟真实输入。所以正确做法是**先剥 cb_summary 块**，
       剥完没内容就整条丢弃；直接 search 第一个 <user_query> 会把历史原话当成新一轮，
       还贴上本轮时间戳（2026-10-30 修的就是这个，见 _clean）。
  6. 文件可能十几 MB，别整份读进上下文：先 list 或 dump 的首句目录，再定点读。
  7. agent 侧的回复里会夹大段代码/工具回显，默认照抽（失真最小），嫌长用 --clip。
     首句目录用 U/A 标角色，先扫目录再定点读。
  8. 工具调用单独存在 `type=="function_call"` 事件里（不属 message），本脚本只在 --files 时读它。
     一个会话实测 280 条 function_call（Bash 192 / Edit 55 / Read 24 / Write 3…），
     Edit 的 arguments 带 file_path —— 这是「这活到底改了哪些文件」的唯一来源。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

WB_ROOT = Path(os.environ.get("WORKBUDDY_HOME") or (Path.home() / ".workbuddy"))
DB_PATH = WB_ROOT / "workbuddy.db"
PROJECTS_DIR = WB_ROOT / "projects"
OUT_DIR = Path("temp") / "wb_hist"

_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S)
# 压缩摘要整块：内部还包着若干 <user_query>（历史原话回放），必须整块剥掉再找本轮输入
_CB_SUMMARY_RE = re.compile(r"<cb_summary>.*?</cb_summary>", re.S)
_DROP_BLOCKS = [
    re.compile(r"<system-reminder.*?</system-reminder>", re.S),
    re.compile(r"<identity_context>.*?</identity_context>", re.S),
    re.compile(r"<project_context>.*?</project_context>", re.S),
    re.compile(r"<connector-status>.*?</connector-status>", re.S),
    re.compile(r"<current_time>.*?</current_time>", re.S),
    re.compile(r"<product_identity>.*?</product_identity>", re.S),
]
# 不是用户本人的话：后台任务完成通知、团队其它 agent 的消息、harness 的续写指令
# （本机实测：task-notification 891 条、teammate-message 37 条、续写提示 89 条）
_NOT_HUMAN_PREFIXES = (
    "<task-notification",
    "<teammate-message",
    "<agent-message",
    "Please continue with the conversation",
    "This session is being continued",
    "[Request interrupted by user",
    "Continue from where you left",
)
# 会写文件的工具：取 file_path 就是「改动面」
_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _clean(text: str) -> str:
    """抽出本轮真实用户输入；纯压缩摘要 / 非人话返回空串（由调用方丢弃）。

    ⛔ 别退回「直接 search 第一个 <user_query>」：<cb_summary> 里的 user_query 是历史回放，
    取它会得到一条贴在本轮时间戳上的旧原话，真正的当前输入反而没有。
    """
    body = text
    is_summary = "<cb_summary>" in body
    if is_summary:
        body = _CB_SUMMARY_RE.sub("", body)
    m = _QUERY_RE.search(body)
    if m:
        return m.group(1).strip()
    out = body
    for pat in _DROP_BLOCKS:
        out = pat.sub("", out)
    out = out.strip()
    if not out or out.startswith(_NOT_HUMAN_PREFIXES):
        return ""
    return out


def cmd_list(args: argparse.Namespace) -> int:
    if not DB_PATH.exists():
        print(f"[ERROR] 找不到 {DB_PATH}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    where = "WHERE deleted_at IS NULL"
    params: list[object] = []
    if args.keyword:
        kws = [k.strip() for k in args.keyword.split(",") if k.strip()]
        if kws:
            where += " AND (" + " OR ".join("title LIKE ?" for _ in kws) + ")"
            params.extend(f"%{k}%" for k in kws)
    total = conn.execute(f"SELECT COUNT(*) FROM sessions {where}", tuple(params)).fetchone()[0]
    order = "COALESCE(last_activity_at, updated_at)" if args.sort == "activity" else "created_at"
    sql = (
        "SELECT id, user_id, title, custom_title, cwd, created_at, "
        f"COALESCE(last_activity_at, updated_at), status FROM sessions {where} "
        f"ORDER BY {order} DESC LIMIT ?"
    )
    rows = conn.execute(sql, tuple([*params, args.limit])).fetchall()
    print(f"{'创建':16s} {'最后活动':16s} {'账号':9s} {'会话':9s} {'状态':10s} 标题")
    for sid, uid, title, custom, cwd, ts, act, status in rows:
        made = datetime.fromtimestamp((ts or 0) / 1000).strftime("%Y-%m-%d %H:%M")
        live = datetime.fromtimestamp((act or 0) / 1000).strftime("%Y-%m-%d %H:%M")
        who = (uid or "")[:8]
        print(f"{made}  {live}  {who:9s} {sid[:8]}  {(status or '-'):10s} {(title or custom or '(无标题)')}  [{cwd}]")
    if total > len(rows):
        print(f"\n共 {total} 条，本次显示 {len(rows)} 条（--limit {args.limit}，调大或加 -k 收窄）")
    elif total == 0:
        print("\n共 0 条（关键词没命中标题；正文关键词要用 dump -k，或 list 不加 -k 看全量）")
    else:
        print(f"\n共 {len(rows)} 条（已全部显示）")
    print(f"正文用：dump <会话id前缀>；按最后活动时间排：list --sort activity")
    return 0


def _find_jsonl(prefix: str) -> Path | None:
    hits = [p for p in PROJECTS_DIR.glob(f"*/{prefix}*.jsonl")]
    if len(hits) == 1:
        return hits[0]
    if hits:
        print("[WARN] 前缀命中了多个文件：", file=sys.stderr)
        for h in hits:
            print("  ", h, file=sys.stderr)
        return hits[0]
    return None


def _iter_messages(path: Path):
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "message":
                continue
            role = d.get("role")
            if role not in ("user", "assistant"):
                continue
            parts = []
            for c in d.get("content") or []:
                if isinstance(c, dict) and c.get("type") in ("input_text", "text", "output_text"):
                    parts.append(c.get("text") or "")
            text = "\n".join(p for p in parts if p)
            if not text.strip():
                continue
            if "<conversation_history_summary>" in text:
                continue
            yield (role, d.get("timestamp"), text)


def _iter_tool_calls(path: Path):
    """产出 (timestamp, tool_name, arguments_dict)。工具调用不在 message 事件里。"""
    with io.open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or '"function_call"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "function_call":
                continue
            raw = d.get("arguments")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = {}
            yield d.get("timestamp"), str(d.get("name") or "?"), raw if isinstance(raw, dict) else {}


def _collect_files(path: Path) -> list[tuple[str, int, int | None, int | None]]:
    """汇总「改过哪些文件」：file_path → [次数, 首见毫秒, 末见毫秒]。"""
    stats: dict[str, list[int]] = {}
    seen: dict[str, tuple[int | None, int | None]] = {}
    for ts, name, args in _iter_tool_calls(path):
        if name not in _WRITE_TOOLS:
            continue
        fp = args.get("file_path") or args.get("notebook_path") or args.get("path")
        if not isinstance(fp, str) or not fp.strip():
            continue
        key = fp.replace("\\", "/").strip()
        stats.setdefault(key, [0, 0, 0])
        stats[key][0] += 1
        if isinstance(ts, (int, float)):
            prev = seen.get(key)
            first = prev[0] if prev and prev[0] is not None else int(ts)
            last = int(ts)
            seen[key] = (min(first, int(ts)), max(last, int(ts)))
    out = []
    for key, (cnt, _, _) in sorted(stats.items(), key=lambda kv: -kv[1][0]):
        first, last = seen.get(key, (None, None))
        out.append((key, cnt, first, last))
    return out


def _stamp(ts: object) -> str:
    if isinstance(ts, (int, float)) and ts:
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
    return "-"


def cmd_dump(args: argparse.Namespace) -> int:
    path = _find_jsonl(args.session)
    if path is None:
        print(f"[ERROR] 没找到会话文件：{args.session}*", file=sys.stderr)
        return 2
    print(f"[INFO] 源文件 {path} ({path.stat().st_size / 1e6:.1f} MB)")
    kws = [k.strip() for k in (args.keyword or "").split(",") if k.strip()]
    if args.context and not kws:
        print("[WARN] -c/--context 只在配合 -k 时有意义，本次忽略", file=sys.stderr)

    # ① 先按全量口径编号（编号一次定死，后面的过滤一律不重排）
    items: list[tuple[int, str, object, str]] = []
    for role, ts, raw in _iter_messages(path):
        # 默认双方都收：只抽用户一侧会把「用户在回应什么」整段丢掉
        text = _clean(raw) if role == "user" else raw.strip()
        if not text:
            continue
        items.append((len(items) + 1, role, ts, text))
    total = len(items)

    # ② 尾部窗口（接手/续做最常看尾部，不用先读目录再定位）
    rows = items
    if args.tail and args.tail > 0:
        rows = [r for r in items if r[0] > total - args.tail]

    # ③ 关键词 + 上下文：命中条目前后各带 N 条，避免「-k 之后指代链断掉」
    marks: set[int] = set()
    if kws:
        marks = {r[0] for r in rows if any(k in r[3] for k in kws)}
        if marks and args.context and args.context > 0:
            span = range(-args.context, args.context + 1)
            keep = {i + d for i in marks for d in span}
            rows = [r for r in rows if r[0] in keep]
        else:
            rows = [r for r in rows if r[0] in marks]
    if args.user_only:
        rows = [r for r in rows if r[1] == "user"]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.user_only:
        out = OUT_DIR / f"{path.stem}_user.md"
        heading = "用户原话"
    else:
        out = OUT_DIR / f"{path.stem}_chat.md"
        heading = "对话还原（用户 + agent）"
    files = _collect_files(path) if args.files else []
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# {path.stem} · {heading}\n\n")
        f.write(f"源：`{path}`\n\n全量 {total} 条，本次输出 {len(rows)} 条（编号为全量序号，跳号=被过滤）\n\n")
        for idx, role, ts, text in rows:
            if kws and idx not in marks:
                shown = text[: args.clip] if args.clip and len(text) > args.clip else text
                note = f"（上下文，非命中；原 {len(text)} 字）" if args.clip and len(text) > args.clip else "（上下文）"
                f.write(f"## [{idx}] {role} {_stamp(ts)} {note}\n\n{shown}\n\n---\n\n")
                continue
            if role == "assistant" and args.clip and len(text) > args.clip:
                text = text[: args.clip].rstrip() + f"…（已截断，原 {len(text)} 字）"
            f.write(f"## [{idx}] {role} {_stamp(ts)}\n\n{text}\n\n---\n\n")
        if files:
            f.write(f"## 改动文件（--files，全量口径，不受 -k 影响）\n\n")
            f.write(f"共 {len(files)} 个文件被 Edit/Write 写过（按次数倒序）\n\n")
            for fp, cnt, first, last in files:
                f.write(f"- `{fp}` ×{cnt}{'  首 ' + _stamp(first) if first else ''}{'  末 ' + _stamp(last) if last else ''}\n")
            f.write("\n")
    print(f"[OK] 写入 {out}（{len(rows)}/{total} 条）")

    # 首句目录：U=用户 A=agent。先扫一遍再定点读，避免把整份塞进上下文
    print("首句目录（编号=全量序号；* = 关键词命中，其余为上下文）：")
    for idx, role, ts, text in rows:
        tag = "U" if role == "user" else "A"
        star = "*" if idx in marks else " "
        first = " ".join(text.split())[:90]
        print(f" {star}[{idx}] {tag} {len(text):6d}  {first}")

    if files:
        print(f"\n改动文件 {len(files)} 个（全量口径）：")
        for fp, cnt, first, last in files:
            print(f"  ×{cnt:<3d} {fp}")
    return 0


def cmd_copy(args: argparse.Namespace) -> int:
    """把 jsonl 复制到工作区（某些沙箱会拦直接读家目录的命令行）。"""
    path = _find_jsonl(args.session)
    if path is None:
        print(f"[ERROR] 没找到会话文件：{args.session}*", file=sys.stderr)
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dst = OUT_DIR / path.name
    shutil.copy2(path, dst)
    print(f"[OK] {dst}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wb_history", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出会话")
    p_list.add_argument("-k", "--keyword", default="", help="标题关键词，逗号分隔")
    p_list.add_argument("-n", "--limit", type=int, default=60)
    p_list.add_argument(
        "--sort", choices=("created", "activity"), default="created",
        help="created=按创建时间（默认）｜activity=按最后活动时间",
    )
    p_list.set_defaults(func=cmd_list)

    p_dump = sub.add_parser("dump", help="还原某会话的对话（默认双方）")
    p_dump.add_argument("session", help="会话 id 前缀（8 位足够）")
    p_dump.add_argument("-k", "--keyword", default="", help="正文关键词，逗号分隔（双方都筛）")
    p_dump.add_argument(
        "-c", "--context", type=int, default=0,
        help="命中条目前后各带 N 条上下文（默认 0；补指代链用，建议 1-3）",
    )
    p_dump.add_argument("--tail", type=int, default=0, help="只看最后 N 条（0=全部）")
    p_dump.add_argument(
        "--files", action="store_true",
        help="附加「改过哪些文件」清单（Edit/Write 的 file_path，全量口径）",
    )
    p_dump.add_argument(
        "--user-only", action="store_true", help="只抽用户原话（逐字取证用，丢上下文）"
    )
    p_dump.add_argument(
        "--clip", type=int, default=0, help="agent 回复截断到 N 字（0=不截，默认）"
    )
    p_dump.add_argument(
        "--all",
        dest="legacy_all",
        action="store_true",
        help="(兼容保留) 旧写法，现默认已含 agent 回复，等价于不加参数",
    )
    p_dump.set_defaults(func=cmd_dump)

    p_copy = sub.add_parser("copy", help="只复制 jsonl 到 temp/wb_hist/")
    p_copy.add_argument("session")
    p_copy.set_defaults(func=cmd_copy)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

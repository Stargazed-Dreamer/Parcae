"""从 Qoder（QoderCN）本地会话库还原历史对话（用户 + agent 双方，不只用户一侧）。

背景：本机有 ≥4 个 harness（WorkBuddy / Qoder / Claude Code / CodeBuddy），用户不自己存档需求。
「这条要求我是不是在 Qoder 里说过」的现场证据只在 Qoder 的会话 jsonl 里，WorkBuddy 那边看不到。
本脚本按 `tools/wb_history.py` 的口径把它捞出来，输出可直接读的 markdown。

⛔ 与 wb_history 同一条铁律：默认**同时抽用户原话与 agent 回复**。只抽用户一侧会：
  - 把 agent 提的方案写成「用户要求」；
  - 把一句反问当成最终裁定（agent 后面可能追问/改实现/被再推翻）；
  - 看不懂用户在指什么（「这个 / 这一版 / 不是这样」的指代对象全在 agent 上一轮）。

用法：
    uv run python -m tools.qoder_history list                      # 全部会话（倒序）
    uv run python -m tools.qoder_history list -k 抽卡,管家          # 按标题（首条人类消息）筛
    uv run python -m tools.qoder_history list -k 抽卡 --deep        # 标题不中就全文扫（慢）
    uv run python -m tools.qoder_history dump <session_id 前缀>     # 还原双方发言（默认）
    uv run python -m tools.qoder_history dump <前缀> -k 版本,回滚    # 只留命中关键词的条目（双方都筛）
    uv run python -m tools.qoder_history dump <前缀> -k 版本 -c 2    # 命中条目各带前后 2 条上下文
    uv run python -m tools.qoder_history dump <前缀> --tail 40      # 只看最后 40 条（接手/续做定位尾部）
    uv run python -m tools.qoder_history dump <前缀> --files        # 附加「改过哪些文件」（file-history-snapshot）
    uv run python -m tools.qoder_history dump <前缀> --user-only    # 只要用户原话（逐字取证）
    uv run python -m tools.qoder_history dump <前缀> --clip 300     # agent 回复截到 300 字
    uv run python -m tools.qoder_history dump <前缀> --tools        # 额外列出每条 agent 消息调了哪些工具
    uv run python -m tools.qoder_history copy <前缀>                # 只把 jsonl 拷到 temp/qoder_hist/

编号规矩：`[n]` **恒为该会话全量条目序号**，不因 -k / -c / --tail / --user-only 重排。
过滤后编号会跳号，那是被过滤掉的条目，引用时直接用这个号。

产出：`temp/qoder_hist/<session>_chat.md`（`--user-only` 时 `_user.md`）+ 终端首句目录
（`[序号] U/A 字数 首 90 字`）。文件可能上百 MB，**先看目录再定点读**。

实现要点（踩过的坑，别自作聪明改）：
  1. Qoder 没有 sessions 索引表，会话 = `~/.qoder-cn/projects/<workspace-slug>/<session-id>.jsonl`，
     一台机器多账号/多工作区全都平铺在这层 ⇒ 只能扫目录，标题得现推（取首条人类消息前 45 字）。
  2. 路径两版并存：国内版 `~/.qoder-cn`（本机活跃）、国际版 `~/.qoder`。取第一个真有
     `projects/*/*.jsonl` 的根，可用环境变量 `QODER_HOME` 覆盖。
  3. `type=user` 的行**大多是工具回显**（带 `toolUseResult`），真人类话术的判据是
     `origin.kind == "human"`（新版本还带 `humanInput`）。另有 `task-notification`（后台任务）、
     `malformed-tool-use-retry` / `stream-recovery`（内部重试）三类非人类输入，一律不收。
     本机实测 347 条 `This session is being continued…` 全部不是 human，现有判据已能挡住，
     不用再额外加前缀黑名单。
  4. 压缩续写的会话会在 user 行塞 `This session is being continued from a previous conversation…`
     ——那是 agent 写的摘要不是用户原话；`[Request interrupted by user]` 是中断标记。都丢弃。
     另有 `type=system` + `subtype=compact_boundary`（带 `compactMetadata.preTokens`）标压缩点：
     **压缩点之前的轮次，当时的 agent 只看见摘要、看不见原文**；读历史时原文在、但别据此推断
     「它当时知道这些」。本机 362 条。
  5. 时间戳两种格式并存：user/assistant 是 ISO 字符串（`2026-10-30T06:46:22.298Z`，UTC），
     runtime-config / active-leaf 是毫秒 epoch。统一按本地时区渲染。
  6. assistant 的 `message.content` 是块列表：`thinking` / `text` / `tool_use`，而且**同一条 API
     回复被拆成多行 jsonl**（各块一行、共用 `message.id`）。必须按 `message.id` 聚合，否则一个
     回合会碎成几百条只有工具调用的空回复。`thinking` 不进正文，`tool_use` 仅在 `--tools`/`--files` 时读。
  7. 本机实测 `isSidechain` 全空（子代理记录没落盘），但代码保留 `--include-sidechain` 开关。
  8. 单个会话可以到 118 MB（中位 2.3 MB），别整份读进上下文。
  9. 「改过哪些文件」只在 `type=file-history-snapshot` 的 `snapshot.trackedFileBackups` 里
     （键=相对路径，值含 `version` / `backupTime`），本机实测 31/31 非空 —— `--files` 读它。
     它给的是**版本快照**，不是 Edit 次数；要看逐次编辑仍得 `--tools` 或读原文。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

OUT_DIR = Path("temp") / "qoder_hist"

# 人类话术外面可能裹的注入块（与 wb_history 同口径，剥掉才不是噪音）
_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S)
_DROP_BLOCKS = [
    re.compile(r"<system-reminder.*?</system-reminder>", re.S),
    re.compile(r"<identity_context>.*?</identity_context>", re.S),
    re.compile(r"<project_context>.*?</project_context>", re.S),
    re.compile(r"<connector-status>.*?</connector-status>", re.S),
    re.compile(r"<current_time>.*?</current_time>", re.S),
    re.compile(r"<product_identity>.*?</product_identity>", re.S),
]
# 非人类输入 / 非原话
_NOISE_PREFIXES = (
    "This session is being continued from a previous conversation",
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
)
_NOISE_ORIGINS = {"task-notification", "malformed-tool-use-retry", "stream-recovery"}


def _resolve_root() -> Path:
    """定位 Qoder 数据根：env > 国内版 > 国际版。"""
    env = os.environ.get("QODER_HOME")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    home = Path.home()
    candidates += [home / ".qoder-cn", home / ".qoder", home / ".qoderworkcn"]
    for c in candidates:
        projects = c / "projects"
        if projects.is_dir() and any(projects.glob("*/*.jsonl")):
            return c
    # 都不符合也返回第一个候选，让上层报清晰错误
    return candidates[0]


ROOT = _resolve_root()
PROJECTS_DIR = ROOT / "projects"


# ========== 时间 ==========


def _fmt_ts(ts: object) -> str:
    """统一成本地时间字符串。支持毫秒 epoch / ISO 字符串。"""
    if ts is None or ts == "":
        return "-"
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
        text = str(ts).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return "-"


# ========== 消息抽取 ==========


def _clean_user_text(text: str) -> str:
    m = _QUERY_RE.search(text)
    if m:
        text = m.group(1)
    out = text
    for pat in _DROP_BLOCKS:
        out = pat.sub("", out)
    return out.strip()


def _content_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") in ("text", "input_text", "output_text"):
            parts.append(c.get("text") or "")
    return "\n".join(p for p in parts if p)


def _content_tools(message: dict) -> list[str]:
    names: list[str] = []
    for c in message.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "tool_use":
            name = c.get("name") or "?"
            names.append(str(name))
    return names


def _is_human(d: dict) -> bool:
    if d.get("type") != "user":
        return False
    if d.get("toolUseResult") or d.get("isSidechain"):
        return False
    origin = (d.get("origin") or {}).get("kind")
    if origin in _NOISE_ORIGINS:
        return False
    return origin == "human" or "humanInput" in d


def iter_messages(path: Path, include_sidechain: bool = False) -> Iterator[tuple]:
    """流式产出 (role, ts, text, tools)。text 已剥注入块 / 已丢弃非原话。

    ⚠ Qoder 把**同一条 API 回复按 content block 拆成多行 jsonl**（thinking / text / tool_use
    各占一行，共用 `message.id`，实测无跨轮交错），不合并会得到几百条只有工具调用的空回复。
    这里按 `message.id` 聚合：text 块按出现顺序拼接，tool_use 只收名字。
    """
    buf_key: str | None = None
    buf_ts: object = None
    buf_texts: list[str] = []
    buf_tools: list[str] = []
    buf_err: str | None = None

    def flush():
        text = "\n".join(t for t in buf_texts if t).strip()
        if buf_err and not text:
            text = f"[API 错误] {buf_err}"
        if text or buf_tools:
            return ("assistant", buf_ts, text, list(buf_tools))
        return None

    with io.open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if d.get("isSidechain") and not include_sidechain:
                continue
            if t == "user":
                # 用户消息永远独立成轮，先把挂起的 assistant 吐出来
                if buf_key is not None:
                    item = flush()
                    if item:
                        yield item
                    buf_key, buf_ts, buf_texts, buf_tools, buf_err = None, None, [], [], None
                if not _is_human(d):
                    continue
                text = _clean_user_text(_content_text(d.get("message") or {}))
                if not text or text.startswith(_NOISE_PREFIXES):
                    continue
                yield ("user", d.get("timestamp"), text, [])
            elif t == "assistant":
                msg = d.get("message") or {}
                key = msg.get("id") or d.get("uuid") or ""
                if buf_key is not None and key != buf_key:
                    item = flush()
                    if item:
                        yield item
                    buf_ts, buf_texts, buf_tools, buf_err = None, [], [], None
                if buf_key is None or key != buf_key:
                    buf_key = key
                if buf_ts is None:
                    buf_ts = d.get("timestamp")
                buf_texts.append(_content_text(msg))
                buf_tools.extend(_content_tools(msg))
                if d.get("isApiErrorMessage"):
                    buf_err = str(d.get("error") or d.get("errorDetails") or "未知错误")
    if buf_key is not None:
        item = flush()
        if item:
            yield item


def _title_of(path: Path, limit: int = 45) -> str:
    """标题 = 首条人类消息前 N 字（Qoder 无标题字段）。找不到返回 '(无人类消息)'。"""
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if i > 400:  # 实测首条人类消息恒在前 4 行，留足余量即可
                    break
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not _is_human(d):
                    continue
                text = _clean_user_text(_content_text(d.get("message") or {}))
                if text and not text.startswith(_NOISE_PREFIXES):
                    one = " ".join(text.split())
                    return one[:limit] + ("…" if len(one) > limit else "")
    except OSError:
        pass
    return "(无人类消息)"


def _cwd_of(path: Path) -> str:
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if i > 40:
                    break
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("cwd"):
                    return str(d["cwd"])
    except OSError:
        pass
    return ""


def _deep_hit(path: Path, keywords: list[str]) -> bool:
    """全文扫关键词（标题不中时的兜底，慢）。"""
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if any(k in line for k in keywords):
                    return True
    except OSError:
        return False
    return False


# ========== 子命令 ==========


def cmd_list(args: argparse.Namespace) -> int:
    if not PROJECTS_DIR.is_dir():
        print(f"[ERROR] 没找到 Qoder 会话目录 {PROJECTS_DIR}", file=sys.stderr)
        return 2
    files = sorted(PROJECTS_DIR.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print(f"[ERROR] {PROJECTS_DIR} 下没有 jsonl", file=sys.stderr)
        return 2

    kws = [k.strip() for k in (args.keyword or "").split(",") if k.strip()]
    rows: list[tuple[str, str, float, str, str, str]] = []
    deep_scanned = 0
    for path in files:
        title = _title_of(path)
        if kws:
            hit = any(k.lower() in title.lower() for k in kws)
            if not hit and args.deep:
                deep_scanned += 1
                hit = _deep_hit(path, kws)
            if not hit:
                continue
        stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        size_mb = path.stat().st_size / 1e6
        rows.append((stamp, path.stem[:8], size_mb, _cwd_of(path), title, path.parent.name))
        if args.limit and len(rows) >= args.limit:
            break

    print(f"[根] {ROOT}")
    print(f"{'时间':17s}  {'会话':8s}  {'体积':>7s}  标题  [工作目录]")
    for stamp, sid, size_mb, cwd, title, slug in rows:
        print(f"{stamp}  {sid}  {size_mb:6.1f}M  {title}  [{cwd or slug}]")
    print(f"\n共 {len(rows)} 条（扫描 {len(files)} 个会话文件{'，深度扫正文 ' + str(deep_scanned) + ' 个' if args.deep else ''}）。正文用：dump <会话id前缀>")
    return 0


def _find_session(prefix: str) -> Path | None:
    hits = [p for p in PROJECTS_DIR.glob(f"*/{prefix}*.jsonl")]
    if len(hits) == 1:
        return hits[0]
    if hits:
        print("[WARN] 前缀命中了多个文件：", file=sys.stderr)
        for h in hits:
            print("  ", h, file=sys.stderr)
        return hits[0]
    return None


def _fmt_tools(tools: list[str]) -> str:
    """工具名列表压成一行（同名折叠成 name×N）。"""
    if not tools:
        return ""
    counts: dict[str, int] = {}
    for name in tools:
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(f"{n}×{c}" if c > 1 else n for n, c in counts.items())


def _collect_files(path: Path) -> list[tuple[str, int, object, object]]:
    """从 file-history-snapshot 汇总「改过哪些文件」：路径 → (最高版本, 首见, 末见)。

    这是 Qoder 侧唯一的改动面来源（tool_use 的 file_path 只在逐条内容里，且不覆盖 Write 之外的方式）。
    """
    stats: dict[str, list] = {}
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "file-history-snapshot" not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "file-history-snapshot":
                    continue
                backups = (d.get("snapshot") or {}).get("trackedFileBackups") or {}
                if not isinstance(backups, dict):
                    continue
                for fp, meta in backups.items():
                    m = meta if isinstance(meta, dict) else {}
                    ver = m.get("version")
                    ver = int(ver) if isinstance(ver, (int, float)) else 0
                    bt = m.get("backupTime")
                    cur = stats.get(fp)
                    if cur is None:
                        stats[fp] = [ver, bt, bt]
                    else:
                        if ver > cur[0]:
                            cur[0] = ver
                        if bt:
                            cur[1] = cur[1] or bt
                            cur[2] = bt
    except OSError:
        return []
    rows = [(str(k).replace("\\", "/"), v[0], v[1], v[2]) for k, v in stats.items()]
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def cmd_dump(args: argparse.Namespace) -> int:
    path = _find_session(args.session)
    if path is None:
        print(f"[ERROR] 没找到会话文件：{args.session}*", file=sys.stderr)
        return 2
    size_mb = path.stat().st_size / 1e6
    print(f"[INFO] 源文件 {path} ({size_mb:.1f} MB)")

    kws = [k.strip() for k in (args.keyword or "").split(",") if k.strip()]
    if args.context and not kws:
        print("[WARN] -c/--context 只在配合 -k 时有意义，本次忽略", file=sys.stderr)

    # ① 先按全量口径编号（编号一次定死，后面的过滤一律不重排）
    items: list[tuple[int, str, object, str, list[str]]] = []
    for role, ts, text, tools in iter_messages(path, args.include_sidechain):
        items.append((len(items) + 1, role, ts, text, tools))
    total = len(items)

    # ② 尾部窗口（接手/续做最常看尾部）
    rows = items
    if args.tail and args.tail > 0:
        rows = [r for r in items if r[0] > total - args.tail]

    # ③ 只有工具调用、没有正文的回合默认不占行（否则一个任务几百条空回复）；--tools 时才列
    if not args.tools:
        rows = [r for r in rows if not (r[1] == "assistant" and not r[3])]

    # ④ 关键词 + 上下文：命中条目前后各带 N 条，避免「-k 之后指代链断掉」
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
    out = OUT_DIR / f"{path.stem}_{'user' if args.user_only else 'chat'}.md"
    heading = "用户原话" if args.user_only else "对话还原（用户 + agent）"
    files = _collect_files(path) if args.files else []
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# {path.stem} · {heading}\n\n")
        f.write(f"源：`{path}`（{size_mb:.1f} MB）\n\n全量 {total} 条，本次输出 {len(rows)} 条"
                f"（编号为全量序号，跳号=被过滤）\n\n")
        for idx, role, ts, text, tools in rows:
            note = "（上下文，非命中）" if kws and idx not in marks else ""
            if role == "assistant" and args.clip and len(text) > args.clip:
                text = text[: args.clip].rstrip() + f"…（已截断，原 {len(text)} 字）"
            f.write(f"## [{idx}] {role} {_fmt_ts(ts)} {note}\n\n{text}\n\n")
            if tools and args.tools:
                f.write(f"> 工具：{_fmt_tools(tools)}\n\n")
            f.write("---\n\n")
        if files:
            f.write("## 改动文件（--files，来自 file-history-snapshot，全量口径）\n\n")
            f.write(f"共 {len(files)} 个文件有过备份快照（按版本号倒序；版本号≈改过几次）\n\n")
            for fp, ver, first, last in files:
                f.write(f"- `{fp}` v{ver}  末 {_fmt_ts(last)}\n")
            f.write("\n")
    print(f"[OK] 写入 {out}（{len(rows)}/{total} 条）")

    print("\n首句目录（编号=全量序号；* = 关键词命中，其余为上下文）：")
    for idx, role, ts, text, tools in rows:
        tag = "U" if role == "user" else "A"
        star = "*" if idx in marks else " "
        first = " ".join(text.split())[:90] or ("[工具] " + _fmt_tools(tools))
        print(f" {star}[{idx}] {tag} {len(text):6d}  {_fmt_ts(ts)}  {first}")

    if files:
        print(f"\n改动文件 {len(files)} 个（全量口径）：")
        for fp, ver, first, last in files:
            print(f"  v{ver:<3d} {fp}")
    return 0


def cmd_copy(args: argparse.Namespace) -> int:
    path = _find_session(args.session)
    if path is None:
        print(f"[ERROR] 没找到会话文件：{args.session}*", file=sys.stderr)
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dst = OUT_DIR / path.name
    shutil.copy2(path, dst)
    print(f"[OK] {dst}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，中文输出改成 UTF-8（取不到 reconfigure 就原样跑）
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(prog="qoder_history", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出会话（标题=首条人类消息）")
    p_list.add_argument("-k", "--keyword", default="", help="标题关键词，逗号分隔")
    p_list.add_argument("-n", "--limit", type=int, default=60)
    p_list.add_argument("--deep", action="store_true", help="标题不中时再扫正文（慢）")
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
        help="附加「改过哪些文件」（来自 file-history-snapshot，全量口径）",
    )
    p_dump.add_argument("--user-only", action="store_true", help="只抽用户原话（逐字取证用，丢上下文）")
    p_dump.add_argument("--clip", type=int, default=0, help="agent 回复截断到 N 字（0=不截）")
    p_dump.add_argument("--tools", action="store_true", help="每条 agent 消息末尾列出调用的工具名")
    p_dump.add_argument("--include-sidechain", action="store_true", help="连子代理记录一起抽（默认丢）")
    p_dump.set_defaults(func=cmd_dump)

    p_copy = sub.add_parser("copy", help="只复制 jsonl 到 temp/qoder_hist/")
    p_copy.add_argument("session")
    p_copy.set_defaults(func=cmd_copy)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

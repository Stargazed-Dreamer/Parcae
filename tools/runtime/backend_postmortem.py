#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""后端非正常死亡复盘（postmortem）。

硬杀（TerminateProcess / taskkill /F / 进程树回收）不会给进程任何写日志的机会，
Windows 也不审计"是谁调用的 TerminateProcess"。所以事后只能做两件事：
  1. 定位死亡时刻，判定「优雅关闭 vs 硬杀 vs 崩溃 vs 关机」；
  2. 收集死亡时刻的旁证（系统事件、loop 心跳断点、采集断点、死前活动）。

用法::

    python tools/runtime/backend_postmortem.py            # 分析最近一次死亡
    python tools/runtime/backend_postmortem.py --list     # 列出所有启动/死亡点
    python tools/runtime/backend_postmortem.py --at "2026-10-25 18:23"
    python tools/runtime/backend_postmortem.py --no-events
    python tools/runtime/backend_postmortem.py --json

⚠ 死亡时刻精度：server.log 只记录到"最后一条日志"。若死前 150 分钟无日志，
   真实死亡时间就在最后一行之后、且无法再收窄 —— 报告里会标注这个区间。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "data" / "logs" / "server.log"
TASKS = ROOT / "data" / "loops" / "tasks.json"
RAW = ROOT / "data" / "activity" / "raw"
RUNTIME = ROOT / "data" / "runtime"

START_MARKER = "文件日志已启用"
GRACEFUL = (
    "StreamableHTTP session manager is shutting down",
    "Parcae API 已关闭",
    "关闭步骤:",
    "Finished server process",
    "Application shutdown complete",
    "Shutting down",
)
TS_FMT = "%Y-%m-%d %H:%M:%S"

# 硬杀时"最后一行"的高频形态，用于归因
HARDKILL_SIGNATURES = {
    "ListToolsRequest": "死前正被 MCP 客户端连着（可能是 IDE/会话侧断开时连带清理）",
    "collect_windows": "死在正常 loop 中（外力终止，非自身异常）",
    "screen_vl": "死在正常 loop 中（外力终止，非自身异常）",
}


def _read_tail(path: Path, nbytes: int = 10_000_000) -> list[str]:
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        return f.read().decode("utf-8", errors="replace").splitlines()


def _ts(line: str) -> datetime | None:
    try:
        return datetime.strptime(line[:19], TS_FMT)
    except (ValueError, IndexError):
        return None


def find_deaths(lines: list[str]) -> list[dict]:
    """每两条启动横幅之间 = 一次生命。返回 [(death_idx, start_idx, death_ts, start_ts)]"""
    starts = [i for i, ln in enumerate(lines) if START_MARKER in ln]
    out = []
    for i in starts:
        if i == 0:
            continue  # 文件开头，上一次生命在轮转掉的文件里
        death_line = lines[i - 1]
        out.append(
            {
                "death_idx": i - 1,
                "start_idx": i,
                "death_line": death_line,
                "death_ts": _ts(death_line),
                "start_ts": _ts(lines[i]),
            }
        )
    return out


def classify(lines: list[str], death_idx: int, lookback: int = 60) -> tuple[str, list[str]]:
    """判定死法。lookback 只看死前 N 行，避免把更早的优雅关闭算进来。"""
    window = lines[max(0, death_idx - lookback) : death_idx + 1]
    for ln in window:
        for marker in GRACEFUL:
            if marker in ln:
                # 只认「死前最后一次启动之后」的关闭标记
                return "优雅关闭", [ln]
    return "硬杀（无 shutdown 记录）", []


def query_events(death_ts: datetime, minutes_before: int = 30, minutes_after: int = 5) -> dict:
    """查 Windows 系统/应用日志里的关机、断电、崩溃事件。"""
    start = (death_ts - timedelta(minutes=minutes_before)).strftime("%Y-%m-%d %H:%M:%S")
    end = (death_ts + timedelta(minutes=minutes_after)).strftime("%Y-%m-%d %H:%M:%S")
    ps = f"""
$ids = 41,1074,6005,6006,6008,1000,1001,109,42
$r = Get-WinEvent -FilterHashtable @{{LogName='System'; StartTime='{start}'; EndTime='{end}'}} -ErrorAction SilentlyContinue |
      Where-Object {{ $ids -contains $_.Id }}
if ($r) {{ $r | ForEach-Object {{ "$($_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'))|$($_.Id)|$($_.ProviderName)" }} }}
else {{ 'NONE' }}
"""
    try:
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": str(e)}
    out = (cp.stdout or "").strip().splitlines()
    return {"events": [ln for ln in out if ln and ln != "NONE"], "stderr": (cp.stderr or "").strip()[:300]}


def loop_heartbeats() -> list[dict]:
    """tasks.json 形如 {task_id: {last_run_at: <epoch float>, paused_reason: ...}}"""
    if not TASKS.exists():
        return []
    try:
        data = json.loads(TASKS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = []
    if isinstance(data, dict):
        for tid, t in data.items():
            if not isinstance(t, dict):
                continue
            lr = t.get("last_run_at")
            if isinstance(lr, (int, float)):
                lr = datetime.fromtimestamp(lr).strftime(TS_FMT)
            rows.append(
                {
                    "task_id": tid,
                    "last_run_at": lr,
                    "paused_reason": t.get("paused_reason"),
                    "fail_count": t.get("fail_count"),
                }
            )
    return rows


def collect_gaps(death_ts: datetime) -> dict:
    """窗口/屏幕采集在死亡时刻附近的缺口：死前最后一条 与 死后第一条。"""
    day = death_ts.strftime("%Y%m%d")
    out = {}
    for kind in ("windows", "screen"):
        p = RAW / f"{kind}_{day}.jsonl"
        if not p.exists():
            out[kind] = {"before": None, "after": None, "gap_min": None}
            continue
        before = after = None
        with open(p, "rb") as f:
            for raw in f:
                if b'"ts"' not in raw:
                    continue
                try:
                    ts = datetime.fromisoformat(json.loads(raw.decode("utf-8", "replace"))["ts"])
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                if ts <= death_ts:
                    before = ts
                else:
                    after = ts
                    break
        gap = round((after - before).total_seconds() / 60, 1) if (before and after) else None
        out[kind] = {"before": before, "after": after, "gap_min": gap}
    return out


def pid_info() -> dict:
    pidf = RUNTIME / "server.pid"
    out = {"pid_file": str(pidf), "pid": None, "mtime": None}
    if pidf.exists():
        try:
            out["pid"] = pidf.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        out["mtime"] = datetime.fromtimestamp(pidf.stat().st_mtime).strftime(TS_FMT)
    return out


def analyze(death: dict, lines: list[str], with_events: bool) -> dict:
    manner, evidence = classify(lines, death["death_idx"])
    last_line = death["death_line"]
    sig = next((v for k, v in HARDKILL_SIGNATURES.items() if k in last_line), "未知")

    rep = {
        "死亡时刻(日志末行).": last_line[:19],
        "启动时刻(上次)": death["start_ts"].strftime(TS_FMT) if death["start_ts"] else None,
        "死法": manner,
        "死法证据": evidence,
        "最后一行": last_line.strip()[:200],
        "死前形态": sig,
        "死前 15 行": [ln.strip()[:180] for ln in lines[max(0, death["death_idx"] - 14) : death["death_idx"] + 1]],
    }
    if with_events and death["death_ts"]:
        rep["系统事件"] = query_events(death["death_ts"])
    rep["loop 心跳"] = loop_heartbeats()
    if death["death_ts"]:
        rep["采集断点"] = collect_gaps(death["death_ts"])
    rep["当前 pid 文件"] = pid_info()
    return rep


def print_report(rep: dict) -> None:
    print("=" * 78)
    print("后端死亡复盘")
    print("=" * 78)
    for k in ("死亡时刻(日志末行).", "启动时刻(上次)", "死法", "死前形态"):
        if k in rep:
            print(f"{k:<18}: {rep[k]}")
    print(f"{'最后一行':<18}: {rep['最后一行']}")
    if rep["死法证据"]:
        print(f"{'死法证据':<18}: {rep['死法证据'][0].strip()[:150]}")

    print("\n-- 死前 15 行 --")
    for ln in rep["死前 15 行"]:
        print(f"  {ln}")

    ev = rep.get("系统事件")
    if ev:
        print("\n-- 系统事件（关机/断电/崩溃）--")
        if ev.get("error"):
            print(f"  查询失败: {ev['error']}")
        elif not ev["events"]:
            print("  无 1074/41/6008/1000/1001 ⇒ 机器没关机、没断电、python 没崩溃")
        else:
            for e in ev["events"]:
                print(f"  {e}")

    beats = rep.get("loop 心跳") or []
    if beats:
        print("\n-- loop 心跳（paused_reason 非空 = 曾被自动暂停）--")
        for b in beats:
            flag = f"  ⚠ {b['paused_reason']}" if b["paused_reason"] else ""
            print(f"  {b['task_id']:<42} last_run={b['last_run_at']}{flag}")

    gaps = rep.get("采集断点")
    if gaps:
        print("\n-- 采集断点（死前最后一条 / 死后第一条）--")
        for k in ("windows", "screen"):
            v = gaps.get(k) or {}
            b, a, g = v.get("before"), v.get("after"), v.get("gap_min")
            if not b and not a:
                print(f"  {k:<8} 当天无文件")
                continue
            bs = b.strftime("%H:%M:%S") if b else "—"
            as_ = a.strftime("%H:%M:%S") if a else "—"
            tail = f"   缺口 {g} 分钟" if g is not None else ""
            print(f"  {k:<8} {bs} → {as_}{tail}")

    pf = rep.get("当前 pid 文件")
    if pf:
        print("\n-- 当前实例 --")
        print(f"  pid={pf['pid']}  pid 文件写入于 {pf['mtime']}")


def _delta(iso: str, ref: str) -> str:
    try:
        a = datetime.fromisoformat(iso)
        b = datetime.strptime(ref, TS_FMT)
        sec = int((a - b).total_seconds())
        return f"{sec/60:+.1f} 分钟"
    except (ValueError, TypeError):
        return "?"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="后端非正常死亡复盘")
    ap.add_argument("--at", help='指定死亡时刻附近的时间，如 "2026-10-25 18:23"')
    ap.add_argument("--list", action="store_true", help="列出所有启动/死亡点")
    ap.add_argument("--no-events", action="store_true", help="跳过 Windows 事件查询")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if not LOG.exists():
        print(f"找不到日志: {LOG}", file=sys.stderr)
        return 2

    lines = _read_tail(LOG)
    deaths = find_deaths(lines)
    if not deaths:
        print("日志里没有找到启动横幅，无法定位", file=sys.stderr)
        return 2

    if args.list:
        print(f"共 {len(deaths) + 1} 次启动 / {len(deaths)} 个可分析的死亡点\n")
        for i, d in enumerate(deaths, 1):
            manner, _ = classify(lines, d["death_idx"])
            ts = d["death_ts"].strftime(TS_FMT) if d["death_ts"] else "?"
            print(f"{i:>3}. 死于 {ts}  [{manner}]  最后一行: {d['death_line'][:110]}")
        return 0

    target = deaths[-1]
    if args.at:
        want = args.at.strip()[:16]
        cand = [d for d in deaths if d["death_ts"] and d["death_ts"].strftime("%Y-%m-%d %H:%M") == want]
        if not cand:
            cand = [d for d in deaths if d["death_ts"] and d["death_ts"].strftime("%Y-%m-%d %H:%M").startswith(args.at.strip()[:13])]
        if not cand:
            print(f"没有匹配 '{args.at}' 的死亡点，用 --list 看看", file=sys.stderr)
            return 2
        target = cand[-1]

    rep = analyze(target, lines, with_events=not args.no_events)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(rep)
        print("\n提示：想知道「谁杀的」，靠这份报告不够 —— 需要进程活着时记下父进程链。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

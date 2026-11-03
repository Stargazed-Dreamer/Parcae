# -*- coding: utf-8 -*-
"""SSD 写入 × 使用强度 —— 把 ssd_wear_log.csv 折成"能不能怪 agent"的证据表。

只做算术，不做判决：输出每窗口写入速率、harness 平均核数、日志写入量，
以及三者的相关性和分桶中位数。真假、强弱由人看数字下结论（脚本的 hard verdict
对付不了"哪些窗口在干别的事"这类活问题）。

用法：
    uv run python tools/disk_io_audit/ssd_wear_report.py
    uv run python tools/disk_io_audit/ssd_wear_report.py --html temp/ssd_report.html
    uv run python tools/disk_io_audit/ssd_wear_report.py --since 2026-11-03
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CSV = os.path.join(_ROOT, "workspace", "disk_manager", "ssd_wear_log.csv")

CORES_BUCKETS: list[tuple[str, float, float]] = [
    ("≈0（<0.05 核）", 0.0, 0.05),
    ("0.05–0.3 核", 0.05, 0.3),
    ("0.3–1 核", 0.3, 1.0),
    ("1–2 核", 1.0, 2.0),
    ("≥2 核", 2.0, float("inf")),
]


def fnum(row: dict, key: str) -> float | None:
    v = row.get(key)
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_windows(csv_path: str, disk: int, min_win: int) -> list[dict]:
    """读 CSV → 每窗口一条（含派生量）。dw 缺失/窗口过短的行丢弃。"""
    out: list[dict] = []
    if not os.path.exists(csv_path):
        return out
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                if int(row.get("disk", -1)) != disk:
                    continue
            except (TypeError, ValueError):
                continue
            dw = fnum(row, "dw_bytes")
            win = fnum(row, "win_secs")
            if dw is None or win is None or win < min_win:
                continue
            hours = win / 3600.0
            harness = {}
            raw = row.get("harness_json") or ""
            if raw:
                try:
                    harness = json.loads(raw)
                except ValueError:
                    harness = {}
            log_wr = sum(float(g.get("log_wr_mb") or 0) for g in harness.values())
            log_n = sum(int(g.get("log_n") or 0) for g in harness.values())
            cores = fnum(row, "cores")
            out.append({
                "epoch": int(float(row["epoch"])),
                "ts": row.get("ts", ""),
                "gb": dw / 1e9,
                "hours": hours,
                "gb_per_h": dw / 1e9 / hours,
                "cores": cores if cores is not None else 0.0,
                "cpu_s": fnum(row, "cpu_s") or 0.0,
                "log_wr_mb": log_wr,
                "log_n": log_n,
                "harness": harness,
            })
    out.sort(key=lambda r: r["epoch"])
    return out


def median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    rk = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            rk[order[k]] = avg
        i = j + 1
    return rk


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def bucket_of(cores: float) -> str:
    for name, lo, hi in CORES_BUCKETS:
        if lo <= cores < hi:
            return name
    return CORES_BUCKETS[-1][0]


def aggregate(wins: list[dict]) -> dict:
    days: dict[str, dict] = {}
    for w in wins:
        day = dt.datetime.fromtimestamp(w["epoch"]).strftime("%Y-%m-%d")
        d = days.setdefault(day, {"gb": 0.0, "hours": 0.0, "cpu_s": 0.0,
                                  "log_wr": 0.0, "n": 0, "max_gb_h": 0.0, "max_cores": 0.0})
        d["gb"] += w["gb"]
        d["hours"] += w["hours"]
        d["cpu_s"] += w["cpu_s"]
        d["log_wr"] += w["log_wr_mb"]
        d["n"] += 1
        d["max_gb_h"] = max(d["max_gb_h"], w["gb_per_h"])
        d["max_cores"] = max(d["max_cores"], w["cores"])
    return days


def main() -> int:
    ap = argparse.ArgumentParser(description="SSD 写入 × 使用强度 汇总")
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--disk", type=int, default=0)
    ap.add_argument("--min-win", type=int, default=600, help="只用窗口 ≥N 秒的行（默认 10 分钟）")
    ap.add_argument("--since", default="", help="只统计该日期之后（YYYY-MM-DD）")
    ap.add_argument("--html", default="", help="另存一张散点图 HTML")
    args = ap.parse_args()

    wins = load_windows(args.csv, args.disk, args.min_win)
    if args.since:
        cut = dt.datetime.strptime(args.since, "%Y-%m-%d").timestamp()
        wins = [w for w in wins if w["epoch"] >= cut]

    if not wins:
        print("没有可用窗口（CSV: %s，disk=%d，min_win=%ds）" % (args.csv, args.disk, args.min_win))
        print("提示：先让采样器跑几轮（loop 任务 disk_manager.ssd_wear 每 30 分钟一行）。")
        return 1

    span = wins[-1]["epoch"] - wins[0]["epoch"]
    covered = sum(w["hours"] for w in wins)
    total_gb = sum(w["gb"] for w in wins)
    print("== 覆盖 %d 个窗口 / %s ~ %s ==" % (
        len(wins),
        dt.datetime.fromtimestamp(wins[0]["epoch"]).strftime("%m-%d %H:%M"),
        dt.datetime.fromtimestamp(wins[-1]["epoch"]).strftime("%m-%d %H:%M")))
    print("   窗口合计 %.1f h（墙钟跨度 %.1f h，覆盖率 %.0f%%）  写入合计 %.1f GB"
          % (covered, span / 3600.0, 100.0 * covered / max(span / 3600.0, 1e-9), total_gb))

    xs = [w["cores"] for w in wins]
    ys = [w["gb_per_h"] for w in wins]
    med_gb_h = median(ys)
    med_cores = median(xs)
    pr = pearson(xs, ys)
    sr = pearson(ranks(xs), ranks(ys))
    print("\n== 相关性（窗口级，n=%d）==" % len(wins))
    print("   写入速率 中位 %.1f GB/h   均值 %.1f GB/h   最大 %.1f GB/h"
          % (med_gb_h or 0, sum(ys) / len(ys), max(ys)))
    print("   harness 核数 中位 %.2f 核   均值 %.2f 核   最大 %.2f 核"
          % (med_cores or 0, sum(xs) / len(xs), max(xs)))
    print("   Pearson r = %s      Spearman ρ = %s"
          % ("n/a" if pr is None else "%.3f" % pr, "n/a" if sr is None else "%.3f" % sr))
    print("   （r 只说明同向变化，不说明因果；要看是否同向、幅度是否成比例——见下表分桶）")

    print("\n== 按 harness 核数分桶（看写入是否真跟着负载走）==")
    print("   %-16s %5s %10s %10s %10s %10s" % ("核数区间", "窗口", "写入GB", "中位GB/h", "均值GB/h", "日志写入MB/窗口"))
    for name, lo, hi in CORES_BUCKETS:
        sel = [w for w in wins if lo <= w["cores"] < hi]
        if not sel:
            print("   %-16s %5d %10s %10s %10s %10s" % (name, 0, "-", "-", "-", "-"))
            continue
        print("   %-16s %5d %10.1f %10.1f %10.1f %10.1f"
              % (name, len(sel), sum(w["gb"] for w in sel),
                 median([w["gb_per_h"] for w in sel]) or 0,
                 sum(w["gb_per_h"] for w in sel) / len(sel),
                 sum(w["log_wr_mb"] for w in sel) / len(sel)))

    top = sorted(wins, key=lambda w: w["gb_per_h"], reverse=True)[:8]
    print("\n== 写入速率最高的 8 个窗口 ==")
    print("   %-17s %9s %8s %8s %9s" % ("时间", "GB/h", "核数", "日志文件", "日志MB"))
    for w in top:
        print("   %-17s %9.1f %8.2f %8d %9.1f"
              % (dt.datetime.fromtimestamp(w["epoch"]).strftime("%m-%d %H:%M"), w["gb_per_h"],
                 w["cores"], w["log_n"], w["log_wr_mb"]))

    zero_cores_hot = [w for w in wins if w["cores"] < 0.05 and w["gb_per_h"] > (med_gb_h or 0) * 2]
    print("\n== 需要解释的窗口：核数≈0 但写入速率 > 中位数 2 倍 ==")
    if not zero_cores_hot:
        print("   无（高写入窗口都有对应的 harness 负载）")
    else:
        print("   共 %d 个，合计 %.1f GB（占总量 %.0f%%）—— 这些不是 agent 写的，要另找来源"
              % (len(zero_cores_hot), sum(w["gb"] for w in zero_cores_hot),
                 100.0 * sum(w["gb"] for w in zero_cores_hot) / total_gb))
        for w in sorted(zero_cores_hot, key=lambda w: w["gb"], reverse=True)[:8]:
            print("     %s  %7.1f GB  %.1f GB/h  日志 %d 个/%.1f MB"
                  % (dt.datetime.fromtimestamp(w["epoch"]).strftime("%m-%d %H:%M"),
                     w["gb"], w["gb_per_h"], w["log_n"], w["log_wr_mb"]))

    days = aggregate(wins)
    print("\n== 每天 ==")
    print("   %-12s %5s %9s %9s %9s %8s" % ("日期", "窗口", "写入GB", "折GB/天", "最高GB/h", "最高核"))
    for day in sorted(days):
        d = days[day]
        per_day = d["gb"] / (d["hours"] / 24.0) if d["hours"] > 0 else 0
        print("   %-12s %5d %9.1f %9.1f %9.1f %8.2f"
              % (day, d["n"], d["gb"], per_day, d["max_gb_h"], d["max_cores"]))

    if args.html:
        write_html(args.html, wins, days, pr, sr)
        print("\n散点图已写入: %s" % args.html)
    return 0


def write_html(path: str, wins: list[dict], days: dict[str, dict],
               pr: float | None, sr: float | None) -> None:
    """纯 SVG 散点（不引第三方库）：x=harness 核数, y=写入 GB/h。"""
    W, H, PAD = 760, 380, 56
    xmax = max([w["cores"] for w in wins] + [1.0]) * 1.1
    ymax = max([w["gb_per_h"] for w in wins] + [1.0]) * 1.1

    def sx(v: float) -> float:
        return PAD + (W - 2 * PAD) * v / xmax

    def sy(v: float) -> float:
        return H - PAD - (H - 2 * PAD) * v / ymax

    pts = []
    for w in wins:
        r = 3 + min(6.0, w["gb"] ** 0.5)
        pts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="#2f6fdb" fill-opacity="0.45" '
                   'stroke="#1d4fa8" stroke-width="0.5"><title>%s  %.1f GB/h  %.2f 核  %.1f GB</title></circle>'
                   % (sx(w["cores"]), sy(min(w["gb_per_h"], ymax)), r,
                      dt.datetime.fromtimestamp(w["epoch"]).strftime("%m-%d %H:%M"),
                      w["gb_per_h"], w["cores"], w["gb"]))
    ticks = []
    for i in range(6):
        xv = xmax * i / 5
        yv = ymax * i / 5
        ticks.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#e3e6ea"/>'
                     % (sx(xv), PAD, sx(xv), H - PAD))
        ticks.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#e3e6ea"/>'
                     % (PAD, sy(yv), W - PAD, sy(yv)))
        ticks.append('<text x="%.1f" y="%.1f" font-size="11" fill="#5b6470" text-anchor="middle">%.1f</text>'
                     % (sx(xv), H - PAD + 16, xv))
        ticks.append('<text x="%.1f" y="%.1f" font-size="11" fill="#5b6470" text-anchor="end">%.0f</text>'
                     % (PAD - 8, sy(yv) + 4, yv))

    daily_rows = "".join(
        "<tr><td>%s</td><td>%d</td><td>%.1f</td><td>%.1f</td><td>%.2f</td></tr>"
        % (d, days[d]["n"], days[d]["gb"],
           days[d]["gb"] / (days[d]["hours"] / 24.0) if days[d]["hours"] else 0,
           days[d]["max_cores"])
        for d in sorted(days))

    html = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>SSD 写入 × 使用强度</title>
<style>
body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:32px;color:#1c2430;background:#fbfbfc}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#5b6470;font-size:13px;margin-bottom:20px}}
.card{{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:16px 18px;margin-bottom:18px}}
table{{border-collapse:collapse;font-size:13px}} th,td{{border-bottom:1px solid #eceff2;padding:6px 14px;text-align:right}}
th:first-child,td:first-child{{text-align:left}}
.k{{font-variant-numeric:tabular-nums}}
</style>
<h1>SSD 写入 × 使用强度</h1>
<div class="sub">{n} 个窗口　Pearson r = {pr}　Spearman ρ = {sr}　点大小=窗口写入量</div>
<div class="card"><svg viewBox="0 0 {W} {H}" width="100%" height="auto">{ticks}{pts}
<text x="{cx}" y="{base}" font-size="12" fill="#333" text-anchor="middle">harness 平均核数（窗口内 CPU 秒 / 窗口秒）</text>
<text x="14" y="{cy}" font-size="12" fill="#333" transform="rotate(-90 14 {cy})" text-anchor="middle">写入 GB/小时</text>
</svg></div>
<div class="card"><table><tr><th>日期</th><th>窗口</th><th>写入 GB</th><th>折 GB/天</th><th>最高核数</th></tr>{rows}</table></div>
</html>""".format(n=len(wins), pr="n/a" if pr is None else "%.3f" % pr,
                 sr="n/a" if sr is None else "%.3f" % sr,
                 W=W, H=H, ticks="".join(ticks), pts="".join(pts),
                 cx=W / 2, cy=H / 2, base=H - 8, rows=daily_rows)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)


if __name__ == "__main__":
    sys.exit(main())

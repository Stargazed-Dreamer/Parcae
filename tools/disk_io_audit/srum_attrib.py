# -*- coding: utf-8 -*-
"""SRUM 历史磁盘 I/O 归因：某段时期内谁在高强度读写。

数据源：SRUDB.dat 的 application 表（{D10CA2FE-...}），每应用 × 每小时记录
前台/后台 读写字节数 + 读写次数。保留期通常 1 年以上（本库实测从 2024-12 起）。

用法：
  1. 先用管理员拷出数据库（esentutl /y 可读被锁的 ESE 文件）：
     esentutl /y C:\\Windows\\System32\\sru\\SRUDB.dat /d temp\\srum\\SRUDB.dat /o
  2. 依赖 dissect.esedb（纯 Python，约 3 MB）。**本项目 venv 里不装它**，装到任意独立目录，
     再用 --lib 告诉脚本：
       python -m pip install --target D:\\libs\\srum dissect.esedb
  3. 跑：uv run python tools/disk_io_audit/srum_attrib.py --db temp/srum/SRUDB.dat --lib D:\\libs\\srum
     （--lib 可重复；不加 --lib 时按 venv 正常 import，装进 venv 的人可以直接跑）

输出（写到 --db 同目录）：
  - srum_disk_io_totals.csv   每应用总读/写/次数/活跃小时
  - srum_disk_io_timeline.csv 每小时全局读写（用于定位"哪天/哪小时"有尖峰）
  - SRUM_DISK_IO_REPORT.md    报告（含指定时间窗 Top 榜）
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from collections import defaultdict
from typing import Any, Callable

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))

ConvFn = Callable[[int], dt.datetime]


def load_dissect(lib_dirs: list[str]) -> tuple[Any, tuple[ConvFn, ConvFn], Callable[[bytes], Any]]:
    """导入 dissect 三件套；失败时给出**确切**的安装姿势而不是裸 ImportError。

    刻意不把 dissect 写进 pyproject：它只有这个 SRUM 解析器用得到，而 SRUM 取证是偶发动作。
    历史上这里写过 `sys.path.insert(0, temp/srum_attrib_20261002/lib)`，temp 会被清理 ⇒
    脚本"看起来能跑"却在下次直接崩，所以改成显式 --lib。
    """
    for d in lib_dirs:
        abs_d = os.path.abspath(d)  # 相对路径会在 import 那一刻按 cwd 定，先归一化
        if abs_d not in sys.path:
            sys.path.insert(0, abs_d)
    try:
        # 这三行静态解析不到是**预期**：dissect 刻意不装进项目 venv，运行时由 --lib 注入目录。
        # 用逐行 ignore 而不是把包加进依赖来骗过 pyright。
        from dissect.esedb import EseDB  # pyright: ignore[reportMissingImports]
        from dissect.util.sid import read_sid  # pyright: ignore[reportMissingImports]
        from dissect.util.ts import oatimestamp, wintimestamp  # pyright: ignore[reportMissingImports]
    except ImportError as exc:  # 依赖缺失是预期分支，报成"怎么用"而不是栈
        raise SystemExit(
            "缺少 dissect.esedb（SRUM 解析库），本项目 venv 有意不装它。\n"
            "装到任意独立目录后用 --lib 传进来：\n"
            "  python -m pip install --target <lib目录> dissect.esedb\n"
            "  uv run python tools/disk_io_audit/srum_attrib.py --db <SRUDB.dat> --lib <lib目录>\n"
            f"（已试过的 --lib：{lib_dirs or '（无）'}；导入错误：{exc}）"
        ) from exc
    return EseDB, (oatimestamp, wintimestamp), read_sid


def parse_ts(raw: int, convs: tuple[ConvFn, ConvFn]) -> dt.datetime | None:
    """SRUM TimeStamp 有两种编码并存（OLE date / FILETIME），回退解析。

    用年份合法区间区分，避免把 FILETIME 当 OLE 解出离谱日期。
    """
    for fn in convs:
        try:
            v = fn(raw)
        except Exception:
            continue
        if v and dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc) <= v <= dt.datetime(2035, 1, 1, tzinfo=dt.timezone.utc):
            return v
    return None

TZ = dt.timezone(dt.timedelta(hours=8))  # GMT+8

OUT_TOTALS = OUT_TIMELINE = OUT_REPORT = None  # main() 里按 --db 所在目录解析


def fmt_b(n: float) -> str:
    for unit, div in (("B", 1), ("KiB", 1024), ("MiB", 1024**2), ("GiB", 1024**3), ("TiB", 1024**4)):
        if abs(n) < div * 1024 or unit == "TiB":
            return f"{n / div:,.2f} {unit}"
    return "?"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="本地时间起，如 2026-09-25 或 2026-09-25T00:00")
    ap.add_argument("--until", help="本地时间止（含）")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--db", required=True,
                    help="esentutl /y 拷出的 SRUDB.dat 路径（输出也写在该目录）；不给默认值——"
                         "副本每次都要现场拷，写死默认路径等于指向一个会被清掉的 temp 文件")
    ap.add_argument("--lib", action="append", default=[],
                    help="dissect.esedb 的独立安装目录（可重复）；已装进当前 venv 就不必给")
    args = ap.parse_args()

    EseDB, convs, read_sid = load_dissect(args.lib)

    global OUT_TOTALS, OUT_TIMELINE, OUT_REPORT
    db_dir = os.path.dirname(os.path.abspath(args.db))
    OUT_TOTALS = os.path.join(db_dir, "srum_disk_io_totals.csv")
    OUT_TIMELINE = os.path.join(db_dir, "srum_disk_io_timeline.csv")
    OUT_REPORT = os.path.join(db_dir, "SRUM_DISK_IO_REPORT.md")

    since = until = None
    if args.since:
        since = dt.datetime.fromisoformat(args.since).replace(tzinfo=TZ)
    if args.until:
        until = dt.datetime.fromisoformat(args.until).replace(tzinfo=TZ)
        if len(args.until) == 10:  # 纯日期 → 扩到当天 23:59:59
            until = until + dt.timedelta(hours=23, minutes=59, seconds=59)

    db = EseDB(open(args.db, "rb"))
    tbl = db.table("{D10CA2FE-6FCF-4F6D-848E-B2E99266FA89}")
    cols = tbl.column_names
    print("列:", ", ".join(cols))

    # SruDbIdMapTable：AppId 索引 → 应用名 / 用户 SID
    idmap = db.table("SruDbIdMapTable")
    id2name: dict[int, str] = {}
    id2sid: dict[int, str] = {}
    for rec in idmap.records():
        idx = rec.get("IdIndex")
        blob = rec.get("IdBlob")
        if not blob:
            continue
        try:
            if rec.get("IdType") in (0, 1, 2):
                id2name[idx] = blob.decode("utf-16-le").rstrip("\x00")
            else:
                id2sid[idx] = str(read_sid(blob))
        except Exception:
            continue
    print(f"id_map: {len(id2name)} 个应用 / {len(id2sid)} 个用户")

    # ⛔ 同一个镜像可能有多个 AppId，且**只是大小写不同**（2026-10-03 实测：
    #    8484 个 AppId 里有 642 组重名，某 7 小时窗内 28.2% 的写入被拆到两行，
    #    QQ/chrome/WorkBuddy 各自被劈成两半）⇒ 按 AppId 聚合会系统性低估。
    #    故聚合键用小写名，展示时保留首次见到的原始大小写。
    key2name: dict[str, str] = {}

    def app_key(idx: int) -> str:
        name = id2name.get(idx, f"<id {idx}>")
        k = name.lower()
        key2name.setdefault(k, name)
        return k

    # 聚合容器
    agg: dict[str, list] = {}  # app_key -> [W, R, w_ops, r_ops, rows, min_ts, max_ts]
    app_hours: dict[str, set] = defaultdict(set)  # app_key -> 去重小时（SRUM 同小时可能多行）
    hourly: dict[dt.datetime, list] = defaultdict(lambda: [0, 0])  # ts -> [W, R]
    n_rows = 0
    ts_min = ts_max = None

    for rec in tbl.records():
        ts_raw = rec.get("TimeStamp")
        if not ts_raw:
            continue
        ts = parse_ts(ts_raw, convs)
        if ts is None:
            continue  # 无法解析的脏数据行
        ts = ts.astimezone(TZ)  # parse_ts 返回 UTC ⇒ 统一转本地再分桶/展示
        if ts_min is None or ts < ts_min:
            ts_min = ts
        if ts_max is None or ts > ts_max:
            ts_max = ts
        if since and ts < since:
            continue
        if until and ts > until:
            continue
        n_rows += 1
        app = app_key(rec.get("AppId"))
        w = (rec.get("ForegroundBytesWritten") or 0) + (rec.get("BackgroundBytesWritten") or 0)
        r = (rec.get("ForegroundBytesRead") or 0) + (rec.get("BackgroundBytesRead") or 0)
        wo = (rec.get("ForegroundNumWriteOperations") or 0) + (rec.get("BackgroundNumWriteOperations") or 0)
        ro = (rec.get("ForegroundNumReadOperations") or 0) + (rec.get("BackgroundNumReadOperations") or 0)
        a = agg.get(app)
        if a is None:
            agg[app] = [w, r, wo, ro, 1, ts, ts]
        else:
            a[0] += w
            a[1] += r
            a[2] += wo
            a[3] += ro
            a[4] += 1
            if ts < a[5]:
                a[5] = ts
            if ts > a[6]:
                a[6] = ts
        app_hours[app].add(ts.replace(minute=0))
        h = hourly[ts.replace(minute=0)]
        h[0] += w
        h[1] += r

    if ts_min is None or ts_max is None:
        # 两者同时赋值，为空就是"窗内一条都没有"；判据写全是为了让类型收窄也成立
        print("!! 没有可用记录（application 表为空，或 --since/--until 把窗口筛空了）")
        sys.exit(1)

    print(f"覆盖范围: {ts_min:%Y-%m-%d %H:%M} ~ {ts_max:%Y-%m-%d %H:%M} (GMT+8)，"
          f"窗内行数 {n_rows}，应用数 {len(agg)}")

    def name_of(key: str) -> str:
        return key2name.get(key, key)

    rows = sorted(agg.items(), key=lambda kv: -kv[1][0])

    # CSV：每应用总量
    with open(OUT_TOTALS, "w", newline="", encoding="utf-8-sig") as f:
        wtr = csv.writer(f)
        wtr.writerow(["应用", "写入总量", "读出总量", "写次数", "读次数", "活跃小时数", "最早", "最晚"])
        for k, a in rows:
            wtr.writerow([name_of(k), a[0], a[1], a[2], a[3], len(app_hours[k]),
                          f"{a[5]:%Y-%m-%d %H:%M}", f"{a[6]:%Y-%m-%d %H:%M}"])

    # CSV：每小时时间线
    with open(OUT_TIMELINE, "w", newline="", encoding="utf-8-sig") as f:
        wtr = csv.writer(f)
        wtr.writerow(["小时(GMT+8)", "写入", "读出"])
        for ts in sorted(hourly):
            w, r = hourly[ts]
            wtr.writerow([f"{ts:%Y-%m-%d %H:00}", w, r])

    # 按天聚合（写入 Top，用于看"哪天猛"）
    daily: dict[str, float] = defaultdict(float)
    for ts, (w, _r) in hourly.items():
        daily[f"{ts:%Y-%m-%d}"] += w
    top_days = sorted(daily.items(), key=lambda kv: -kv[1])[:10]

    span_days = max((ts_max - ts_min).total_seconds() / 86400, 1e-9)
    total_w = sum(a[0] for _, a in rows)
    total_r = sum(a[1] for _, a in rows)

    lines = []
    lines.append(f"# SRUM 历史磁盘 I/O 归因报告")
    lines.append("")
    lines.append(f"- 数据源：`C:\\Windows\\System32\\sru\\SRUDB.dat`（application 表，系统每小时记账）")
    lines.append(f"- 覆盖范围：**{ts_min:%Y-%m-%d %H:%M} ~ {ts_max:%Y-%m-%d %H:%M}**（GMT+8，共 {span_days:.0f} 天）")
    lines.append(f"- 时间窗：{f'{since:%Y-%m-%d %H:%M}' if since else '（全库起点）'} ~ "
                 f"{f'{until:%Y-%m-%d %H:%M}' if until else '（最新）'}；窗内 {n_rows} 条 / {len(agg)} 个应用")
    lines.append(f"- 窗内合计：**写 {fmt_b(total_w)}**，读 {fmt_b(total_r)}")
    lines.append("")
    lines.append(f"## 写入 Top {args.top}（整窗累计）")
    lines.append("")
    lines.append("| # | 应用 | 写入 | 读出 | 写次数 | 活跃小时 | 首~末 |")
    lines.append("|---|------|------|------|--------|----------|--------|")
    for i, (k, a) in enumerate(rows[: args.top], 1):
        lines.append(f"| {i} | `{name_of(k)}` | {fmt_b(a[0])} | {fmt_b(a[1])} | {a[2]:,} | "
                     f"{len(app_hours[k])} | {a[5]:%m-%d}~{a[6]:%m-%d} |")
    lines.append("")
    lines.append(f"## 读出 Top {args.top}")
    lines.append("")
    lines.append("| # | 应用 | 读出 | 写入 | 读次数 |")
    lines.append("|---|------|------|------|--------|")
    for i, (k, a) in enumerate(sorted(rows, key=lambda kv: -kv[1][1])[: args.top], 1):
        lines.append(f"| {i} | `{name_of(k)}` | {fmt_b(a[1])} | {fmt_b(a[0])} | {a[3]:,} |")
    lines.append("")
    lines.append("## 写入最多的 10 天")
    lines.append("")
    lines.append("| 日期 | 写入 |")
    lines.append("|------|------|")
    for d, w in top_days:
        lines.append(f"| {d} | {fmt_b(w)} |")
    lines.append("")
    lines.append("> 口径说明：SRUM 每小时记一条「该小时内该应用的累计量」，与进程实测计数器同源"
                 "（ETW AppResource），含前台+后台；服务/系统应用可能显示为 UWP 包名。")
    with open(OUT_REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("报告:", OUT_REPORT)
    print("CSV :", OUT_TOTALS)
    print("CSV :", OUT_TIMELINE)


if __name__ == "__main__":
    main()

"""分批运行测试：每个批次一个独立 pytest 进程，慢目录可并行，批间护内存。

为什么要有这个工具（而不是一次性 pytest tests/）：
- 单进程跑全量在本机会把 RSS 推到 11-12 GB，可用内存常只有 8-9 GB，会被系统保护打断；
  分批 = 每批跑完即释放，峰值受控。
- 全量里真正慢的是几个"真起 GUI / 真连 CDP:9222"的 e2e 目录，它们彼此不共享进程状态，
  可以并行；其余小目录串行更省（进程启动开销比收益大）。

用法:
    uv run python tools/run_tests_batched.py                 # full 层，慢目录并行 2 路
    uv run python tools/run_tests_batched.py --quick         # quick 层（排除外部资源 marker）
    uv run python tools/run_tests_batched.py --serial        # 全部串行（对照/排障用）
    uv run python tools/run_tests_batched.py --parallel 3    # 改并行度
    uv run python tools/run_tests_batched.py --only tests/browser

产出（temp/batch_tests/）:
    <batch>.log / <batch>.xml   每批原始输出与 JUnit XML。rc=0 的批次解析完 XML 后自删，
                                所以全绿的一轮跑完这里只剩 SUMMARY.md + mem_min.log；
                                非零批次的两份保留（SUMMARY.md 的明细行直接链到 .log）。
                                上一轮遗留、本轮已无对应批次的产物（测试目录改名/删除）起跑时送回收站。
    mem_min.log                 每 5s 采样的可用内存新低（含并行重叠期），边跑边写
    SUMMARY.md                  批次表 + 合并后的失败清单 + 内存谷值与总耗时
                                （`--only` 部分重跑改写 SUMMARY.partial.md，不覆盖整轮那份）

退出码：0=全部批次 rc=0；1=有批次非零（含进程崩溃，绝不当成"0 失败"）。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_runner import QUICK_MARKERS, classify_exit, parse_junit_xml  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "temp" / "batch_tests"

# 慢目录（2026-09-28 实测：approval_screen 152s / browser 167s / recorder+client_ui 225s）。
# 判定依据是单批墙钟时间，不是测试条数——纯逻辑目录几百条也只跑几秒。
SLOW_BATCHES = ("tests/approval_screen", "tests/browser", "tests/client_ui", "workspace/recorder/tests")

# 独占批：在飞时不允许任何别的批与它并行（不是"彼此不并行"，是"跟谁都不并行"）。
# 这两批都会真动本机桌面：tests/approval_screen 用 in-process TestClient 直接执行服务端
# 代码，SetForegroundWindow / SendInput 是真的发生；tests/browser 驱动 9222 上的 Chrome。
# 2026-10-03 发版全量实测：两批同段并行 → browser 批 1 条真红 + 88 个静默 skip（同一批用例
# 串行跑是 362 passed / 3 skipped / 112s），approval_screen 批 1 条真红。skip 的成因是
# conftest 的 CDP 探针每用例一次、timeout=2，回环连接被拖慢就被写成"浏览器没在跑"。
# 另记 2026-09-29 的 recorder ∥ browser 假红（那条改的是测试侧轮询姿势）。
SOLO_BATCHES = ("tests/approval_screen", "tests/browser")

# 默认与全量不跑的批次（要单跑用 --only 显式选中，见 main）。
# workspace/recorder/tests：1190 例 / ~225s 的重批，且反复占掉发版全量的长尾——
# 2026-09-28 崩在 34%（Qt 收割二次删除）、2026-10-03 recorder ∥ browser 假红。
EXCLUDED_BATCHES = ("workspace/recorder/tests",)

# 内存感知并发（2026-10-04 用户要求提速，替代旧的"慢目录固定 2 路 + 其余全部串行"）。
# 旧策略只并行 4 个慢批里的 2 个，20 多个小批全串行——14GB 可用内存却只跑 1-2 路，慢在这。
# 现每个批按预估峰值记账，启动前要求 (可用 - 在飞承诺) >= 余量 + 本批成本，内存富余时自然
# 多开（这是"智能分配"），紧张时退化成 1 路。
MEM_RESERVE_GB = 4.0      # 系统余量红线：低于它不再启新批
COST_GB_SLOW = 2.5        # 重 GUI / 真连浏览器的批，峰值预估
COST_GB_NORMAL = 1.0      # 纯逻辑批
MAX_IN_FLIGHT_DEFAULT = 6
WAIT_LIMIT_S = 900        # 等内存上限：等过头强放行，绝不把整个发版卡死

MEM_LOG = OUT_DIR / "mem_min.log"
_IN_FLIGHT = 0   # 只在飞时用于给采样行标注"当时几路在跑"，诊断用途不要求精确

# 并发槽位记账（只被 _acquire_slot / _release_slot 读写；不要求精确，只求保守）
_slot_lock = threading.Lock()
_inflight_cost_gb = 0.0
_inflight_count = 0
_max_in_flight = MAX_IN_FLIGHT_DEFAULT


def _mem_sampler(stop: threading.Event) -> None:
    """每 5s 采一次可用内存，只在出现新低时追加一行。

    为什么要单独采样：`_run_batch` 的 avail_before/avail_after 是**每批各自**的端点，
    挡不住并行段两批重叠期的叠加占用；而且报告在收尾才写，跑到一半被杀掉就什么都留不下。
    """
    lowest = float("inf")
    while not stop.is_set():
        avail = psutil.virtual_memory().available / 2**30
        if avail < lowest:
            lowest = avail
            with open(MEM_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} avail={avail:.2f} in_flight={_IN_FLIGHT}\n")
        stop.wait(5)


def _has_tests(p: Path) -> bool:
    """目录里是否真有 test_*.py（tests/fixtures 只放夹具，当测试目录跑会得到 rc=5 假红）。"""
    return any(p.rglob("test_*.py"))


def discover_batches(include_workspace: bool = True) -> list[tuple[str, str]]:
    """返回 [(批次标签, pytest 路径), ...]：tests/ 各子目录 + tests/ 根文件 + 各组件测试目录。"""
    batches: list[tuple[str, str]] = []
    tdir = PROJECT_ROOT / "tests"
    for sub in sorted(p for p in tdir.iterdir() if p.is_dir() and p.name != "__pycache__"):
        if _has_tests(sub):
            batches.append((f"tests/{sub.name}", f"tests/{sub.name}"))
    if any(tdir.glob("test_*.py")):
        # ⚠️ 根平铺文件必须逐个成批：早期版本这里写的是 "tests"，等于把整棵 tests/ 又跑一遍
        # （27 批里冒出个 27 分钟的巨无霸，还把分批省下的内存护栏整个抵消掉）。
        for f in sorted(tdir.glob("test_*.py")):
            batches.append((f"tests/{f.name}", f"tests/{f.name}"))
    if include_workspace:
        sys.path.insert(0, str(PROJECT_ROOT))
        from lib.component_manifest import collect_test_dirs

        for d in collect_test_dirs():
            rel = Path(d).relative_to(PROJECT_ROOT).as_posix()
            batches.append((rel, rel))
    return batches


def _cost_gb_for(label: str) -> float:
    """该批的峰值内存预估（GB），供并发槽位记账。

    按"会不会真起 GUI / 真连浏览器"给，不按目录名或测试条数给——纯逻辑目录几百条也只吃
    一两百 MB，拿同一个数会把并发卡死。
    """
    return COST_GB_SLOW if label in SLOW_BATCHES else COST_GB_NORMAL


def _acquire_slot(cost_gb: float) -> float:
    """等内存槽位，返回实际等待秒数。

    判据用 (可用 - 在飞承诺)，不是只看可用：可用是实时值，十个批同时看到 14GB 会一起放行、
    一起把内存吃穿；把"已承诺但尚未体现在可用里"的部分扣掉才是保守下界。
    等满 WAIT_LIMIT_S 强放行——宁可冒一次内存风险，也不能把整个发版卡死。

    ⚠️ 独占批（SOLO_BATCHES）的"跟谁都不并行"由调用方保证（它们先串行跑完），
    这个函数只管预算，不管互斥。
    """
    global _inflight_cost_gb, _inflight_count
    waited = 0.0
    while True:
        with _slot_lock:
            avail = psutil.virtual_memory().available / 2**30
            if (waited >= WAIT_LIMIT_S
                    or ((avail - _inflight_cost_gb) >= MEM_RESERVE_GB + cost_gb
                        and _inflight_count < _max_in_flight)):
                _inflight_cost_gb += cost_gb
                _inflight_count += 1
                return waited
        time.sleep(2)
        waited += 2


def _release_slot(cost_gb: float) -> None:
    global _inflight_cost_gb, _inflight_count
    with _slot_lock:
        _inflight_cost_gb -= cost_gb
        _inflight_count -= 1


def _slug(label: str) -> str:
    """批次标签 → 产物文件名主干。⚠️ 产物命名与孤儿清理共用这一处，别各写一份正则。"""
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", label)


def purge_orphan_artifacts(batches: list[tuple[str, str]]) -> list[str]:
    """删掉本轮已无对应批次的遗留 `.log`/`.xml`（测试目录改名或被删时会永久留下）。

    绿批产物由 `_run_batch` 解析完自删，这里只兜孤儿。调用方必须传**未经 `--only` 过滤**的
    全量批次，否则跑子集会把别的批次留着待查的红批日志当孤儿删掉。

    ⚠️ 孤儿是**上一轮**留下的，没法证明本轮之外没人在那目录放过东西 ⇒ 走回收站
    （`tools/disk/recycle.py`）而非物理删；送站失败就留着并喊出来，绝不退回 `unlink`。
    （本轮绿批的产物是自己几秒前造的、每轮约 30 个，那些才直接 `unlink`——全走回收站会把
    回收站塞满测试垃圾。）
    """
    keep = {_slug(label) for label, _ in batches}
    orphans = [f for pattern in ("*.log", "*.xml") for f in sorted(OUT_DIR.glob(pattern))
               if f.name != MEM_LOG.name and f.stem not in keep]
    if not orphans:
        return []

    sys.path.insert(0, str(PROJECT_ROOT))
    from tools.disk.recycle import send_to_recycle

    removed: list[str] = []
    for f in orphans:
        ok, err = send_to_recycle(str(f))
        if ok:
            removed.append(f.name)
        else:
            print(f"[batched] 孤儿产物送回收站失败，保留 {f.name}：{err}", flush=True)
    return removed


def _run_batch(label: str, path: str, markers: str | None) -> dict:
    global _IN_FLIGHT
    slug = _slug(label)
    log_path = OUT_DIR / f"{slug}.log"
    xml_path = OUT_DIR / f"{slug}.xml"
    cmd = [sys.executable, "-m", "pytest", path, "--tb=line", "-p", "no:cacheprovider",
           f"--junit-xml={xml_path}"]
    if markers:
        cmd += ["-m", markers]

    cost = _cost_gb_for(label)
    waited = _acquire_slot(cost)
    avail = psutil.virtual_memory().available / 2**30   # 报告里的"起跑前可用"
    t0 = time.monotonic()
    _IN_FLIGHT += 1
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
            rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                cwd=str(PROJECT_ROOT)).returncode
    finally:
        _IN_FLIGHT -= 1
        _release_slot(cost)
    dur = time.monotonic() - t0
    low_water = psutil.virtual_memory().available / 2**30

    summary, failures = parse_junit_xml(xml_path)
    status, detail = classify_exit(rc)
    if rc == 0:
        log_path.unlink(missing_ok=True)
        xml_path.unlink(missing_ok=True)
        log_ref = ""   # 空 = 产物已回收，别让人去打开一个不存在的路径
    else:
        log_ref = str(log_path.relative_to(PROJECT_ROOT))
    return {
        "label": label, "rc": rc, "status": status, "detail": detail,
        "seconds": dur, "waited": waited, "avail_before": avail, "avail_after": low_water,
        "summary": summary, "failures": failures, "log": log_ref,
        "slow": label in SLOW_BATCHES,
    }


def _fmt_counts(summary: dict | None) -> str:
    if not summary:
        return "(XML 未生成)"
    return (f"{summary['passed']}p {summary['failures']}f "
            f"{summary['errors']}e {summary['skipped']}s")


def run(all_batches: list[tuple[str, str]], markers: str | None, parallel: int) -> list[dict]:
    """调度：独占批先串行跑完 → 其余全部进并发池，实际并发度由内存槽位动态给。

    ⚠️ 独占批（真动本机桌面）必须"跟谁都不并行"，所以它们先串行跑完再进并发段——
    这段没有可压缩空间，是硬约束换来的稳定性（2026-10-03 实测并行必假红）。
    """
    solo = [b for b in all_batches if b[0] in SOLO_BATCHES]
    rest = [b for b in all_batches if b[0] not in SOLO_BATCHES]
    results: dict[str, dict] = {}
    t_start = time.monotonic()
    stop = threading.Event()
    threading.Thread(target=_mem_sampler, args=(stop,), daemon=True).start()

    def _echo(kind: str, r: dict) -> None:
        wait = f" | 等内存 {r['waited']:.0f}s" if r["waited"] else ""
        print(f"{'OK ' if r['rc'] == 0 else 'RED'} [{kind}] {r['label']}: "
              f"{_fmt_counts(r['summary'])} | {r['seconds']:.0f}s{wait} | "
              f"跑完可用 {r['avail_after']:.1f}GB | rc={r['rc']}", flush=True)

    try:
        if solo:
            print(f"[batched] 独占批 {len(solo)} 个（跑的时候不与其他批并行）先串行",
                  flush=True)
        for label, path in solo:
            r = _run_batch(label, path, markers)
            results[r["label"]] = r
            _echo("独占", r)

        if parallel > 1 and rest:
            # 重的先派，减少长尾；实际同时在飞几个由内存槽位决定，与派发顺序无关
            rest.sort(key=lambda b: (b[0] not in SLOW_BATCHES, b[0]))
            print(f"[batched] 并发段 {len(rest)} 个批次（内存感知，上限 {parallel} 路）",
                  flush=True)
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(rest)) as pool:
                futures = [pool.submit(_run_batch, label, path, markers) for label, path in rest]
                for fut in concurrent.futures.as_completed(futures):
                    r = fut.result()
                    results[r["label"]] = r
                    _echo("并发", r)
        else:
            for label, path in rest:
                r = _run_batch(label, path, markers)
                results[r["label"]] = r
                _echo("串行", r)
    finally:
        stop.set()

    ordered = [results[lbl] for lbl, _ in all_batches if lbl in results]
    for r in ordered:
        r["wall_total"] = time.monotonic() - t_start
    return ordered


def _sampled_valley() -> tuple[float, int]:
    """采样日志里的全程最低可用内存 + 当时几路在跑（并行重叠期只有采样能看到）。"""
    lo, infl = float("inf"), 0
    if MEM_LOG.exists():
        for ln in MEM_LOG.read_text(encoding="utf-8").splitlines():
            m = re.search(r"avail=([\d.]+) in_flight=(\d+)", ln)
            if m and float(m.group(1)) < lo:
                lo, infl = float(m.group(1)), int(m.group(2))
    return (0.0 if lo == float("inf") else lo), infl


def write_report(results: list[dict], markers: str | None, parallel: int,
                 partial: bool = False) -> Path:
    total = sum((r["summary"] or {}).get("tests", 0) for r in results)
    passed = sum((r["summary"] or {}).get("passed", 0) for r in results)
    skipped = sum((r["summary"] or {}).get("skipped", 0) for r in results)
    failed = sum((r["summary"] or {}).get("failures", 0) for r in results)
    errors = sum((r["summary"] or {}).get("errors", 0) for r in results)
    red = [r for r in results if r["rc"] != 0]
    valley, valley_inflight = _sampled_valley()
    wall = max((r.get("wall_total", 0.0) for r in results), default=0.0)

    lines = ["# 分批测试报告" + ("（部分重跑）" if partial else "") + "\n",
             f"- 批次 {len(results)}，非零退出 {len(red)}（并发上限 {parallel} 路，"
             f"内存感知；marker 过滤：{'quick 层' if markers else '无（不排除外部资源）'}）",
             f"- 合计：{passed} passed / {failed} failed / {errors} errors / {skipped} skipped"
             f"（共 {total} 条）",
             f"- 总墙钟 {wall / 60:.1f} 分钟；采样最低可用内存 {valley:.2f} GB"
             f"（当时 {valley_inflight} 路在跑）\n",
             "| 批次 | 结果 | 计数 | 墙钟 | 等内存 | 起跑前可用 | rc |",
             "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['label']}{'（慢/并行）' if r['slow'] else ''} | {r['status']} | "
                     f"{_fmt_counts(r['summary'])} | {r['seconds']:.0f}s | {r['waited']:.0f}s | "
                     f"{r['avail_before']:.1f} GB | {r['rc']} |")
    if red:
        lines.append("\n## 非零批次明细\n")
        for r in red:
            lines.append(f"- **{r['label']}** rc={r['rc']} — {r['detail']}；日志 `{r['log']}`")
            for f in r["failures"][:20]:
                lines.append(f"  - `{f.get('classname', '')}` {f.get('message', '')[:180]}")
            if len(r["failures"]) > 20:
                lines.append(f"  - …另有 {len(r['failures']) - 20} 条，见 XML")
    # 部分重跑（--only）写 SUMMARY.partial.md：红批日志现在跨轮保留，让子集跑覆盖整轮报告
    # 等于把那份索引丢掉。名字固定不加时间戳，否则又回到"产物越堆越多"。
    out = OUT_DIR / ("SUMMARY.partial.md" if partial else "SUMMARY.md")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main() -> int:
    global _max_in_flight
    p = argparse.ArgumentParser(description="分批运行测试（内存感知并发 + 内存护栏）")
    p.add_argument("--quick", action="store_true", help="只跑纯逻辑层（排除 gpu/browser/real_backend/network/chaos）")
    p.add_argument("--parallel", type=int, default=0,
                   help="并发硬上限；0=按内存自动（默认，上限 6），1=串行")
    p.add_argument("--serial", action="store_true", help="强制串行，对照或排障用")
    p.add_argument("--only", action="append", default=[],
                   help="只跑匹配的批次（可重复）；EXCLUDED_BATCHES 也可被显式选中")
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MEM_LOG.write_text("", encoding="utf-8")   # 采样数字只属于本轮，别和上次混
    if args.serial:
        _max_in_flight = 1
    elif args.parallel > 0:
        _max_in_flight = args.parallel
    else:
        _max_in_flight = MAX_IN_FLIGHT_DEFAULT
    parallel = _max_in_flight
    markers = QUICK_MARKERS if args.quick else None
    batches_all = discover_batches()
    # 按"排除前"的完整批次判孤儿：被默认跳过的批次不是"已无对应批次"，它的遗留日志要留
    purged = purge_orphan_artifacts(batches_all)   # 见函数 docstring
    if purged:
        print(f"[batched] 清掉上轮遗留产物 {len(purged)} 个: {purged}", flush=True)
    if args.only:
        batches = [b for b in batches_all if any(o in b[0] for o in args.only)]
    else:
        skipped = [b for b in batches_all if b[0] in EXCLUDED_BATCHES]
        batches = [b for b in batches_all if b[0] not in EXCLUDED_BATCHES]
        if skipped:
            print(f"[batched] 默认跳过 {[l for l, _ in skipped]}（--only 可显式跑）",
                  flush=True)

    print(f"[batched] 批次 {len(batches)} 个，可用内存 "
          f"{psutil.virtual_memory().available / 2**30:.1f} GB", flush=True)
    results = run(batches, markers, parallel)
    report = write_report(results, markers, parallel, partial=bool(args.only))

    red = [r["label"] for r in results if r["rc"] != 0]
    print(f"\n[batched] 报告 -> {report}", flush=True)
    if red:
        print(f"[batched] 非零批次 {len(red)}: {json.dumps(red, ensure_ascii=False)}", flush=True)
        return 1
    print("[batched] 全部批次 rc=0", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

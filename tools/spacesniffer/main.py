"""SpaceSniffer 快照浏览器 —— 命令行入口。

用法::

    .venv\\Scripts\\python.exe tools/spacesniffer/main.py                # 打开空窗口
    .venv\\Scripts\\python.exe tools/spacesniffer/main.py <快照.sns>     # 直接加载
    .venv\\Scripts\\python.exe tools/spacesniffer/main.py --info <快照.sns>   # 只打信息不开窗

Windows 上也可以直接把 `.sns` 文件拖到 `start.bat` 上；窗口本身也支持拖放。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 与 viewer.py 相同的引导：tools/ 下各工具目录都不是包，同级模块走扁平导入。
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
for _path in (str(_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import sns_model  # noqa: E402


def _print_reconciliation(snapshot: "sns_model.Snapshot") -> None:
    """顶层对账：unknown=0 并不一定代表扫全了。

    权限不足时 `05` 伪记录照样写 0（`is_complete` 因此为真），但卷记录的 used 与
    「顶层子项之和」会差出一大截。正常只差 NTFS 元数据（$MFT / 日志 / 簇尾浪费），
    量级随文件数与卷容量增长；差得离谱就该怀疑这份快照，不要拿它排 TOP 榜。
    详见 `workspace/disk_manager/references/sns_format.md`「unknown=0 不等于扫全了」。
    """
    children = snapshot.root.children or []
    files = [c for c in children if not c.is_dir]
    dirs = [c for c in children if c.is_dir]
    gap = snapshot.used_bytes - sum(c.size for c in children)
    if gap <= 0:
        return
    print(f"顶层对账    卷 used − Σ顶层子项 = {sns_model.human_size(gap)}"
          f"（{len(dirs)} 个顶层目录 + {len(files)} 个根级文件）")
    if snapshot.stateful_records:
        # 有实证：状态字非零都落在需提权的路径上，且这些盘上真的缺了整个受保护子树
        print("            ⚠ 本份快照存在未列举分支 ⇒ 受保护目录很可能没进树，")
        print("              不要当作完整的占用清单用（提权跑 SpaceSniffer 重扫可解）。")
    elif gap > 30 * 1024**3:
        print("            ⚠ 差额偏大。候选原因：卷影副本/系统还原（不在目录枚举里）、")
        print("              System Volume Information 内容、或被跳过的目录；建议提权核对。")


def _print_info(path: str) -> int:
    """`--info`：不开窗，只把解析结果的关键指标打出来（排查/脚本化用）。"""
    snapshot = sns_model.parse_file(path)
    print(f"文件        {path}")
    print(f"体积        {sns_model.human_size(snapshot.file_size or 0)}")
    print(f"数据时点    {snapshot.scanned_at() or '未知'}")
    print(f"卷容量      {sns_model.human_size(snapshot.total_bytes)}")
    print(f"已用        {sns_model.human_size(snapshot.used_bytes)}")
    print(f"可用        {sns_model.human_size(snapshot.free_bytes)}")
    print(f"目录 / 文件 {snapshot.n_dirs:,} / {snapshot.n_files:,}")
    print(f"覆盖完整    {'是' if snapshot.is_complete else '否'}（未计入 {sns_model.human_size(snapshot.unknown_bytes)}）")
    if snapshot.stateful_records:
        print(
            f"状态字非零  {snapshot.stateful_records:,} 条 —— 说明本次扫描有条目走了非默认枚举分支"
            f"\n            （通常是提权不足，集中在 Users / Program Files / Windows 等受保护路径）"
        )
    _print_reconciliation(snapshot)
    top = sorted(snapshot.root.children or [], key=lambda c: -c.size)[:10]
    print("顶层最大的 10 项（目录或根目录下的大文件）：")
    for entry in top:
        kind = "目录" if entry.is_dir else "文件"
        print(
            f"  {sns_model.human_size(entry.size):>10}  {kind}  "
            f"{entry.name}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spacesniffer",
        description="SpaceSniffer 快照浏览器：读取 .sns 快照并用 treemap 可视化",
    )
    parser.add_argument(
        "snapshot",
        nargs="?",
        help="要打开的 .sns 快照路径（省略则启动空窗口，可从菜单/拖放打开）",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="只在命令行打印快照摘要，不启动图形界面",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.snapshot and not Path(args.snapshot).exists():
        print(f"找不到快照文件：{args.snapshot}", file=sys.stderr)
        return 2

    if args.info:
        if not args.snapshot:
            parser.error("--info 需要同时给出快照路径")
        try:
            return _print_info(args.snapshot)
        except sns_model.SnsParseError as exc:
            print(f"解析失败：{exc}", file=sys.stderr)
            return 1

    import viewer

    # 解析在后台线程里做，失败时窗口会弹提示，这里不再重复捕获。
    return viewer.launch(args.snapshot, argv=sys.argv[:1])


if __name__ == "__main__":
    raise SystemExit(main())

"""调试浏览器 profile 数据安全工具（备份 / 盘点 / 还原）。

背景（2026-10-24 事故）：跨浏览器打开同一个 `chrome_debug/` 会让目标浏览器做
扩展 MAC 校验失败 → **静默删除 Default/Extensions 下的文件**；事后又没有备份，
只能靠用户手动重装。本工具把"动 profile 之前先备份、动完能验证、砸了能还原"
固化成一条命令，替代口头约定。

用法：
    # 1) 任何要动 profile 的操作之前，先备份（默认落到 backups/browser_profile/）
    python tools/browser/profile_guard.py backup --profile chrome_debug

    # 2) 盘点（不写盘，只统计 cookie / 扩展 / 本地存储 / 关键文件体积）
    python tools/browser/profile_guard.py inspect --profile chrome_debug

    # 3) 出事还原（会先自动备份当前状态，再覆盖）
    python tools/browser/profile_guard.py restore \
        --profile chrome_debug --backup backups/browser_profile/chrome_debug_20261024_190500

设计约束：
- 只读备份不改动源 profile；restore 前强制再备份一次当前状态（双保险）。
- 备份清单写 manifest.json（体积 / 文件数 / sha1 / 扩展 ID 列表 / cookie 条数），
  备份后立刻自检（重新 stat + 重算 sha1），不一致直接 exit 1，不假装成功。
- Cookies 用 `immutable=1` 只读打开，绝不写源库；源浏览器在跑也能读。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# profile 根目录下需要保护的关键条目（相对路径）。顺序即重要性。
KEY_ITEMS = [
    "Local State",                        # cookie 加密密钥(os_crypt) / 版本标记
    "Default/Secure Preferences",         # 扩展条目 + MAC 签名（跨浏览器会被清空）
    "Default/Preferences",
    "Default/Extensions",                 # 扩展实体（跨浏览器打开会被静默删除）
    "Default/Local Extension Settings",   # 扩展自身存储
    "Default/Network/Cookies",            # 登录态
    "Default/Login Data",                 # 站点密码（DPAPI 加密，随 Local State 密钥）
    "Default/Web Data",
    "Default/Local Storage",              # 站点 localStorage（很多站点 token 存这里）
]


def _sha1_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with p.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _dir_stats(p: Path) -> tuple[int, int]:
    size = 0
    count = 0
    for f in p.rglob("*"):
        if f.is_file():
            size += f.stat().st_size
            count += 1
    return size, count


def _cookie_count(profile: Path) -> int | None:
    db = profile / "Default" / "Network" / "Cookies"
    if not db.exists():
        return None
    try:
        uri = "file:" + db.resolve().as_posix() + "?immutable=1"
        con = sqlite3.connect(uri, uri=True)
        try:
            return int(con.execute("SELECT COUNT(*) FROM cookies").fetchone()[0])
        finally:
            con.close()
    except Exception:
        return None


def _extension_ids(profile: Path) -> list[str]:
    base = profile / "Default" / "Extensions"
    if not base.is_dir():
        return []
    ids = []
    for d in sorted(base.iterdir()):
        if d.is_dir() and any(d.glob("*/manifest.json")):
            ids.append(d.name)
    return ids


def inspect_profile(profile: Path) -> dict:
    info: dict = {
        "profile": str(profile),
        "exists": profile.is_dir(),
        "cookie_rows": _cookie_count(profile),
        "extension_ids": _extension_ids(profile),
        "items": {},
    }
    for rel in KEY_ITEMS:
        p = profile / rel
        if not p.exists():
            info["items"][rel] = {"present": False}
            continue
        if p.is_dir():
            size, count = _dir_stats(p)
            info["items"][rel] = {"present": True, "kind": "dir", "bytes": size, "files": count}
        else:
            info["items"][rel] = {"present": True, "kind": "file", "bytes": p.stat().st_size, "files": 1}
    return info


def cmd_inspect(args: argparse.Namespace) -> int:
    profile = Path(args.profile)
    info = inspect_profile(profile)
    if not info["exists"]:
        print(f"[ERROR] profile 不存在：{profile}")
        return 1
    print(f"profile        : {info['profile']}")
    print(f"cookie 条数    : {info['cookie_rows']}")
    print(f"扩展数(有效)   : {len(info['extension_ids'])}  {info['extension_ids'][:8]}{' …' if len(info['extension_ids']) > 8 else ''}")
    print("-" * 72)
    for rel, st in info["items"].items():
        if not st["present"]:
            print(f"  {'缺失':<6} {rel}")
        else:
            mb = st["bytes"] / 1048576
            print(f"  {'OK':<6} {rel:<38} {mb:9.2f} MB  {st['files']:>7} 文件")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    profile = Path(args.profile)
    if not profile.is_dir():
        print(f"[ERROR] profile 不存在：{profile}")
        return 1
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) / f"{profile.resolve().name}_{stamp}"
    out.mkdir(parents=True, exist_ok=False)

    manifest: dict = {"profile": str(profile.resolve()), "created_at": stamp, "items": {}}
    for rel in KEY_ITEMS:
        src = profile / rel
        if not src.exists():
            manifest["items"][rel] = {"present": False}
            continue
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
            size, count = _dir_stats(dst)
            manifest["items"][rel] = {"present": True, "kind": "dir", "bytes": size, "files": count}
        else:
            shutil.copy2(src, dst)
            manifest["items"][rel] = {
                "present": True, "kind": "file",
                "bytes": dst.stat().st_size, "files": 1,
                "sha1": _sha1_file(src),
            }
        print(f"  [copy] {rel}")

    # 备份后自检：体积 / sha1 / 扩展完整性必须一致
    manifest["cookie_rows"] = _cookie_count(profile)
    manifest["extension_ids"] = _extension_ids(profile)
    bad = []
    for rel, st in manifest["items"].items():
        if not st.get("present"):
            continue
        dst = out / rel
        if not dst.exists():
            bad.append(f"{rel}: 备份后不存在")
            continue
        if st["kind"] == "dir":
            got_size, got_count = _dir_stats(dst)
            if (got_size, got_count) != (st["bytes"], st["files"]):
                bad.append(f"{rel}: 体积/文件数不一致 {got_size}/{got_count} != {st['bytes']}/{st['files']}")
        else:
            if _sha1_file(dst) != st["sha1"]:
                bad.append(f"{rel}: sha1 不一致")

    # 扩展实体必须与 profile 里登记的有效扩展数对得上
    ext_backup = out / "Default" / "Extensions"
    ext_ok = len([d for d in ext_backup.iterdir() if d.is_dir() and any(d.glob("*/manifest.json"))]) if ext_backup.is_dir() else 0
    if ext_ok != len(manifest["extension_ids"]):
        bad.append(f"Extensions: 备份内有效扩展 {ext_ok} != profile {len(manifest['extension_ids'])}")

    manifest["verified"] = not bad
    manifest["problems"] = bad
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if bad:
        print(f"[FAIL] 备份自检未通过（清单已写 {out / 'manifest.json'}）：")
        for b in bad:
            print("   -", b)
        return 1
    total = sum(v.get("bytes", 0) for v in manifest["items"].values())
    print(f"[OK] 备份完成并自检通过：{out}  ({total / 1048576:.1f} MB, 扩展 {len(manifest['extension_ids'])} 个, cookie {manifest['cookie_rows']} 条)")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    profile = Path(args.profile)
    backup_root = Path(args.backup)
    mf = backup_root / "manifest.json"
    if not mf.exists():
        print(f"[ERROR] 备份目录缺 manifest.json：{backup_root}")
        return 1
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    if not args.yes:
        print(f"[ABORT] 还原会覆盖 {profile}，确认请加 --yes（建议先跑一次 backup）")
        return 2

    # 覆盖前先把当前状态再备一份（双保险，避免"还原成更坏的状态"无从回溯）
    print("[1/2] 覆盖前先备份当前状态 …")
    ns = argparse.Namespace(profile=str(profile), out=str(Path(args.out)))
    if cmd_backup(ns) != 0:
        print("[ABORT] 当前状态备份失败，拒绝还原（不允许在没有退路的情况下覆盖）")
        return 1

    print("[2/2] 按清单还原 …")
    for rel, st in manifest["items"].items():
        if not st.get("present"):
            continue
        src = backup_root / rel
        dst = profile / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
        elif dst.exists():
            dst.unlink()
        if st["kind"] == "dir":
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        print(f"  [restore] {rel}")
    print("[OK] 还原完成；请用 browser 启动脚本重启调试实例后验证扩展与登录态")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="调试浏览器 profile 备份 / 盘点 / 还原")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("inspect", help="盘点 profile 关键数据（只读）")
    p1.add_argument("--profile", required=True)

    p2 = sub.add_parser("backup", help="备份关键数据并自检")
    p2.add_argument("--profile", required=True)
    p2.add_argument("--out", default="backups/browser_profile")

    p3 = sub.add_parser("restore", help="从备份还原")
    p3.add_argument("--profile", required=True)
    p3.add_argument("--backup", required=True)
    p3.add_argument("--out", default="backups/browser_profile")
    p3.add_argument("--yes", action="store_true")

    args = ap.parse_args()
    return {"inspect": cmd_inspect, "backup": cmd_backup, "restore": cmd_restore}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

"""文件关联备份 - 共享读取/解析工具

只依赖标准库（winreg 只读 + subprocess 调 reg save/export），不引入外部 exe。
被 assoc_snapshot / assoc_diff / assoc_restore 复用。

Windows 10/11 一个后缀的默认程序由三层决定（优先级从高到低）：
  1. HKCU\\...\\Explorer\\FileExts\\<ext>\\UserChoice   —— 真正生效的每用户默认（带 Hash，恢复受限）
  2. HKCU\\Software\\Classes\\<ext>                      —— 每用户经典 ProgID 映射（可无损恢复）
  3. HKCR / HKLM\\Software\\Classes\\<ext>              —— 系统级经典映射（只读采集）

UserChoice 的 Hash 绑定「SID+后缀+ProgID+时间+开机密钥」，普通 reg import 导回旧值会被
Windows 判哈希失败而忽略——所以本模块把「采集/对比」和「恢复」分开设计，恢复层见 assoc_restore。
"""

from __future__ import annotations

import os
import re
import json
import subprocess
import winreg
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from typing import Any, Iterator, Optional

# 备份根目录，与 backup_env.py 保持一致（整机备份同域）
BACKUP_BASE = Path(r"<data_drive>:/<backup_root>\SystemBackup")
# 文件关联专属区（独立于按次的系统快照，便于多次增量快照/diff）
ASSOC_ROOT = BACKUP_BASE / "file_associations"

FILEEXTS_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def _open_key(
    root: int, subkey: str, access: int = winreg.KEY_READ
) -> Iterator[Optional[Any]]:
    """安全打开注册表项；不存在时 yield None。负责关闭句柄。"""
    try:
        h = winreg.OpenKey(root, subkey, 0, access)
    except OSError:
        yield None
        return
    try:
        yield h
    finally:
        winreg.CloseKey(h)


def _read_value(key: Any, name: str) -> Optional[Any]:
    try:
        value, _type = winreg.QueryValueEx(key, name)
        return value
    except OSError:
        return None


def _read_default(key: Any) -> Optional[str]:
    try:
        value = winreg.QueryValue(key, "")
        return value if isinstance(value, str) and value else None
    except OSError:
        return None


def list_extensions() -> list[str]:
    """枚举 FileExts 下出现过的所有后缀（每用户曾设置过默认程序的后缀）。"""
    out: list[str] = []
    with _open_key(winreg.HKEY_CURRENT_USER, FILEEXTS_SUBKEY) as k:
        if k is None:
            return out
        i = 0
        while True:
            try:
                name = winreg.EnumKey(k, i)
            except OSError:
                break
            out.append(name)
            i += 1
    return sorted(out)


def get_userchoice(ext: str) -> dict[str, Any]:
    """读取某后缀的 UserChoice（生效默认）。返回 progid / hash_present / app。"""
    with _open_key(winreg.HKEY_CURRENT_USER, f"{FILEEXTS_SUBKEY}\\{ext}\\UserChoice") as k:
        if k is None:
            return {"progid": None, "hash_present": False, "app": None}
        progid = _read_value(k, "ProgID")
        hash_val = _read_value(k, "Hash")
        app = _read_value(k, "App")
        return {
            "progid": progid if isinstance(progid, str) else None,
            "hash_present": hash_val is not None,
            "app": app if isinstance(app, str) else None,
        }


def get_classic_progid(ext: str, root: int) -> Optional[str]:
    """读取经典层某后缀的默认 ProgID（HKCU\\Software\\Classes 或 HKCR）。"""
    with _open_key(root, ext) as k:
        return _read_default(k) if k is not None else None


_EXPAND_RE = re.compile(r"%(\w+)%")


def _expand(cmd: str) -> str:
    return _EXPAND_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), cmd)


def resolve_command(progid: str) -> dict[str, Optional[str]]:
    """把 ProgID 解析成 open 命令行与可执行文件路径，并尽量取友好名。"""
    result: dict[str, Optional[str]] = {"command": None, "exe": None, "friendly": None}
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_CLASSES_ROOT):
        subkey = (
            f"Software\\Classes\\{progid}\\shell\\open\\command"
            if root == winreg.HKEY_CURRENT_USER
            else f"{progid}\\shell\\open\\command"
        )
        with _open_key(root, subkey) as k:
            if k is not None:
                raw = _read_default(k)
                if raw:
                    result["command"] = _expand(raw)
                    result["exe"] = _extract_exe(raw)
                    break

    # 友好名：HKCR\<ProgID> 默认值（多数经典 ProgID 有；无则为 None）
    with _open_key(winreg.HKEY_CLASSES_ROOT, progid) as k:
        if k is not None:
            result["friendly"] = _read_default(k)

    # 浏览器类 ProgID（MSEdge* / ChromeHTML / FirefoxURL）没有 open\command，走 App Paths 反查
    if result["exe"] is None:
        result["exe"] = _browser_exe_from_progid(progid)
    return result


def _extract_exe(command: str) -> Optional[str]:
    cmd = command.strip()
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end > 0:
            return cmd[1:end]
    else:
        # 取到第一个空格前，但要处理 .exe 边界
        m = re.match(r"^(\S+\.exe)", cmd, re.IGNORECASE)
        if m:
            return m.group(1)
        parts = cmd.split(" ", 1)
        if parts:
            return parts[0]
    return None


_BROWSER_HINTS = {
    "MSEdgePDF": "msedge.exe",
    "MSEdge.HTML": "msedge.exe",
    "MSEdge.EPUB": "msedge.exe",
    "ChromeHTML": "chrome.exe",
    "FirefoxURL": "firefox.exe",
    "OperaStable": "opera.exe",
}


def _browser_exe_from_progid(progid: str) -> Optional[str]:
    exe_name = _BROWSER_HINTS.get(progid)
    if not exe_name:
        return None
    base = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    candidates = [
        Path(base) / "Microsoft" / "Edge" / exe_name,
        Path(base) / "Google" / "Chrome" / "Application" / exe_name,
        Path(base) / "Mozilla Firefox" / exe_name,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return exe_name  # 找不到实体也记录程序名，供人工核对


def collect_all() -> list[dict[str, Any]]:
    """采集当前全部后缀的有效关联，返回可 diff 的稳定记录列表。"""
    exts = set(list_extensions())
    # 补充：仅在经典 HKCU\\Software\\Classes 出现、但 FileExts 没有的后缀
    with _open_key(winreg.HKEY_CURRENT_USER, r"Software\Classes") as k:
        if k is not None:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(k, i)
                except OSError:
                    break
                if name.startswith("."):
                    exts.add(name)
                i += 1

    records: list[dict[str, Any]] = []
    for ext in sorted(exts):
        uc = get_userchoice(ext)
        hkcu = get_classic_progid(ext, winreg.HKEY_CURRENT_USER) if _has_classic(ext) else None
        hkcr = get_classic_progid(ext, winreg.HKEY_CLASSES_ROOT)
        eff = uc["progid"] or hkcu or hkcr
        rec: dict[str, Any] = {
            "ext": ext,
            "userchoice_progid": uc["progid"],
            "userchoice_hash": uc["hash_present"],
            "classic_hkcu": hkcu,
            "classic_hkcr": hkcr,
            "effective_progid": eff,
            "command": None,
            "exe": None,
            "friendly": None,
        }
        if eff:
            resolved = resolve_command(eff)
            rec["command"] = resolved["command"]
            rec["exe"] = resolved["exe"]
            rec["friendly"] = resolved["friendly"]
        records.append(rec)
    return records


def _has_classic(ext: str) -> bool:
    with _open_key(winreg.HKEY_CURRENT_USER, f"Software\\Classes\\{ext}") as k:
        return k is not None


def build_inventory() -> dict[str, Any]:
    """生成一份带元数据的清单快照。"""
    recs = collect_all()
    return {
        "captured_at": _now_iso(),
        "user": os.environ.get("USERNAME", "?"),
        "sid": _current_sid(),
        "count": len(recs),
        "records": recs,
    }


def _current_sid() -> str:
    try:
        out = subprocess.run(
            ["whoami", "/user"], capture_output=True, text=True, timeout=8
        )
        for line in out.stdout.splitlines():
            if line.strip().upper().startswith("SID"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "unknown"


# ---------- 快照存取 ----------

def snapshot_dir(ts: str) -> Path:
    return ASSOC_ROOT / ts


def list_snapshots() -> list[str]:
    if not ASSOC_ROOT.exists():
        return []
    dirs = [p.name for p in ASSOC_ROOT.iterdir() if p.is_dir() and (p / "inventory.json").exists()]
    return sorted(dirs)


def latest_snapshot() -> Optional[str]:
    snaps = list_snapshots()
    return snaps[-1] if snaps else None


def load_inventory(ts: str) -> dict[str, Any]:
    p = snapshot_dir(ts) / "inventory.json"
    with p.open("r", encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


def save_inventory(ts: str, inv: dict[str, Any]) -> Path:
    d = snapshot_dir(ts)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "inventory.json"
    p.write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
    # 同步一份 CSV，方便人肉查看/Excel 比对
    csv_p = d / "associations.csv"
    _write_csv(inv.get("records", []), csv_p)
    return p


def _write_csv(records: list[dict[str, Any]], path: Path) -> None:
    import csv as _csv

    cols = [
        "ext", "effective_progid", "userchoice_progid", "userchoice_hash",
        "classic_hkcu", "classic_hkcr", "friendly", "exe", "command",
    ]
    # utf-8-sig 让 Excel 直接双击不乱码
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow(r)


def diff_inventories(base: dict[str, Any], cur: dict[str, Any]) -> list[dict[str, Any]]:
    """对比两份清单，返回变化的后缀列表（按 ext 索引）。"""
    b = {r["ext"]: r for r in base.get("records", [])}
    c = {r["ext"]: r for r in cur.get("records", [])}
    changes: list[dict[str, Any]] = []
    for ext in sorted(set(b) | set(c)):
        rb, rc = b.get(ext), c.get(ext)
        if rb is None:
            changes.append({"ext": ext, "kind": "added", "before": None, "after": rc})
        elif rc is None:
            changes.append({"ext": ext, "kind": "removed", "before": rb, "after": None})
        elif rb.get("effective_progid") != rc.get("effective_progid"):
            changes.append({"ext": ext, "kind": "changed", "before": rb, "after": rc})
    return changes

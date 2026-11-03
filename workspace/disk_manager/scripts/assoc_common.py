"""文件关联备份 - 共享读取/解析工具

只依赖标准库（winreg 只读 + subprocess 调 reg save/export），不引入外部 exe。
被 assoc_snapshot / assoc_diff / assoc_restore 复用。

Windows 10/11 一个后缀的默认程序由三层决定（优先级从高到低）：
  1. HKCU\\...\\Explorer\\FileExts\\<ext>\\UserChoice   —— 真正生效的每用户默认（带 Hash，恢复受限）
  2. HKCU\\Software\\Classes\\<ext>                      —— 每用户经典 ProgID 映射（可无损恢复）
  3. HKCR / HKLM\\Software\\Classes\\<ext>              —— 系统级经典映射（只读采集）

UserChoice 的 Hash 绑定「SID+后缀+ProgID+时间+开机密钥」，普通 reg import 导回旧值会被
Windows 判哈希失败而忽略——所以本模块把「采集/对比」和「恢复」分开设计，恢复层见 assoc_restore。

★ 2026-10-11~10-14 实证补充（详见 settings_watch/FILE_ASSOCIATION.md）：
  - ⛔ 旧说「『设经典层 + 删 UserChoice』对文档类后缀有效、对音视频/图像无效」**已作废**
    （2026-10-12 单变量实验更正，见 FILE_ASSOCIATION.md §9.1）：修完会不会弹「你要如何打开这个文件?」
    与"是不是文档类"**无关**，只取决于有没有踩这两个条件——
      ① 声明冲突：`HKCU\Software\Classes\<ext>` 与 `HKLM\Software\Classes\<ext>` 都存在且值不同
         （⛔ 还须 HKLM 侧能解析出 `shell\open\command`，否则冲突不成立、照样不弹）；
      ② 多程序歧义：`HKCU\...\Explorer\FileExts\<ext>\OpenWithProgids` 含指向**另一个 exe** 的 ProgID。
    两条任一成立就弹，都不成立就不弹（与 ProgID 写法无关）。当年音视频/图像那批失败，是因为脚本
    顺手往 OWP 里塞了异项、踩中 ②，不是因为类别竞争。
  - ★ **修复正路 = 走路 ①（官方路）**：双击该类型文件 → 选应用 → 勾「始终使用」→ 确定，
    Windows 自写带 Hash 的 UserChoice，立即生效且不动经典层 / 不动 OWP（副作用≈0）。
    ⛔ 别走"删 HKCU 经典层值 + 清 OWP"的注册表批量路——治 ① 只能二选一，会把生效程序换成 HKLM 那个。
  - 所以这里补了一组判定原语：effective_app()（看真正生效的是谁）、perceived_type()（判有无
    类别竞争）、delete_userchoice_key()（winreg 删键，reg.exe 会被 ACL 拒）、progid_alive()
    （目标程序还在不在）。恢复层用它们决定「能修 / 该引导 / 该跳过」，不再制造假成功。
"""

from __future__ import annotations

import os
import re
import ctypes
import json
import subprocess
import winreg
from ctypes import wintypes
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
            ["whoami", "/user"], capture_output=True, timeout=8
        )
        # 同上：本机 exe 中文输出不能用 text=True 兜
        for line in (out.stdout or b"").decode("gbk", "replace").splitlines():
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


# ==================== 以下为 2026-10-11 实证改造新增 ====================
# 全部只读或「只在明确调用时才写」，采集链路不会触发任何写入。

ASSOCSTR_COMMAND = 1
ASSOCSTR_EXECUTABLE = 2
ASSOCSTR_FRIENDLYAPPNAME = 4
# 实测校准（2026-10-11）：22 = APPPUBLISHER（发行者名），曾经被我误当成「默认 ProgID」
ASSOCSTR_PROGID = 20
ASSOCSTR_APPPUBLISHER = 22

_shlwapi = ctypes.WinDLL("shlwapi", use_last_error=True)
_shlwapi.AssocQueryStringW.restype = ctypes.c_long  # 用 c_long，失败 HRESULT 不被 ctypes 当异常
_shlwapi.AssocQueryStringW.argtypes = [
    wintypes.DWORD, ctypes.c_int, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.LPWSTR, ctypes.POINTER(ctypes.c_ulong),
]
_shlwapi.AssocGetPerceivedType.restype = ctypes.c_long
_shlwapi.AssocGetPerceivedType.argtypes = [
    wintypes.LPCWSTR,                  # pszExt
    ctypes.POINTER(ctypes.c_int),      # pType
    ctypes.POINTER(ctypes.c_int),      # pFlag
    ctypes.POINTER(wintypes.LPWSTR),   # ppszType（传 None）
]

# AssocGetPerceivedType 的 PERCEIVED 枚举（节选）
# PERCEIVED 枚举（实测校准：.txt=1 / .mp4=4 / .docx=6 / .jpg=2，起点是负数）
_PERCEIVED = {
    -3: "custom", -2: "unspecified", -1: "folder", 0: "unknown", 1: "text", 2: "image",
    3: "audio", 4: "video", 5: "compressed", 6: "document", 7: "system",
    8: "application", 9: "gamemedia", 10: "contacts",
}
# ★ 有「类别默认 AppX」竞争的三类：UserChoice 一空就会被 电影和电视/媒体播放器/照片 补位
CONTENDED_PERCEIVED = {"video", "audio", "image"}


def assoc_query(what: int, ext: str) -> str:
    """Windows 官方判定：这条后缀真正会用什么打开。
    assoc 命令和 winreg 读出来的层可能与 Explorer 实际用的不一致，以这个为准。"""
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.c_ulong(1024)
    hr = _shlwapi.AssocQueryStringW(0, what, ext, None, buf, ctypes.byref(n))
    return buf.value.strip() if hr == 0 else ""


def effective_app(ext: str) -> dict[str, Optional[str] | bool]:
    """返回该后缀当前真正生效的程序（Explorer 双击用的判定）。"""
    friendly = assoc_query(ASSOCSTR_FRIENDLYAPPNAME, ext)
    exe = assoc_query(ASSOCSTR_EXECUTABLE, ext)
    return {
        "progid": assoc_query(ASSOCSTR_PROGID, ext) or None,
        "exe": exe or None,
        "friendly": friendly or None,
        # ⛔ 别用「exe 是否为空」判会不会弹窗：AppX（照片等）查不到 exe 是正常的，
        #    照那个判会把「有默认应用」全误报成「会弹选取应用」（2026-10-11 实测 .jpg/.png 全中）。
        #    正确判据：友好名 == 选取应用，或 exe == OpenWith.exe
        "prompts": friendly.startswith("选取应用")
        or (bool(exe) and exe.lower().endswith("openwith.exe")),
    }


def perceived_type(ext: str) -> str:
    """Windows 自己认为这条后缀属于什么类别（用于预判有无 AppX 补位竞争）。"""
    pt = ctypes.c_int(0)
    flag = ctypes.c_int(0)
    hr = _shlwapi.AssocGetPerceivedType(ext, ctypes.byref(pt), ctypes.byref(flag), None)
    if hr != 0:
        return "unknown"
    return _PERCEIVED.get(pt.value, f"code{pt.value}")


def is_contended(ext: str) -> bool:
    """True = 删掉 UserChoice 后会被系统用 AppX 接管，注册表手段修不了，只能让目标程序自己关联。"""
    return perceived_type(ext) in CONTENDED_PERCEIVED


def progid_alive(progid: str) -> tuple[bool, str]:
    """目标 ProgID 还能不能真的打开东西（避开「程序早就没了」的白修）。"""
    if not progid:
        return False, "无目标 ProgID"
    info = resolve_command(progid)
    exe = info.get("exe")
    if not exe:
        return True, ""  # 浏览器类/AppX 类反查不到 exe，不算失效
    exe_clean = exe.replace('"', "").strip()
    for cand in (exe_clean, _expand(exe_clean)):
        if cand and os.path.exists(cand):
            return True, ""
    return False, f"目标程序已不存在: {exe_clean}"


def delete_userchoice_key(ext: str) -> tuple[bool, str]:
    """删除 UserChoice。

    ⛔ 两条都不能用：
      1. reg.exe —— 这些键的 ACL 会直接「拒绝访问」（2026-10-07 实测 103 个删不掉）。
      2. winreg 以 KEY_ALL_ACCESS 打开 —— Windows 给自己写的 UserChoice 加了一条
         `Deny <user> SetValue`，而 KEY_ALL_ACCESS 的权限集**含 SET_VALUE** ⇒ 整个
         OpenKey 就被拒（WinError 5），压根轮不到删（2026-10-11 在 .db 上实测到）。
    ✅ 正确做法：该 DENY **只挡「写值」，不挡「删除」**（FullControl 里 DELETE 仍在）。
       所以以 KEY_READ 判存在即可，再用 winreg.DeleteKey 直接删掉——DeleteKey 只需
       DELETE 权限。实测掩码证据：KEY_ALL_ACCESS 失败；KEY_READ / READ|DELETE / DELETE
       单独 均打开成功（见 temp/assoc_loss_probe_20261007/probe26_mask.py）。
    """
    parent = FILEEXTS_SUBKEY + "\\" + ext
    sub = parent + "\\UserChoice"
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, parent, 0, winreg.KEY_READ)
        winreg.CloseKey(k)
    except OSError:
        return True, "后缀不存在，无需删除"
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_READ)
        winreg.CloseKey(k)
    except FileNotFoundError:
        return True, "UserChoice 本就不存在"
    except OSError as e:
        return False, f"读取失败: {e}"
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        return True, "已删除"
    except FileNotFoundError:
        return True, "UserChoice 本就不存在"
    except PermissionError as e:
        return False, f"删除仍被拒（DELETE 权限被剥夺）: {e}"
    except OSError as e:
        return False, f"删除失败: {e}"


def run_reg_gbk(*args: str, timeout: int = 20) -> tuple[int, str]:
    """⛔ reg.exe 输出是 GBK。用 text=True 会 UnicodeDecodeError 并把中文报错吞干净
    （2026-10-07 因此把 114 条成功误判成失败）。"""
    try:
        r = subprocess.run(["reg", *args], capture_output=True, timeout=timeout)
        out = (r.stdout or b"").decode("gbk", "replace")
        err = (r.stderr or b"").decode("gbk", "replace")
        return r.returncode, (out + err).strip()
    except Exception as e:  # noqa: BLE001
        return 1, str(e)

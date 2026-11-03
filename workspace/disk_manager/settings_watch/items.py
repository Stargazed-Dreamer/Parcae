"""settings_watch 内置条目（providers）

每个 provider 只声明「怎么采集」和「怎么恢复」，diff/存储/CLI 由 base 引擎驱动。
新增一项 = 在这里写一个 WatchItem 子类并 @register。

首批内置：
  - file_associations  文件关联（每后缀默认程序）  —— 复用同级 scripts/assoc_common
  - env_path           PATH / 环境变量            （#8）
  - url_browser        默认浏览器 + URL 协议       （#9）
  - autostart          开机自启                    （#10）
  - winlogon           Winlogon 劫持监控（Shell / Userinit）（#14，安全向）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

try:  # 作为包导入（python -m workspace.disk_manager.settings_watch.watch）
    from .base import (
        RestoreOp, WatchItem, register, run_reg,
        reg_list_subkeys, reg_read_value, reg_list_values,
    )
except ImportError:  # 作为脚本直跑（python settings_watch/items.py，watch.py 已把本目录放上 sys.path）
    from base import (  # type: ignore
        RestoreOp, WatchItem, register, run_reg,
        reg_list_subkeys, reg_read_value, reg_list_values,
    )

import winreg  # noqa: E402

# 让同级 scripts/ 里的 assoc_common 可导入（复用其解析/采集逻辑，不重复造轮子）
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import assoc_common as C  # type: ignore # noqa: E402  (运行时经 SCRIPTS_DIR 导入，pyright 静态看不到)


def _by_key(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(r["key"]): r for r in records}


def _short(s: Any, n: int = 72) -> str:
    t = str(s)
    return t if len(t) <= n else t[: n - 1] + "…"


# ============================ 文件关联 ============================

@register
class FileAssocItem(WatchItem):
    id = "file_associations"
    name = "文件关联（每后缀默认程序）"
    kind = "assoc"
    supports_restore = True

    def collect(self) -> list[dict[str, Any]]:
        recs = C.collect_all()
        for r in recs:
            r["key"] = r["ext"]
        return recs

    def change_sig(self, rec: dict[str, Any]) -> Any:
        # 只看「指向」的四层来源，忽略派生的 command/exe/friendly（避免环境变量展开差异造成假变更）
        return (
            rec.get("effective_progid"),
            rec.get("userchoice_progid"),
            rec.get("classic_hkcu"),
            rec.get("classic_hkcr"),
        )

    def label(self, rec: dict[str, Any]) -> str:
        eff = rec.get("effective_progid") or "(未设置)"
        who = rec.get("exe") or rec.get("friendly") or "?"
        return f"{rec.get('key')} -> {eff} [{who}]"

    def restore_ops(
        self, target: list[dict[str, Any]], current: list[dict[str, Any]]
    ) -> list[RestoreOp]:
        cur_by = _by_key(current)
        ops: list[RestoreOp] = []
        for t in target:
            ext = str(t["key"])
            want = t.get("effective_progid")
            if not want:
                continue  # 基线本身就没有有效目标，无从恢复
            c = cur_by.get(ext)
            if c is not None and self.change_sig(t) == self.change_sig(c):
                continue  # 未变
            now = c.get("effective_progid") if c else "(丢失)"
            ops.append(self._make_op(ext, want, now))
        # 注意：仅「基线有、当前被改/丢」才自动恢复；「当前新增的后缀认领」不自动撤销（只在 diff 报告），保守起见
        return ops

    @staticmethod
    def _make_op(ext: str, want: str, now: Any) -> RestoreOp:
        def run() -> tuple[bool, str]:
            # 前置 1：目标程序还在不在？（2026-10-11 实测：残余 17 个全是目标已被卸载/挪走）
            alive, why = C.progid_alive(want)
            if not alive:
                return False, f"跳过 {ext}：{why}（属软件已不在，关联修好也打不开）"
            # 前置 2：有没有类别竞争？video/audio/image 被 Windows 的类别默认 AppX 盯着，
            #         UserChoice 一删就 +15s 被补位，写注册表注定失败——别假装成功。
            if C.is_contended(ext):
                return False, (
                    f"{ext} 属 {C.perceived_type(ext)} 类，Windows 有默认 AppX 会接管，注册表改不动；"
                    f"请让目标程序自己关联一次（如爱奇艺万能联播：设置 → 文件关联 → 全选 → 确定）"
                )
            # 哈希无关两步：设每用户经典层 -> 删带哈希的 UserChoice 覆盖
            rc1, out1 = run_reg(
                "add", f"HKCU\\Software\\Classes\\{ext}",
                "/ve", "/t", "REG_SZ", "/d", want, "/f",
            )
            if rc1 != 0:
                return False, f"设置经典层失败: {out1}"
            uc = C.get_userchoice(ext)
            if uc["progid"] is not None or uc["hash_present"]:
                # ⛔ 必须走 winreg：reg.exe 删这些键会被 ACL 拒（2026-10-07 实测 103/247 删不动）
                ok, why2 = C.delete_userchoice_key(ext)
                if not ok:
                    # UserChoice 残留 = 带哈希的那层仍然压着经典层，等于没修好，如实报 False
                    return False, f"经典层已设，但删 UserChoice 失败: {why2}（多为 Adobe 类保护 ACL）"
            return True, f"{ext}: {now} -> {want}"

        return RestoreOp(desc=f"关联 {ext} -> {want}（当前 {now}）", run=run)


# ============================ 环境变量 / PATH ============================

_TYPE_NAME = {
    winreg.REG_SZ: "REG_SZ",
    winreg.REG_EXPAND_SZ: "REG_EXPAND_SZ",
    winreg.REG_DWORD: "REG_DWORD",
}

_ENV_SCOPES = [
    # (scope名, root常量, subkey, 是否可自动恢复, 恢复所需reg键路径)
    ("USER", winreg.HKEY_CURRENT_USER, r"Environment", True, "HKCU\\Environment"),
    (
        "SYSTEM",
        winreg.HKEY_LOCAL_MACHINE,
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        False,  # 写 HKLM 需管理员，只报告不自动改
        "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment",
    ),
]


def _env_entries(root: int, subkey: str) -> list[tuple[str, Any, int]]:
    """返回 [(name, value, regtype)]，读取失败返回 []。"""
    out: list[tuple[str, Any, int]] = []
    try:
        k = winreg.OpenKey(root, subkey, 0, winreg.KEY_READ)
    except OSError:
        return out
    try:
        i = 0
        while True:
            try:
                name, val, typ = winreg.EnumValue(k, i)
            except OSError:
                break
            out.append((name, val, typ))
            i += 1
    finally:
        winreg.CloseKey(k)
    return out


def _split_path(value: str) -> list[str]:
    return [p for p in (value or "").split(";")]


def _broadcast_env_change() -> bool:
    """广播 WM_SETTINGCHANGE，让新进程读到改后的环境（尽力而为，失败不致命）。

    SendMessageTimeoutW 是 7 参数，末位 lpdwResult 必须给出合法 out 指针，
    否则 C 往野指针写会触发 0xC0000409 栈破坏（实测踩过）。
    """
    try:
        import ctypes
        from ctypes import wintypes

        HWND_BROADCAST = wintypes.HWND(0xFFFF)
        WM_SETTINGCHANGE = 0x1A
        SMTO_ABORTIFHUNG = 0x0002
        user32 = ctypes.windll.user32
        user32.SendMessageTimeoutW.restype = wintypes.LPARAM
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
            wintypes.LPCWSTR, wintypes.UINT, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        result = wintypes.DWORD(0)
        user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(result),
        )
        return True
    except Exception:  # noqa: BLE001
        return False


@register
class EnvPathItem(WatchItem):
    id = "env_path"
    name = "环境变量 / PATH"
    kind = "path"
    supports_restore = True
    restore_requires_admin = False  # 用户级自动、系统级手动，逐 op 标注

    def collect(self) -> list[dict[str, Any]]:
        recs: list[dict[str, Any]] = []
        for scope, root, subkey, _auto, regpath in _ENV_SCOPES:
            for name, val, typ in _env_entries(root, subkey):
                is_path = name.lower() == "path"
                rec: dict[str, Any] = {
                    "key": f"{scope}\\{name}",
                    "scope": scope,
                    "name": name,
                    "regtype": _TYPE_NAME.get(typ, f"type{typ}"),
                    "kind": "pathlist" if is_path else "string",
                    "value": val if isinstance(val, str) else str(val),
                    "entries": _split_path(val) if is_path else None,
                    "regpath": regpath,
                }
                recs.append(rec)
        return recs

    def change_sig(self, rec: dict[str, Any]) -> Any:
        if rec.get("kind") == "pathlist":
            return tuple(rec.get("entries") or [])  # 顺序敏感：挪动也算变更
        return rec.get("value")

    def label(self, rec: dict[str, Any]) -> str:
        return f"[{rec.get('scope')}] {rec.get('name')}"

    def describe_change(self, ch: Any) -> str:
        b, a = ch.before or {}, ch.after or {}
        if b.get("kind") == "pathlist" or a.get("kind") == "pathlist":
            be, ae = (b.get("entries") or []), (a.get("entries") or [])
            bes, aes = set(be), set(ae)
            added = [e for e in ae if e not in bes]
            removed = [e for e in be if e not in aes]
            parts = [f"{label} {len(lst)} 条" for label, lst in (("新增", added), ("移除", removed)) if lst]
            if not parts and be and ae:
                parts.append("顺序调整")
            body = " ; ".join(parts) if parts else "无条目增减"
            # 附带具体增减项（截断），方便一眼看出被塞/被删的是哪个目录
            detail_items = []
            if added:
                detail_items.append("+ " + _short(added[-1], 50))
            if removed:
                detail_items.append("- " + _short(removed[-1], 50))
            tail = ("  [" + " , ".join(detail_items) + "]") if detail_items else ""
            return f"[~] {ch.label}: {body}{tail}"
        return super().describe_change(ch)

    def restore_ops(
        self, target: list[dict[str, Any]], current: list[dict[str, Any]]
    ) -> list[RestoreOp]:
        cur_by = _by_key(current)
        ops: list[RestoreOp] = []
        for t in target:
            key = str(t["key"])
            c = cur_by.get(key)
            if c is not None and self.change_sig(t) == self.change_sig(c):
                continue
            if t["scope"] == "USER":
                ops.append(_env_make_user_op(t, c))
            else:  # SYSTEM 需管理员，仅报告目标值
                ops.append(
                    RestoreOp(
                        desc=f"[SYSTEM] {t['name']} 需管理员手动改回 {_env_hint(t)}",
                        run=lambda: (False, "系统级需管理员，未自动改"),
                        manual=True,
                    )
                )
        return ops


def _env_hint(rec: dict[str, Any]) -> str:
    if rec.get("kind") == "pathlist":
        return f"（PATH 基线 {len(rec.get('entries') or [])} 条，详见快照）"
    return f": {_short(rec.get('value'))}"


def _env_make_user_op(t: dict[str, Any], c: dict[str, Any] | None) -> RestoreOp:
    name, val, rtype, regpath = t["name"], t["value"], t["regtype"], t["regpath"]

    def run() -> tuple[bool, str]:
        rc, out = run_reg(
            "add", regpath, "/v", name, "/t", rtype, "/d", val, "/f"
        )
        if rc != 0:
            return False, f"写 {name} 失败: {out}"
        _broadcast_env_change()
        return True, f"[USER] {name} 已恢复为基线"

    state = "" if c else "（该项当前丢失，将重建）"
    return RestoreOp(desc=f"[USER] {name} 恢复{_env_hint(t)}{state}", run=run)


# ============================ 默认浏览器 / URL 协议 ============================

_URLASSOC_SUBKEY = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"
_HKCU_CLASSES = r"Software\Classes"
# http/https 走 UrlAssociations UserChoice（和文件关联一样带 Hash，恢复用哈希无关杠杆）
_BROWSER_SCHEMES = ("http", "https")


def _url_userchoice(scheme: str) -> dict[str, Any]:
    progid = reg_read_value(
        winreg.HKEY_CURRENT_USER, f"{_URLASSOC_SUBKEY}\\{scheme}\\UserChoice", "ProgID"
    )
    hash_present = (
        reg_read_value(
            winreg.HKEY_CURRENT_USER, f"{_URLASSOC_SUBKEY}\\{scheme}\\UserChoice", "Hash"
        )
        is not None
    )
    return {"progid": progid, "hash_present": hash_present}


def _exe_for(progid: Any) -> Any:
    if not progid or not isinstance(progid, str):
        return None
    try:
        return C.resolve_command(progid).get("exe")
    except Exception:  # noqa: BLE001
        return None


def _protocol_handler(root: int, base_path: str, scheme: str) -> Any:
    cmd = reg_read_value(root, f"{base_path}\\{scheme}\\shell\\open\\command", "")
    if cmd is None:
        cmd = reg_read_value(root, f"{base_path}\\{scheme}\\shell\\open\\ddeexec", "")
    return cmd


@register
class UrlBrowserItem(WatchItem):
    id = "url_browser"
    name = "默认浏览器 / URL 协议"
    kind = "browser"
    supports_restore = True

    def collect(self) -> list[dict[str, Any]]:
        recs: list[dict[str, Any]] = []

        # 1) 系统默认浏览器（StartMenuInternet 注册值）
        mb = reg_read_value(winreg.HKEY_CURRENT_USER, r"Software\Clients\StartMenuInternet", "")
        recs.append({
            "key": "default_browser", "family": "default_browser",
            "progid": mb, "exe": _exe_for(mb),
        })

        # 2) http/https 生效程序（UrlAssociations UserChoice > 经典层）
        for scheme in _BROWSER_SCHEMES:
            uc = _url_userchoice(scheme)
            classic = reg_read_value(winreg.HKEY_CURRENT_USER, f"{_HKCU_CLASSES}\\{scheme}", "") \
                or reg_read_value(winreg.HKEY_CLASSES_ROOT, scheme, "")
            eff = uc["progid"] or classic
            recs.append({
                "key": f"scheme\\{scheme}", "family": "url_scheme", "scheme": scheme,
                "userchoice_progid": uc["progid"], "userchoice_hash": uc["hash_present"],
                "classic_progid": classic, "effective_progid": eff, "exe": _exe_for(eff),
            })

        # 3) 自定义 URL 协议处理器（steam/cursor 等），分用户级/机器级
        #    判据是「键下有 URL Protocol 值」，scheme 键名通常不带冒号（如 "steam"）
        seen: set[str] = set()
        skip = set(_BROWSER_SCHEMES) | {"file", "ftp"}
        for root, base_path, owner in (
            (winreg.HKEY_CURRENT_USER, _HKCU_CLASSES, "USER"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Classes", "MACHINE"),
        ):
            for name in reg_list_subkeys(root, base_path):
                if name in skip or name in seen:
                    continue
                if reg_read_value(root, f"{base_path}\\{name}", "URL Protocol") is None:
                    continue
                seen.add(name)
                recs.append({
                    "key": f"protocol\\{name}", "family": "protocol", "scheme": name,
                    "owner": owner, "command": _protocol_handler(root, base_path, name),
                })
        return recs

    def change_sig(self, rec: dict[str, Any]) -> Any:
        fam = rec.get("family")
        if fam == "url_scheme":
            return (rec.get("userchoice_progid"), rec.get("classic_progid"))
        if fam == "default_browser":
            return rec.get("progid")
        if fam == "protocol":
            return (rec.get("owner"), rec.get("command"))
        return rec.get("value")

    def label(self, rec: dict[str, Any]) -> str:
        fam = rec.get("family")
        if fam == "protocol":
            return f"协议 {rec.get('scheme')} [{rec.get('owner')}]"
        if fam == "default_browser":
            return f"默认浏览器 -> {rec.get('progid') or '(未设置)'}"
        return f"{rec.get('key')} -> {rec.get('effective_progid') or '(未设置)'}"

    def restore_ops(
        self, target: list[dict[str, Any]], current: list[dict[str, Any]]
    ) -> list[RestoreOp]:
        cur_by = _by_key(current)
        tgt_by = _by_key(target)
        ops: list[RestoreOp] = []

        # A) http/https 被改：哈希无关杠杆（设经典层 + 删 UserChoice）
        for t in target:
            if t.get("family") != "url_scheme":
                continue
            c = cur_by.get(str(t["key"]))
            if c is not None and self.change_sig(t) == self.change_sig(c):
                continue
            want = t.get("effective_progid")
            if not want:
                continue
            ops.append(self._make_scheme_op(str(t["scheme"]), want, (c or {}).get("effective_progid")))

        # B) 默认浏览器：Windows 11 锁定强，自动杠杆常不可靠 -> 引导手动
        tb = tgt_by.get("default_browser")
        cb = cur_by.get("default_browser")
        if tb and (cb is None or self.change_sig(tb) != self.change_sig(cb)):
            ops.append(RestoreOp(
                desc=f"默认浏览器当前 {cb.get('progid') if cb else '?'} != 基线 {tb.get('progid')}，请到 设置->默认应用 重选",
                run=lambda: (False, "需手动（设置页）"), manual=True))

        # C) 新增的自定义协议认领（当前有、基线没有）：用户级可自动删，机器级仅报告
        for c in current:
            if c.get("family") != "protocol":
                continue
            key = str(c["key"])
            if key in tgt_by:
                continue  # 基线也有，非新增
            scheme = str(c.get("scheme"))
            if c.get("owner") == "USER":
                ops.append(self._make_protocol_del_op(scheme))
            else:
                ops.append(RestoreOp(
                    desc=f"[MACHINE] 协议 {scheme} 被新增（需管理员，仅报告）",
                    run=lambda: (False, "机器级需管理员"), manual=True))
        return ops

    @staticmethod
    def _make_scheme_op(scheme: str, want: str, now: Any) -> RestoreOp:
        def run() -> tuple[bool, str]:
            rc1, out1 = run_reg(
                "add", f"HKCU\\{_HKCU_CLASSES}\\{scheme}",
                "/ve", "/t", "REG_SZ", "/d", want, "/f")
            if rc1 != 0:
                return False, f"设经典层失败: {out1}"
            uc = _url_userchoice(scheme)
            if uc["progid"] is not None or uc["hash_present"]:
                rc2, out2 = run_reg(
                    "delete", f"HKCU\\{_URLASSOC_SUBKEY}\\{scheme}\\UserChoice", "/f")
                if rc2 != 0 and "not found" not in out2.lower() and "找不到" not in out2:
                    return True, f"经典层已设，删 UserChoice 返回: {out2}"
            return True, f"协议 {scheme}: {now} -> {want}"
        return RestoreOp(desc=f"协议 {scheme} 恢复 -> {want}（当前 {now}）", run=run)

    @staticmethod
    def _make_protocol_del_op(scheme: str) -> RestoreOp:
        def run() -> tuple[bool, str]:
            rc, out = run_reg("delete", f"HKCU\\{_HKCU_CLASSES}\\{scheme}", "/f")
            if rc != 0 and "not found" not in out.lower() and "找不到" not in out:
                return False, f"删除协议 {scheme} 失败: {out}"
            return True, f"已移除用户级协议认领 {scheme}"
        return RestoreOp(desc=f"移除新增协议认领 {scheme}（用户级）", run=run)


# ============================ 开机自启 ============================

_RUN_HIVES = [
    # (标签, root, subkey, reg显示根, 是否用户级可自动删)
    ("HKCU_Run", winreg.HKEY_CURRENT_USER,
     r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run", True),
    ("HKCU_RunOnce", winreg.HKEY_CURRENT_USER,
     r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce", True),
    ("HKLM_Run", winreg.HKEY_LOCAL_MACHINE,
     r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM\\...\\Run", False),
    ("HKLM_RunOnce", winreg.HKEY_LOCAL_MACHINE,
     r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "HKLM\\...\\RunOnce", False),
    ("HKLM_RunWOW", winreg.HKEY_LOCAL_MACHINE,
     r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "HKLM\\...\\WOW6432Node\\Run", False),
]


def _startup_dirs() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    ad = os.environ.get("APPDATA")
    if ad:
        out.append(("STARTUP_USER", Path(ad) / r"Microsoft\Windows\Start Menu\Programs\Startup"))
    pd = os.environ.get("ProgramData")
    if pd:
        out.append(("STARTUP_COMMON", Path(pd) / r"Microsoft\Windows\Start Menu\Programs\Startup"))
    return out


@register
class AutostartItem(WatchItem):
    id = "autostart"
    name = "开机自启（Run / 启动文件夹）"
    kind = "autostart"
    supports_restore = True

    def collect(self) -> list[dict[str, Any]]:
        recs: list[dict[str, Any]] = []
        for label, root, subkey, _regpath, auto in _RUN_HIVES:
            for name, val in reg_list_values(root, subkey).items():
                if name == "(默认)":
                    continue
                recs.append({
                    "key": f"{label}\\{name}", "family": "run", "hive": label,
                    "name": name, "command": str(val), "auto": auto,
                })
        for label, d in _startup_dirs():
            if not d.exists():
                continue
            for f in d.iterdir():
                try:
                    size = f.stat().st_size if f.is_file() else -1
                except OSError:
                    size = -1
                recs.append({
                    "key": f"{label}\\{f.name}", "family": "startup", "hive": label,
                    "name": f.name, "command": str(f), "size": size, "auto": label == "STARTUP_USER",
                })
        return recs

    def change_sig(self, rec: dict[str, Any]) -> Any:
        if rec.get("family") == "startup":
            return rec.get("size")
        return rec.get("command")

    def label(self, rec: dict[str, Any]) -> str:
        return f"{rec.get('hive')} :: {rec.get('name')}"

    def restore_ops(
        self, target: list[dict[str, Any]], current: list[dict[str, Any]]
    ) -> list[RestoreOp]:
        tgt_by = _by_key(target)
        cur_by = _by_key(current)
        ops: list[RestoreOp] = []
        # 新增的自启项（当前有、基线没有）：用户级 Run 可自动删，其余仅报告
        for c in current:
            key = str(c["key"])
            if key in tgt_by:
                continue
            if c.get("family") == "run" and c.get("auto"):
                ops.append(self._make_run_del_op(str(c["name"])))
            else:
                ops.append(RestoreOp(
                    desc=f"新增 {c.get('hive')}::{c.get('name')}（手动确认是否放行/移除）",
                    run=lambda: (False, "需手动"), manual=True))
        # 命令被改的用户级 Run：报告 old/new，不擅自覆盖（可能应用自己更新的）
        for t in target:
            if t.get("family") != "run":
                continue
            c = cur_by.get(str(t["key"]))
            if c is not None and self.change_sig(t) != self.change_sig(c):
                ops.append(RestoreOp(
                    desc=f"{t.get('hive')}::{t.get('name')} 命令变化 {_short(t.get('command'),40)} -> {_short(c.get('command'),40)}",
                    run=lambda: (False, "需手动判断"), manual=True))
        return ops

    @staticmethod
    def _make_run_del_op(name: str) -> RestoreOp:
        def run() -> tuple[bool, str]:
            rc, out = run_reg(
                "delete", "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                "/v", name, "/f")
            if rc != 0 and "not found" not in out.lower() and "找不到" not in out:
                return False, f"删除自启项 {name} 失败: {out}"
            return True, f"已移除自启项 HKCU::Run::{name}"
        return RestoreOp(desc=f"移除新增自启项 HKCU::Run::{name}", run=run)


# ============================ Winlogon 劫持监控 ============================
# 经典恶意/流氓持久化面：改 Shell（登录后跑的"外壳"，正常恒为 explorer.exe）
# 或在 HKCU 凭空加一个 Shell 覆盖机器级值。这两处几乎没人会想起来查，
# 一旦偏离就是登录链路被劫持。只盯 Shell / Userinit 两项，零噪音、高信号。

_WINLOGON_SUBKEY = r"Software\Microsoft\Windows NT\CurrentVersion\Winlogon"
_WINLOGON_SCOPES = [
    # (scope, root, reg 显示根, 是否用户级可自动写)
    ("USER", winreg.HKEY_CURRENT_USER, "HKCU\\" + _WINLOGON_SUBKEY, True),
    ("MACHINE", winreg.HKEY_LOCAL_MACHINE, "HKLM\\" + _WINLOGON_SUBKEY, False),  # 写 HKLM 需管理员，仅报告
]
# 采集固定这组值名：即使当前不存在也记一条 value=None，
# 便于「基线没有、后来被凭空加上（劫持）」被判成 changed 而非漏掉。
_WINLOGON_VALUES = ("Shell", "Userinit")
# 各值名的"标准样子"，仅用于可读提示（不作为恢复依据——恢复一律回到基线值）
_WINLOGON_EXPECTED = {
    "Shell": "explorer.exe",
    "Userinit": "userinit.exe",  # 完整标准值是 "C:\Windows\system32\userinit.exe,"（末尾逗号），这里只做包含判断
}


def _winlogon_hint(name: str, val: Any) -> str:
    """给一个可读的『是否符合标准值』标注（软提示，不硬判）。"""
    exp = _WINLOGON_EXPECTED.get(name)
    if val is None:
        return "（未设置）" if name == "Shell" else "（未设置，正常）"
    s = str(val)
    if name == "Shell":
        return "" if s.strip().lower() == "explorer.exe" else "  !! 非 explorer.exe"
    if name == "Userinit":
        return "" if exp and exp.lower() in s.lower() else "  !! 不含 userinit.exe"
    return ""


@register
class WinlogonItem(WatchItem):
    id = "winlogon"
    name = "Winlogon 劫持监控（Shell / Userinit）"
    kind = "security"
    supports_restore = True
    restore_requires_admin = False  # HKCU 自动、HKLM 仅报告，逐 op 标注

    def collect(self) -> list[dict[str, Any]]:
        recs: list[dict[str, Any]] = []
        for scope, root, regpath, auto in _WINLOGON_SCOPES:
            for name in _WINLOGON_VALUES:
                val = reg_read_value(root, _WINLOGON_SUBKEY, name)
                recs.append({
                    "key": f"{scope}\\{name}", "scope": scope, "name": name,
                    "value": val, "regpath": regpath, "auto": auto,
                })
        return recs

    def change_sig(self, rec: dict[str, Any]) -> Any:
        return rec.get("value")  # None 表示未设置，与实际字符串区分，能抓「凭空加」

    def label(self, rec: dict[str, Any]) -> str:
        return f"[{rec.get('scope')}] {rec.get('name')}"

    def describe_change(self, ch: Any) -> str:
        b, a = ch.before or {}, ch.after or {}
        hint = _winlogon_hint(str(b.get("name") or a.get("name")), a.get("value"))
        return super().describe_change(ch) + hint

    def restore_ops(
        self, target: list[dict[str, Any]], current: list[dict[str, Any]]
    ) -> list[RestoreOp]:
        cur_by = _by_key(current)
        ops: list[RestoreOp] = []
        for t in target:
            key = str(t["key"])
            c = cur_by.get(key)
            if c is not None and self.change_sig(t) == self.change_sig(c):
                continue
            scope, name = t["scope"], t["name"]
            want = t.get("value")  # 基线值（可能为 None = 基线本就没有）
            now = c.get("value") if c else None
            if t["auto"] and scope == "USER":
                ops.append(self._make_user_op(str(t["regpath"]), name, want, now))
            else:
                ops.append(RestoreOp(
                    desc=f"[MACHINE] {name} 当前 {now!r} != 基线 {want!r}，写 HKLM 需管理员，请手动改回",
                    run=lambda: (False, "机器级需管理员，未自动改"), manual=True))
        # 基线没有、当前却多出来的 USER 值（add-on 劫持）：上面按 target 遍历漏不到，
        # 因为我们对全部固定 key 都建了基线记录（value=None）。无需额外 added 分支。
        return ops

    @staticmethod
    def _make_user_op(regpath: str, name: str, want: Any, now: Any) -> RestoreOp:
        def run() -> tuple[bool, str]:
            if want is None:
                # 基线本无此值（多半是 HKCU 被凭空加了 Shell）-> 删掉，回到未设置
                rc, out = run_reg("delete", regpath, "/v", name, "/f")
                if rc != 0 and "not found" not in out.lower() and "找不到" not in out and "未找到" not in out:
                    return False, f"删除 {name} 失败: {out}"
                return True, f"[USER] {name} 已删除（回到基线未设置态）"
            rc, out = run_reg("add", regpath, "/v", name, "/t", "REG_SZ", "/d", str(want), "/f")
            if rc != 0:
                return False, f"写 {name} 失败: {out}"
            return True, f"[USER] {name}: {now!r} -> {want!r}"

        verb = "删除（回到未设置）" if want is None else f"改回 {want!r}"
        return RestoreOp(desc=f"[USER] {name} 当前 {now!r} -> {verb}", run=run)



# ============================ TCP 动态端口范围 ============================

# 为什么值得纳管：后端固定端口（如 8766）被 Windows 端口保留区顶掉时，
# 表面现象是「昨天能起、今天一启动就关」，真实根因常常是**动态端口范围被改到低位**
# （WinNAT 只能从低位划保留块，正好盖住常用端口）。而改这个值的往往不是我们：
# 「网络优化」「游戏加速器」「系统精简脚本」都会顺手写 MaxUserPort / dynamicport。
# 所以这里把注册表原值纳入快照 —— 被改回去时能 diff 出来、也能按基线改回。
_TCPIP_PARAMS = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
_TCPIP_REG_PATH = "HKLM\\" + _TCPIP_PARAMS


def _tcp_port_values() -> dict[str, tuple[Any, int]]:
    """枚举 Tcpip\\Parameters 下我们关心的端口参数 -> {name: (value, regtype)}。"""
    wanted = {
        "DynamicPortRangeStartPort",
        "DynamicPortRangeNumberOfPorts",
        "MaxUserPort",
        "ReservedPorts",
        "PersistentPortExclusions",
    }
    out: dict[str, tuple[Any, int]] = {}
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _TCPIP_PARAMS, 0, winreg.KEY_READ)
    except OSError:
        return out
    try:
        i = 0
        while True:
            try:
                name, val, typ = winreg.EnumValue(k, i)
            except OSError:
                break
            if name in wanted:
                out[name] = (val, typ)
            i += 1
    finally:
        winreg.CloseKey(k)
    return out


@register
class TcpPortRangeItem(WatchItem):
    id = "tcp_port_range"
    name = "TCP 动态端口范围 / 保留段（端口被顶走的根因）"
    kind = "network"
    supports_restore = True
    restore_requires_admin = True

    _NAMES = (
        "DynamicPortRangeStartPort",
        "DynamicPortRangeNumberOfPorts",
        "MaxUserPort",
        "ReservedPorts",
        "PersistentPortExclusions",
    )

    def collect(self) -> list[dict[str, Any]]:
        vals = _tcp_port_values()
        recs: list[dict[str, Any]] = []
        for name in self._NAMES:
            present = name in vals
            value, typ = vals[name] if present else (None, 0)
            recs.append(
                {
                    "key": name,
                    "present": present,
                    "value": value,
                    "regtype": typ,
                    "regpath": _TCPIP_REG_PATH,
                }
            )
        return recs

    def label(self, rec: dict[str, Any]) -> str:
        return f"TCP 参数 {rec.get('key')}"

    def describe_change(self, ch: Any) -> str:
        b, a = ch.before or {}, ch.after or {}
        if ch.kind == "added":
            return f"[+] {ch.label} 出现：{a.get('value')!r}"
        if ch.kind == "removed":
            return f"[-] {ch.label} 消失（原 {b.get('value')!r}）"
        return f"[~] {ch.label}: {b.get('value')!r} -> {a.get('value')!r}"

    def restore_ops(
        self, target: list[dict[str, Any]], current: list[dict[str, Any]]
    ) -> list[RestoreOp]:
        cur_by = _by_key(current)
        ops: list[RestoreOp] = []
        for t in target:
            name = str(t["key"])
            c = cur_by.get(name)
            if c is not None and self.change_sig(t) == self.change_sig(c):
                continue
            ops.append(self._make_op(name, t, c))
        return ops

    @staticmethod
    def _make_op(name: str, want_rec: dict[str, Any], now_rec: dict[str, Any] | None) -> RestoreOp:
        present = bool(want_rec.get("present"))
        want = want_rec.get("value")
        typ = int(want_rec.get("regtype") or 0)
        now = now_rec.get("value") if now_rec else "(缺失)"

        if present and typ == winreg.REG_MULTI_SZ:
            # reg add 对 REG_MULTI_SZ 的分隔符处理很别扭，宁可让用户手工改，不冒险
            return RestoreOp(
                desc=f"[MACHINE] {name} 当前 {now!r} != 基线 {want!r}（多字符串值需手工改）",
                run=lambda: (False, "REG_MULTI_SZ 未自动写"),
                manual=True,
            )

        def run() -> tuple[bool, str]:
            if not present:
                rc, out = run_reg("delete", _TCPIP_REG_PATH, "/v", name, "/f")
                if rc != 0 and "找不到" not in out and "not found" not in out.lower():
                    return False, f"删除 {name} 失败: {out}"
                return True, f"{name} 已删除（回到基线未设置态）"
            if typ == winreg.REG_DWORD:
                rc, out = run_reg(
                    "add", _TCPIP_REG_PATH, "/v", name, "/t", "REG_DWORD", "/d", str(int(want)), "/f"
                )
            else:
                rc, out = run_reg(
                    "add", _TCPIP_REG_PATH, "/v", name, "/t", "REG_SZ", "/d", str(want), "/f"
                )
            if rc != 0:
                return False, f"写 {name} 失败: {out}"
            return True, f"{name}: {now!r} -> {want!r}"

        verb = "删除（回到未设置）" if not present else f"改回 {want!r}"
        return RestoreOp(desc=f"[MACHINE] {name} 当前 {now!r} -> {verb}", run=run)

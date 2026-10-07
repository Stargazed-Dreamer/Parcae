"""settings_watch —— 通用「设置看门狗」引擎

针对一整类「会被软件偷偷改、你很难察觉、手动恢复又麻烦」的机器设置，提供统一闭环：
    快照 snapshot  →  比对 diff（谁被从 A 改成 B）  →  恢复 restore（分档 + 诚实边界）

每项设置是一个 WatchItem（provider），只声明「怎么采集」和「怎么恢复」；
diff / 快照存储 / 基线 / CLI 由本引擎统一驱动。新增一项 = 写一个 provider 注册进来。

已内置条目见 items.py（file_associations / env_path / url_browser / autostart）。
文件关联的深工具（CSV 清单等）仍是同级 scripts/assoc_*.py，本引擎复用其解析逻辑。

约定（本机 Windows，务必遵守，否则反复踩坑）：
  - 打印只用 ASCII 符号 + 中文（GBK 终端能显示），禁 emoji/箭头/制表符，否则崩。
  - 调 reg 一律用 subprocess 列表形式，勿经 bash（MSYS 会把 /ve /d /f 当路径转换）。
  - 采集/只读用 winreg（HKCU/HKCR/HKLM 读取免提权）；写 HKCU 免提权，写 HKLM 需管理员。
"""

from __future__ import annotations

import json
import subprocess
import winreg
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

BACKUP_BASE = Path(r"<data_drive>:/<backup_root>\SystemBackup")
WATCH_ROOT = BACKUP_BASE / "settings_watch"


# ============================ 恢复动作 ============================

@dataclass
class RestoreOp:
    """一个可执行的恢复动作。run() 返回 (ok, detail)。"""

    desc: str
    run: Callable[[], tuple[bool, str]]
    manual: bool = False  # True = 无法自动，仅提示（诚实标注）


@dataclass
class Change:
    """一项差异记录（跨条目统一结构）。"""

    key: str
    kind: str  # added / removed / changed
    before: Optional[dict[str, Any]]
    after: Optional[dict[str, Any]]
    label: str
    detail: str = ""  # 变化字段的可读描述


# ============================ provider 协议 ============================

class WatchItem:
    id: str = ""
    name: str = ""
    kind: str = "misc"  # assoc / path / browser / autostart，供 CLI 分组
    supports_restore: bool = False
    restore_requires_admin: bool = False  # 部分恢复需管理员（如实标注）

    def collect(self) -> list[dict[str, Any]]:
        """返回归一化记录列表，每条必须含唯一 'key' 字段。"""
        raise NotImplementedError

    def change_sig(self, rec: dict[str, Any]) -> Any:
        """判定「算不算变了」的指纹，默认忽略 key 与 volatile 字段。"""
        return {k: v for k, v in rec.items() if k not in ("key", "_ts")}

    def label(self, rec: dict[str, Any]) -> str:
        return str(rec.get("key"))

    def describe_change(self, ch: Change) -> str:
        """给 diff 用的可读差异描述；provider 可覆写得更有信息量。"""
        if ch.kind == "added":
            return f"[+] 新增 {ch.label}"
        if ch.kind == "removed":
            return f"[-] 消失 {ch.label}"
        b, a = ch.before or {}, ch.after or {}
        diffs = []
        keys = set(b) | set(a)
        for k in sorted(keys):
            if k in ("key", "_ts"):
                continue
            if self._sig_field(b.get(k)) != self._sig_field(a.get(k)):
                diffs.append(f"{k}: {b.get(k)!r} -> {a.get(k)!r}")
        return f"[~] 改动 {ch.label}" + (f"  ({'; '.join(diffs)})" if diffs else "")

    @staticmethod
    def _sig_field(v: Any) -> Any:
        return v

    def restore_ops(self, target: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[RestoreOp]:
        """基于基线目标 target 与当前 current，产出恢复动作。默认无。"""
        return []


_REGISTRY: dict[str, WatchItem] = {}


def register(cls: type[WatchItem]) -> type[WatchItem]:
    inst = cls()
    if not inst.id:
        raise ValueError(f"provider {cls.__name__} 缺少 id")
    _REGISTRY[inst.id] = inst
    return cls


def all_items() -> list[WatchItem]:
    return list(_REGISTRY.values())


def get_item(item_id: str) -> Optional[WatchItem]:
    return _REGISTRY.get(item_id)


# ============================ 通用 diff ============================

def diff_records(item: WatchItem, base: list[dict[str, Any]], cur: list[dict[str, Any]]) -> list[Change]:
    b = {r["key"]: r for r in base}
    c = {r["key"]: r for r in cur}
    out: list[Change] = []
    for key in sorted(set(b) | set(c)):
        rb, rc = b.get(key), c.get(key)
        if rb is None:
            ch = Change(key, "added", None, rc, item.label(rc or {}))
        elif rc is None:
            ch = Change(key, "removed", rb, None, item.label(rb))
        elif item.change_sig(rb) != item.change_sig(rc):
            ch = Change(key, "changed", rb, rc, item.label(rc))
        else:
            continue
        ch.detail = item.describe_change(ch)
        out.append(ch)
    return out


# ============================ 快照存储 ============================

def new_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def snapshot_dir(ts: str) -> Path:
    return WATCH_ROOT / ts


def save_snapshot(ts: str, data: dict[str, Any]) -> Path:
    d = snapshot_dir(ts)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "snapshot.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_snapshot(ts: str) -> dict[str, Any]:
    with (snapshot_dir(ts) / "snapshot.json").open("r", encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


def list_snapshots() -> list[str]:
    if not WATCH_ROOT.exists():
        return []
    return sorted(
        p.name for p in WATCH_ROOT.iterdir() if p.is_dir() and (p / "snapshot.json").exists()
    )


def latest_snapshot() -> Optional[str]:
    s = list_snapshots()
    return s[-1] if s else None


def set_baseline(ts: str) -> None:
    (WATCH_ROOT / "baseline.json").write_text(
        json.dumps({"ts": ts, "set_at": datetime.now().astimezone().isoformat(timespec="seconds")}, ensure_ascii=False),
        encoding="utf-8",
    )


def current_baseline() -> Optional[str]:
    p = WATCH_ROOT / "baseline.json"
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f).get("ts")
    except Exception:
        return None


def build_snapshot(item_ids: Optional[list[str]] = None) -> dict[str, Any]:
    """对指定（或全部）条目采集，错误隔离，返回可存储快照结构。"""
    data: dict[str, Any] = {
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "items": {},
    }
    for it in all_items():
        if item_ids and it.id not in item_ids:
            continue
        try:
            recs = it.collect()
            data["items"][it.id] = {"name": it.name, "error": None, "count": len(recs), "records": recs}
        except Exception as e:  # noqa: BLE001
            data["items"][it.id] = {"name": it.name, "error": str(e), "count": 0, "records": []}
    return data


# ============================ 注册表小工具（读免提权 / 写 HKCU 免提权） ============================

def reg_list_values(root: int, subkey: str) -> dict[str, Any]:
    """返回某键下所有 (name -> value)。不存在返回 {}。"""
    out: dict[str, Any] = {}
    try:
        k = winreg.OpenKey(root, subkey, 0, winreg.KEY_READ)
    except OSError:
        return out
    try:
        i = 0
        while True:
            try:
                name, val, _t = winreg.EnumValue(k, i)
            except OSError:
                break
            out[name or "(默认)"] = val
            i += 1
    finally:
        winreg.CloseKey(k)
    return out


def reg_list_subkeys(root: int, subkey: str) -> list[str]:
    out: list[str] = []
    try:
        k = winreg.OpenKey(root, subkey, 0, winreg.KEY_READ)
    except OSError:
        return out
    try:
        i = 0
        while True:
            try:
                out.append(winreg.EnumKey(k, i))
            except OSError:
                break
            i += 1
    finally:
        winreg.CloseKey(k)
    return out


def reg_read_value(root: int, subkey: str, name: str = "") -> Optional[Any]:
    try:
        k = winreg.OpenKey(root, subkey, 0, winreg.KEY_READ)
    except OSError:
        return None
    try:
        if name == "":
            v, _t = winreg.QueryValueEx(k, "")
        else:
            v, _t = winreg.QueryValueEx(k, name)
        return v
    except OSError:
        return None
    finally:
        winreg.CloseKey(k)


def run_reg(*args: str) -> tuple[int, str]:
    """subprocess 列表形式调 reg（绕开 MSYS 路径转换）。"""
    try:
        r = subprocess.run(["reg", *args], capture_output=True, text=True, timeout=15)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return 1, str(e)

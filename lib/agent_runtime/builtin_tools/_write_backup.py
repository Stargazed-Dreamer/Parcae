"""覆盖写之前的可回滚副本（unknown-outcome-evidence T19）

`builtin_tool_executor.py` 给 `file_write` / `file_edit` 的定级注释是
"有副作用但**可逆**"。实测这个断言不成立：覆盖一个已存在的文件，旧内容当场没了，
项目里也没有任何副本——"可逆"是写在注释里的愿望，不是系统属性。

这里把它变成属性：覆盖前把旧字节复制一份到 `temp/write_backups/`，
并把副本路径回写进工具结果，让人和模型都看得见退路在哪。

范围克制：
- 只在目标文件**已存在**时备份（新建文件没有可回滚的东西）
- 备份失败不阻断写入（副本是保险，不是闸门），但会吵一句
- 按 mtime 保留 BACKUP_RETENTION_DAYS 天，进程内只扫一次，避免每次写都遍历
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

logger = logging.getLogger("parcae.agent.write_backup")

BACKUP_DIR = Path(__file__).resolve().parents[4] / "temp" / "write_backups"
BACKUP_RETENTION_DAYS = 7
# 单个文件最多留几份副本（超出删最旧的），避免反复重写同一文件把目录撑爆
MAX_COPIES_PER_FILE = 5

_pruned_this_process = False


def _tag_for(path: Path) -> str:
    """按绝对路径算稳定前缀，便于按原文件归组与查找。"""
    digest = hashlib.sha256(str(path).encode("utf-8", errors="replace")).hexdigest()[:16]
    return digest


def _prune_once() -> None:
    """按保留天数清理旧副本（每个进程只做一次，失败静默）。"""
    global _pruned_this_process
    if _pruned_this_process or not BACKUP_DIR.exists():
        return
    _pruned_this_process = True
    cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
    try:
        for stale in BACKUP_DIR.glob("*.bak"):
            try:
                if stale.stat().st_mtime < cutoff:
                    stale.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError as e:
        logger.debug("清理写入副本失败: %s", e)


def backup_before_overwrite(target: Path) -> str | None:
    """已存在的文件被覆盖前留一份副本，返回副本路径；无需备份或失败返回 None。"""
    try:
        if not target.exists() or not target.is_file():
            return None
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        _prune_once()
        tag = _tag_for(target)
        stamp = int(time.time() * 1000)
        copy_path = BACKUP_DIR / f"{tag}_{stamp}.bak"
        copy_path.write_bytes(target.read_bytes())
        # 同名前缀归组，超上限就丢最旧的
        siblings = sorted(BACKUP_DIR.glob(f"{tag}_*.bak"), key=lambda p: p.name)
        for oldest in siblings[:-MAX_COPIES_PER_FILE]:
            try:
                oldest.unlink(missing_ok=True)
            except OSError:
                continue
        # 附一份原路径映射，让人能用原路径找回副本
        try:
            (BACKUP_DIR / f"{tag}.origin.txt").write_text(str(target), encoding="utf-8")
        except OSError:
            pass
        return str(copy_path)
    except Exception as e:
        # 保险没做成，但不因此拦住正常写入
        logger.warning("写入前备份失败 %s: %s", target, e)
        return None

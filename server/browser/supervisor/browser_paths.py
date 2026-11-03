"""Chromium 浏览器可执行文件探测与启动参数。

与 tools/browser/start_debug_browser.py 的候选表保持一致；本模块先供 supervisor 使用，
后续把工具脚本也切到这里，避免两套探测漂移。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# 按优先级排序；%LOCALAPPDATA% / %ProgramFiles% 在运行时展开。
BROWSER_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe",
)

_PATH_COMMANDS: tuple[str, ...] = ("chrome", "google-chrome", "chromium", "msedge", "brave", "vivaldi")


def _expand(path: str) -> str:
    return os.path.expandvars(path)


def detect_browser_path(explicit: str = "") -> str | None:
    """返回第一个存在的浏览器可执行文件；explicit 非空时只认它。"""
    if explicit:
        candidate = Path(_expand(explicit)).expanduser()
        return str(candidate) if candidate.is_file() else None
    for raw in BROWSER_CANDIDATES:
        candidate = Path(_expand(raw))
        if candidate.is_file():
            return str(candidate)
    for command in _PATH_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    return None


def build_browser_args(*, browser_path: str, port: int, profile_dir: Path) -> list[str]:
    """第一版启动参数：真 Chrome、独立 profile、无 headless、无自动化开关。"""
    return [
        browser_path,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]

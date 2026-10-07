"""整机备份扩展导出组（补齐 backup_env.py 迁移时会漏掉的"只有原机才配得好"的东西）。

分四组，各自独立可测，产物落在 {BACKUP_DIR}/<组名>/ 下：

  credentials  凭据/密钥：WiFi(明文 profile)、SSH/GPG/git/npm/aws/gh/docker dotfiles、
               Chrome/Edge 隐私（走 HackBrowserData：archive 可迁移 profile + dumpkeys
               主密钥 + dump 可读 csv）
  appearance   壁纸文件本体 + 桌面壁纸/样式注册表、深浅色/强调色/透明、已装字体清单、
               输入法用户词库
  policy       防火墙规则、计划任务清单、第三方驱动、打印机、VPN 拨号(pbk)
  devtools     VSCode/Cursor/Trae/JetBrains 配置(仅 User 层，不拷巨型缓存)、AutoHotkey/改键表、
               PowerToys、WSL 发行版导出、shell dotfiles

设计纪律（本项目反复踩过的坑）：
  * 所有子进程一律列表形式调用 + encoding="utf-8", errors="replace"（git-bash 会把 /ve /d
    当路径转换；中文 Windows 下 netsh/wsl 输出走 GBK，不显式指定编码会崩 reader 线程）。
  * 打印只用 ASCII 符号 + 中文，禁 emoji/箭头（GBK 终端）。
  * 绝不把解密的明文密码/token 打进日志或上下文——只报"文件名/记录数/字节数"。
  * 诚实边界：需管理员或本机不存在的命令（printbrm 等）如实降级为提示，不谎称已备份。
  * DPAPI 绑机的东西（Chrome Login Data 等）单拷文件到新机无用 → 用 archive+dumpkeys 组合才能跨机解密。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
import winreg
from pathlib import Path

# 中文 Windows 控制台是 GBK：外部命令解码出的个别乱码字符会让 print 直接崩
# （UnicodeEncodeError）。把 stdout/stderr 设为 errors="replace" —— 中文照常显示，
# 不可编码字符降级为 '?'，全程不崩。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


# ---------- 通用小工具 ----------

def _log(msg: str) -> None:
    print("  " + msg)


def run(cmd: list[str], timeout: int = 120, dec: str = "mbcs") -> tuple[int, str, str]:
    """列表形式跑命令，绝不抛异常。返回 (rc, stdout, stderr)。

    中文 Windows 下 netsh/wsl/pnputil/advfirewall 的输出走控制台 ANSI 码页（GBK），
    所以默认用 mbcs 解码才能读通中文提示；要拿 XML/JSON 正文时传 dec="utf-8"。
    统一 errors="replace"，任何情况都不崩 reader 线程。"""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return (r.returncode,
                (r.stdout or b"").decode(dec, "replace"),
                (r.stderr or b"").decode(dec, "replace"))
    except FileNotFoundError:
        return 127, "", f"命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"超时({timeout}s): {' '.join(cmd)}"
    except Exception as e:  # noqa: BLE001
        return 1, "", f"{type(e).__name__}: {e}"


def safe_copy(src: Path, dst: Path) -> tuple[bool, str]:
    if not src.exists():
        return False, "不存在"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"


def reg_read(root: int, key: str, name: str):
    try:
        k = winreg.OpenKey(root, key)
        v, _ = winreg.QueryValueEx(k, name)
        winreg.CloseKey(k)
        return v
    except OSError:
        return None


def reg_export(key: str, dst: Path) -> tuple[bool, str]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    rc, out, err = run(["reg", "export", key, str(dst), "/y"], timeout=20)
    return rc == 0, (err or out).strip()[:160]


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home())))


# ---------- HackBrowserData 定位 / 获取 ----------

def _hbd_cache_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", _home())) / "LocalAgent" / "bin" / "hbd"
    return base


def ensure_hack_browser_data() -> Path | None:
    """返回可用的 hack-browser-data.exe 路径；缓存里有就复用，否则下载官方 release。
    允许用环境变量 HBD_EXE 直接指定（测试用）。找不到/下载失败返回 None（上层降级）。"""
    env = os.environ.get("HBD_EXE")
    if env and Path(env).exists():
        return Path(env)
    cache = _hbd_cache_dir()
    found = next(cache.glob("hack-browser-data*.exe"), None)
    if found:
        return found
    # 下载到缓存目录（重试吸收冷启动 TLS 超时）
    url = ("https://github.com/moonD4rk/HackBrowserData/releases/download/"
           "v1.1.0/hack-browser-data-windows-64bit.zip")
    cache.mkdir(parents=True, exist_ok=True)
    zp = cache / "hbd.zip"
    for attempt in range(3):
        rc, out, err = run(["curl.exe", "-L", "--fail", "-sS", "--retry", "2",
                            "-o", str(zp), url], timeout=180)
        if rc == 0 and zp.exists() and zp.stat().st_size > 1_000_000:
            try:
                with zipfile.ZipFile(zp) as z:
                    z.extractall(cache)
                zp.unlink(missing_ok=True)
                exe = next(cache.glob("hack-browser-data*.exe"), None)
                if exe:
                    return exe
            except Exception as e:  # noqa: BLE001
                _log(f"[!] 解压失败: {e}")
        else:
            _log(f"[.] 下载尝试 {attempt+1}/3 未成功（冷启动 TLS 抖动），重试…")
    return None


def _browser_privacy(hbd: Path, browser: str, out_dir: Path) -> None:
    """对单个浏览器产：可迁移 archive(zip) + dumpkeys(主密钥 json) + dump(可读 csv)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    arc = out_dir / f"{browser}-profile.zip"
    rc, out, err = run([str(hbd), "archive", "-b", browser, "-c", "all",
                        "-o", str(arc)], timeout=300)
    if rc == 0 and arc.exists():
        _log(f"[已备份] {browser} profile 归档 {arc.name} ({human(arc.stat().st_size)})")
    else:
        _log(f"[失败] {browser} archive: {(err or out).strip()[:120]}")
    keys = out_dir / f"{browser}-masterkeys.json"
    rc, out, err = run([str(hbd), "dumpkeys", "-b", browser, "-o", str(keys)], timeout=120)
    if rc == 0 and keys.exists():
        _log(f"[已备份] {browser} 主密钥 {keys.name}（跨机解密用，含明文密钥，敏感）")
    else:
        _log(f"[失败] {browser} dumpkeys: {(err or out).strip()[:120]}")
    dump = out_dir / "readable"
    rc, out, err = run([str(hbd), "dump", "-b", browser, "-c", "all",
                        "-f", "csv", "-d", str(dump)], timeout=300)
    if rc == 0 and dump.exists():
        cats = [p.name for p in dump.glob("*.csv")]
        _log(f"[已导出] {browser} 可读 csv: {', '.join(cats) or '(空)'}")
    else:
        _log(f"[提示] {browser} dump 未产出（可能无该浏览器/权限）: {(err or out).strip()[:100]}")


# ---------- 组 1：凭据/密钥 ----------

# 只拷"小、含身份/密钥、丢了要重配"的 dotfile；按存在与否处理，大的跳过并说明。
_DOT_CANDIDATES = [
    (".ssh", "dir"),          # SSH 公私钥、config、known_hosts
    (".aws", "dir"),          # aws 凭据/配置
    (".gnupg", "dir"),        # GPG 私钥
    (".npmrc", "file"),       # npm registry auth token
    (".netrc", "file"),       # 通用登录 token
    (".git-credentials", "file"),   # git 明文凭据（store helper）
    (".config/gh/hosts.yml", "file"),   # GitHub CLI token
    (".docker/config.json", "file"),    # docker registry auth
    (".condarc", "file"),
    (".gitconfig", "file"),   # git 身份/别名（非机密但丢了要重配）
    (".claude.json", "file"),
    (".p12", "file"),
]
_DOT_MAX = 50 * 1024 * 1024  # 单个候选超过 50MB 视为可能含缓存，跳过并提示


def backup_credentials(backup_dir: Path) -> None:
    print("=" * 50)
    print("凭据/密钥（明文备份 —— 含敏感数据，filesync 同步前请三思）")
    print("=" * 50)
    cred = backup_dir / "credentials"
    cred.mkdir(parents=True, exist_ok=True)

    # WiFi：导出全部 profile（含明文 PSK），用产出文件数判定，不解析中文标签
    wifi = cred / "wifi"
    wifi.mkdir(parents=True, exist_ok=True)
    rc, out, err = run(["netsh", "wlan", "export", "profile",
                        "key=clear", f"folder={wifi}"], timeout=60)
    n = len(list(wifi.glob("*.xml")))
    if n:
        _log(f"[已备份] WiFi {n} 个网络 profile（XML 含明文密码）")
    else:
        _log(f"[提示] WiFi 未导出（无保存网络或需权限）: {(err or out).strip()[:100]}")

    # dotfiles / 密钥
    dd = cred / "dotfiles"
    dd.mkdir(parents=True, exist_ok=True)
    home = _home()
    hit = []
    for rel, kind in _DOT_CANDIDATES:
        p = home / rel
        if not p.exists():
            continue
        sz = dir_size(p)
        if sz > _DOT_MAX:
            _log(f"[跳过] {rel} 达 {human(sz)}（疑似含缓存，过大不自动拷）")
            continue
        ok, msg = safe_copy(p, dd / rel)
        if ok:
            hit.append(f"{rel}({human(sz)})")
        else:
            _log(f"[失败] {rel}: {msg}")
    _log(f"[已备份] 凭据类 dotfiles: {', '.join(hit) or '(无命中)'}")

    # 凭据管理器：无官方批量导出，写手动指引
    (cred / "credential_manager_README.txt").write_text(
        "Windows 凭据管理器里的条目无法命令行批量导出（且多为 DPAPI 绑机）。\n"
        "迁移前手动核对：控制面板 > 凭据管理器 > Windows 凭据，逐个"
        "『显示】查看密码后手动重录，或依赖各应用的云同步。\n",
        encoding="utf-8")

    # 浏览器隐私（Chrome/Edge）
    browser_dir = backup_dir / "browser"
    hbd = ensure_hack_browser_data()
    if hbd:
        _log(f"[工具] HackBrowserData: {hbd}")
        for b in ("chrome", "edge"):
            _browser_privacy(hbd, b, browser_dir / b)
    else:
        _log("[!] 未能获取 HackBrowserData（离线/下载失败）——"
             "跳过浏览器隐私导出；恢复网络后重跑 --credentials")


# ---------- 组 2：外观 + 输入法词库 ----------

def backup_appearance(backup_dir: Path) -> None:
    print("=" * 50)
    print("外观 / 主题 / 字体 / 输入法词库")
    print("=" * 50)
    ap = backup_dir / "appearance"
    ap.mkdir(parents=True, exist_ok=True)
    HKCU = winreg.HKEY_CURRENT_USER

    # 壁纸：拷文件本体 + 记注册表参数（路径不拷，新机上原路径多半不存在）
    desk = r"Control Panel\Desktop"
    wp = reg_read(HKCU, desk, "Wallpaper")
    info = {
        "Wallpaper": wp,
        "WallpaperStyle": reg_read(HKCU, desk, "WallpaperStyle"),
        "TileWallpaper": reg_read(HKCU, desk, "TileWallpaper"),
        "Slideshow": reg_read(HKCU, desk, "Slideshow"),
    }
    (ap / "wallpaper.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    if wp and Path(wp).exists():
        ok, msg = safe_copy(Path(wp), ap / "wallpaper" / Path(wp).name)
        _log(f"[{'已备份' if ok else '失败'}] 壁纸文件本体 {Path(wp).name} {('' if ok else msg)}")
    elif wp:
        _log(f"[提示] 壁纸路径不存在: {wp}")

    # 主题：深浅色 / 透明 / 强调色
    per = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    theme = {
        "AppsUseLightTheme": reg_read(HKCU, per, "AppsUseLightTheme"),
        "SystemUsesLightTheme": reg_read(HKCU, per, "SystemUsesLightTheme"),
        "EnableTransparency": reg_read(HKCU, per, "EnableTransparency"),
    }
    (ap / "theme.json").write_text(json.dumps(theme, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"[已记录] 深浅色/透明 Apps={theme['AppsUseLightTheme']} Sys={theme['SystemUsesLightTheme']} Trans={theme['EnableTransparency']}")
    reg_export(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes", ap / "themes.reg")

    # 字体：清单 + 非系统自带字体文件（保守：列清单，拷 %LOCALAPPDATA%\Microsoft\Windows\Fonts 用户字体）
    import glob as _glob
    fonts = sorted(os.path.basename(x) for x in _glob.glob(r"C:\Windows\Fonts\*"))
    (ap / "fonts_inventory.txt").write_text("\n".join(fonts), encoding="utf-8")
    ufonts = Path(os.environ.get("LOCALAPPDATA", _home())) / "Microsoft" / "Windows" / "Fonts"
    if ufonts.exists() and any(ufonts.iterdir()):
        ok, _ = safe_copy(ufonts, ap / "user_fonts")
        _log(f"[{'已备份' if ok else '失败'}] 用户安装字体目录（系统字体不拷，重装自带）")
    _log(f"[已记录] 字体清单 {len(fonts)} 项 → fonts_inventory.txt")

    # 输入法用户词库
    ime_cands = [
        Path(os.environ.get("APPDATA", _home())) / "Microsoft" / "InputMethod",
        Path(os.environ.get("APPDATA", _home())) / "Sogou",
        Path(os.environ.get("LOCALAPPDATA", _home())) / "SogouIme",
        _home() / "AppData" / "LocalLow" / "Sogou",
    ]
    copied = []
    for p in ime_cands:
        if p.exists() and dir_size(p) and dir_size(p) < 200 * 1024 * 1024:
            ok, _ = safe_copy(p, ap / "ime" / p.name)
            if ok:
                copied.append(p.name)
    _log(f"[已备份] 输入法词库: {', '.join(copied) or '(未命中已知输入法数据目录)'}")


# ---------- 组 3：系统策略 / 外设 ----------

def backup_policy(backup_dir: Path, with_drivers: bool = False) -> None:
    print("=" * 50)
    print("系统策略 / 外设（防火墙·计划任务·驱动·打印机·VPN）")
    print("=" * 50)
    po = backup_dir / "policy"
    po.mkdir(parents=True, exist_ok=True)

    # 防火墙（导 .wls，通常需管理员）
    rc, out, err = run(["netsh", "advfirewall", "export", str(po / "firewall.wls")], timeout=60)
    if (po / "firewall.wls").exists():
        _log(f"[已备份] 防火墙规则 firewall.wls ({human((po/'firewall.wls').stat().st_size)})")
    else:
        _log("[需管理员] 防火墙导出失败，提权后手动: netsh advfirewall export firewall.wls")

    # 计划任务清单（XML；不含任务密码）
    rc, out, err = run(["schtasks", "/query", "/fo", "XML", "/v"], timeout=90, dec="utf-8")
    if rc == 0 and out:
        (po / "scheduled_tasks.xml").write_text(out, encoding="utf-8")
        _log(f"[已记录] 计划任务清单 {out.count('<Task ')} 条（XML 不含运行密码）")

    # 第三方驱动：默认不导（本机实测 pnputil 会拉出数 GB，且新机多能自动/重装）
    # 需要离线驱动时才 with_drivers=True，产物落 policy/drivers（供 pnputil /add-driver 重装）。
    drv = po / "drivers"
    if with_drivers:
        drv.mkdir(parents=True, exist_ok=True)
        rc, out, err = run(["pnputil", "/export-driver", "*", str(drv)], timeout=900)
        n = len(list(drv.rglob("*.inf")))
        if n:
            _log(f"[已备份] 第三方驱动 {n} 个包，共 {human(dir_size(drv))} → policy/drivers")
        else:
            _log(f"[提示] 驱动导出为空/需管理员: {(err or out).strip()[:100]}")
    else:
        _log("[跳过] 第三方驱动（可达数 GB，默认不导）；需离线驱动时加 --drivers，"
             "或手动: pnputil /export-driver * drivers")

    # 打印机（printbrm 是按需功能，多数机器没有 → 降级为清单+提示）
    rc, out, err = run(["powershell", "-NoProfile", "-Command",
                        "Get-Printer | Select-Object Name,Driver,Port|ConvertTo-Json"], timeout=60)
    (po / "printers.json").write_text(out or "[]", encoding="utf-8")
    rc2, _, _ = run(["printbrm", "-?"], timeout=20)
    if shutil.which("printbrm"):
        run(["printbrm", "-b", "-o", "-f", str(po / "printer_migration.bin")], timeout=120)
        _log("[已备份] 打印机迁移包 printer_migration.bin")
    else:
        _log("[提示] 本机无 printbrm(按需功能)；打印机已存清单 printers.json，驱动需新机重装")

    # VPN 拨号 pbk（含用户名，密码 DPAPI 绑机）
    pbk = Path(os.environ.get("APPDATA", _home())) / "Microsoft" / "Network" / "Connections" / "PBK"
    if pbk.exists():
        ok, _ = safe_copy(pbk, po / "vpn_pbk")
        _log(f"[{'已备份' if ok else '失败'}] VPN 拨号条目 pbk（密码 DPAPI 绑机，新机需重录）")


# ---------- 组 4：开发工具配置（只拷配置层，避巨缓存） ----------

# VSCode 系（含 Cursor/Trae）User 目录里 workspaceStorage/globalStorage 动辄上 GB 且
# 机器绑定，绝不能整包拷 —— 只取可迁移的配置白名单。
_IDE_USER_DIRS = {
    "VSCode": Path(os.environ.get("APPDATA", _home())) / "Code" / "User",
    "Cursor": Path(os.environ.get("APPDATA", _home())) / "Cursor" / "User",
    "Trae": Path(os.environ.get("APPDATA", _home())) / "Trae CN" / "User",
}
_IDE_KEEP = ("settings.json", "keybindings.json", "snippets", "profiles")


def _copy_config_subset(base: Path, dst: Path) -> list[str]:
    """从 IDE 的 User 目录里只拷 _IDE_KEEP 白名单项，返回 '名称(大小)' 列表。"""
    got: list[str] = []
    for name in _IDE_KEEP:
        src = base / name
        if not src.exists():
            continue
        ok, msg = safe_copy(src, dst / name)
        if ok:
            got.append(f"{name}({human(dir_size(dst / name))})")
        else:
            _log(f"[失败] {base.name}/{name}: {msg}")
    return got


def backup_devtools(backup_dir: Path) -> None:
    print("=" * 50)
    print("开发工具配置（只拷配置层，不拷巨型缓存/全局存储）")
    print("=" * 50)
    dv = backup_dir / "devtools"
    dv.mkdir(parents=True, exist_ok=True)

    for ide, base in _IDE_USER_DIRS.items():
        if not base.exists():
            _log(f"[跳过] {ide}: 未找到 User 配置目录 {base}")
            continue
        got = _copy_config_subset(base, dv / ide / "config")
        _log(f"[已备份] {ide} 配置: {', '.join(got) or '(白名单项均不存在)'}")

    # JetBrains：多产品目录，只取 */options（配置），跳过 */plugins（大且机器绑定）
    jb = Path(os.environ.get("APPDATA", _home())) / "JetBrains"
    jb_ok = []
    if jb.exists():
        for prod in jb.iterdir():
            opt = prod / "options"
            if opt.is_dir():
                ok, _ = safe_copy(opt, dv / "JetBrains" / prod.name / "options")
                if ok:
                    jb_ok.append(prod.name)
    _log(f"[已备份] JetBrains options: {', '.join(jb_ok) or '(未命中)'}")

    # 各 IDE 扩展列表（命令行导出，失败留空）
    for exe, name in (("code", "VSCode"), ("cursor", "Cursor"), ("trae", "Trae")):
        rc, out, err = run([exe, "--list-extensions"], timeout=60)
        if rc == 0 and out.strip():
            (dv / f"{name}_extensions.txt").write_text(out, encoding="utf-8")
            _log(f"[已记录] {name} 扩展 {len(out.splitlines())} 个")

    # 改键：Scancode Map（HKLM） + AutoHotkey 脚本目录
    sm = reg_read(winreg.HKEY_LOCAL_MACHINE,
                  r"SYSTEM\CurrentControlSet\Control\Keyboard Layout", "Scancode Map")
    (dv / "scancode_map_note.txt").write_text(
        "Scancode Map " + ("存在(二进制，新机需管理员 reg add 回写)\n" if sm else "未设置\n") +
        "AutoHotkey: 手动拷你的 .ahk 脚本目录并装 AutoHotkey\n", encoding="utf-8")

    # PowerToys 配置
    pt = Path(os.environ.get("LOCALAPPDATA", _home())) / "Microsoft" / "PowerToys"
    if pt.exists():
        ok, _ = safe_copy(pt, dv / "powertoys")
        _log(f"[{'已备份' if ok else '失败'}] PowerToys 配置")

    # WSL 发行版（仅安全记录名字；wsl 命令在本机可能未装/损坏并回吐报错文本，需甄别）
    rc, out, err = run(["wsl", "-l", "-q"], timeout=40)
    if rc != 0:
        _log("[提示] WSL 不可用/未安装（wsl 命令返回错误），跳过发行版导出")
        names = []
    else:
        # 只接受像发行名的行，过滤掉 wsl 可能混进 stdout 的报错噪声
        names = [ln.strip() for ln in out.splitlines() if ln.strip()
                 and "WSL" not in ln and "rror" not in ln and "annot" not in ln]
    if names:
        (dv / "wsl_distro.txt").write_text("\n".join(names), encoding="utf-8")
        _log(f"[已记录] WSL 发行版: {', '.join(names)}（整盘导出较大，默认仅记名，"
             f"要镜像请手动 wsl --export <名> <tar>）")
    elif rc == 0:
        _log("[提示] 未检测到 WSL 发行版")

    # shell dotfiles
    for rel in (".bashrc", ".bash_profile", ".profile", ".inputrc", ".condarc"):
        p = _home() / rel
        if p.exists():
            safe_copy(p, dv / "shell" / rel)
    _log("[已备份] shell dotfiles（存在者）")


GROUPS = {
    "credentials": backup_credentials,
    "appearance": backup_appearance,
    "policy": backup_policy,
    "devtools": backup_devtools,
}

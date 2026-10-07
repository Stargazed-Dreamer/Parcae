"""系统环境备份工具

备份浏览器数据、游戏存档、桌面布局、系统设置、软件列表等。
生成一键恢复脚本和手动确认清单。

用法：
  uv run python workspace/disk_manager/scripts/backup_env.py              # 全量备份
  uv run python workspace/disk_manager/scripts/backup_env.py --browser     # 只备份浏览器
  uv run python workspace/disk_manager/scripts/backup_env.py --games       # 只备份游戏存档
  uv run python workspace/disk_manager/scripts/backup_env.py --system      # 只备份系统设置
  uv run python workspace/disk_manager/scripts/backup_env.py --software    # 只备份软件列表
  uv run python workspace/disk_manager/scripts/backup_env.py --credentials  # 凭据/WiFi/SSH/浏览器隐私(明文!)
  uv run python workspace/disk_manager/scripts/backup_env.py --appearance   # 壁纸/主题/字体/输入法词库
  uv run python workspace/disk_manager/scripts/backup_env.py --policy        # 防火墙/计划任务/打印机/VPN
  uv run python workspace/disk_manager/scripts/backup_env.py --drivers        # 上述系统策略 + 第三方驱动(数GB)
  uv run python workspace/disk_manager/scripts/backup_env.py --devtools       # IDE/WSL/改键/shell 配置
"""

import os
import sys
import json
import shutil
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup_groups as BG  # noqa: E402  迁移时易漏的凭据/外观/策略/开发配置四组导出


BACKUP_BASE = Path(r"<data_drive>:/<backup_root>\SystemBackup")
TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
BACKUP_DIR = BACKUP_BASE / TIMESTAMP


def fmt_size(size: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def safe_copy(src: Path, dst: Path, label: str = ""):
    """安全复制文件/目录"""
    if not src.exists():
        print(f"  [跳过] {label or src.name} 不存在")
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file()) if dst.is_dir() else dst.stat().st_size
        print(f"  [已备份] {label or src.name} → {dst.relative_to(BACKUP_DIR)} ({fmt_size(size)})")
        return True
    except Exception as e:
        print(f"  [失败] {label or src.name}: {e}")
        return False


def reg_export(key: str, dst: Path, label: str = ""):
    """导出注册表项"""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["reg", "export", key, str(dst), "/y"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            print(f"  [已导出] {label or key}")
            return True
        else:
            print(f"  [失败] {label or key}: {result.stderr.strip()}")
            return False
    except Exception as e:
        print(f"  [失败] {label or key}: {e}")
        return False


def run_cmd(cmd: str, label: str = "") -> str:
    """运行命令并返回输出"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30,
        )
        output = result.stdout.strip()
        if output:
            print(f"  [已获取] {label}")
        else:
            print(f"  [空] {label}")
        return output
    except Exception as e:
        print(f"  [失败] {label}: {e}")
        return ""


# ========== 浏览器备份 ==========

def backup_browser():
    """备份Chrome浏览器数据"""
    print("\n" + "="*50)
    print("备份浏览器数据")
    print("="*50)

    dst = BACKUP_DIR / "browser"
    chrome_base = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data" / "Default"

    if not chrome_base.exists():
        print("  Chrome默认配置目录不存在，跳过")
        return

    # 书签
    safe_copy(chrome_base / "Bookmarks", dst / "Bookmarks", "书签")
    safe_copy(chrome_base / "Bookmarks.bak", dst / "Bookmarks.bak", "书签备份")

    # 偏好设置
    safe_copy(chrome_base / "Preferences", dst / "Preferences", "偏好设置")

    # 搜索引擎
    # 从Preferences中提取
    prefs = chrome_base / "Preferences"
    if prefs.exists():
        try:
            data = json.loads(prefs.read_text(encoding="utf-8"))
            search_engines = data.get("default_search_provider_data", {})
            with open(dst / "search_engines.json", "w", encoding="utf-8") as f:
                json.dump(search_engines, f, ensure_ascii=False, indent=2)
            print("  [已提取] 搜索引擎设置")
        except Exception:
            pass

    # 扩展列表
    extensions_dir = chrome_base.parent / "Extensions"
    if extensions_dir.exists():
        ext_list = []
        for ext_id in extensions_dir.iterdir():
            if ext_id.is_dir():
                # 读取manifest获取扩展名
                versions = sorted(ext_id.iterdir(), reverse=True)
                for ver_dir in versions:
                    manifest = ver_dir / "manifest.json"
                    if manifest.exists():
                        try:
                            m = json.loads(manifest.read_text(encoding="utf-8"))
                            ext_list.append({
                                "id": ext_id.name,
                                "name": m.get("name", "未知"),
                                "version": m.get("version", ""),
                            })
                        except Exception:
                            ext_list.append({"id": ext_id.name, "name": "未知", "version": ""})
                        break
        with open(dst / "extensions_list.json", "w", encoding="utf-8") as f:
            json.dump(ext_list, f, ensure_ascii=False, indent=2)
        print(f"  [已提取] 扩展列表 ({len(ext_list)}个)")

    # 油猴脚本
    for ext_dir in (chrome_base.parent / "Extensions").iterdir():
        if "tampermonkey" in ext_dir.name.lower() or "violentmonkey" in ext_dir.name.lower():
            safe_copy(ext_dir, dst / f"extension_{ext_dir.name}", "油猴扩展数据")

    print("  ⚠️ Chrome密码无法自动导出，请手动导出: chrome://password-manager/settings")


# ========== 游戏存档备份 ==========

def backup_game_saves():
    """备份游戏存档"""
    print("\n" + "="*50)
    print("备份游戏存档")
    print("="*50)

    dst = BACKUP_DIR / "game_saves"
    backed_up = 0

    # Steam存档
    steam_userdata = Path(r"C:\Program Files (x86)\Steam\userdata")
    if steam_userdata.exists():
        safe_copy(steam_userdata, dst / "Steam_userdata", "Steam用户数据")
        backed_up += 1

    # Documents/My Games
    my_games = Path(os.environ.get("USERPROFILE", "")) / "Documents" / "My Games"
    if my_games.exists():
        safe_copy(my_games, dst / "My_Games", "My Games")
        backed_up += 1

    # AppData常见游戏存档路径
    appdata_local = Path(os.environ.get("LOCALAPPDATA", ""))
    appdata_roaming = Path(os.environ.get("APPDATA", ""))

    game_patterns = [
        ("Epic Games", appdata_local / "Epic Games"),
        ("GOG Galaxy", appdata_local / "GOG.com"),
        ("NVIDIA", appdata_local / "NVIDIA"),
    ]

    for name, path in game_patterns:
        if path.exists():
            safe_copy(path, dst / name, name)
            backed_up += 1

    if backed_up == 0:
        print("  未找到常见游戏存档路径")
    print("  ⚠️ 部分游戏存档在游戏安装目录下，需手动确认")


# ========== 系统设置备份 ==========

def backup_file_associations(dst_dir: Path) -> None:
    """备份每用户文件关联：reg export 原始 hive（免管理员）+ 可读清单 inventory.json。

    与独立三件套 assoc_snapshot/assoc_diff/assoc_restore 共用解析逻辑；恢复文件关联的
    可靠做法（哈希无关：set classic + del UserChoice）见同目录 assoc_restore.py，
    ⚠️ 不要指望 reg import 旧 UserChoice —— Hash 会校验失败被忽略。
    """
    print("\n--- 文件关联 ---")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import assoc_common as C  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] 无法加载 assoc_common: {e}")
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    raw = dst_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    reg_export("HKCU\\" + C.FILEEXTS_SUBKEY, raw / "FileExts.reg", "FileExts(UserChoice)")
    reg_export("HKCU\\Software\\Classes", raw / "HKCU_Classes.reg", "HKCU Classes(经典ProgID)")
    try:
        inv = C.build_inventory()
        ts = TIMESTAMP
        C.ASSOC_ROOT.mkdir(parents=True, exist_ok=True)
        # 同时写一份到本次全量备份目录，便于随整机备份归档
        (dst_dir / "inventory.json").write_text(
            json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        C.save_inventory("fullbackup_" + ts, inv)
        print(f"  [已备份] 文件关联 {inv['count']} 个后缀 → {dst_dir.relative_to(BACKUP_DIR)}")
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] 采集关联清单: {e}")


def backup_settings_watch() -> None:
    """设置看门狗快照：调用统一引擎把所有纳管项（文件关联/PATH/浏览器/协议/自启）采集
    成一份快照并设为新基线，落在 BACKUP_BASE\\settings_watch\\<ts>（独立于本次全量目录）。

    与手动 `settings_watch/watch.py snapshot --set-baseline` 等价；diff/restore 由该引擎消费。
    """
    print("\n--- 设置看门狗（PATH/浏览器/协议/自启 等纳管项）---")
    watch_py = Path(__file__).resolve().parent.parent / "settings_watch" / "watch.py"
    if not watch_py.exists():
        print(f"  [失败] 找不到 {watch_py}")
        return
    try:
        r = subprocess.run(
            [sys.executable, str(watch_py), "snapshot", "--set-baseline"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        for line in (r.stdout or "").splitlines():
            print("  " + line)
        if r.returncode != 0:
            print(f"  [!] 引擎返回码 {r.returncode}: {(r.stderr or '').strip()[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] 设置看门狗快照: {e}")


def backup_system():
    """备份系统设置"""
    print("\n" + "="*50)
    print("备份系统设置")
    print("="*50)

    dst = BACKUP_DIR / "system"
    dst.mkdir(parents=True, exist_ok=True)

    # 注册表导出
    reg_items = [
        ("HKCU\\Environment", dst / "registry" / "env_user.reg", "用户环境变量"),
        ("HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment", dst / "registry" / "env_system.reg", "系统环境变量"),
        ("HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Taskband", dst / "registry" / "taskband.reg", "任务栏"),
        ("HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", dst / "registry" / "explorer_advanced.reg", "资源管理器设置"),
    ]

    for key, path, label in reg_items:
        reg_export(key, path, label)

    # 桌面图标位置
    reg_export(
        "HKCU\\Software\\Microsoft\\Windows\\Shell\\Bags",
        dst / "registry" / "desktop_layout.reg",
        "桌面布局",
    )

    # 文件关联（每用户默认程序）—— 无损原件 + 可读清单
    backup_file_associations(dst / "file_associations")

    # 设置看门狗：PATH/浏览器/协议/自启 等纳管项统一快照 + 设基线
    backup_settings_watch()

    # 电源计划
    run_cmd(f"powercfg /export \"{dst / 'power_plan.pow'}\"", "电源计划")

    # hosts文件
    hosts = Path(r"C:\Windows\System32\drivers\etc\hosts")
    if hosts.exists():
        try:
            shutil.copy2(hosts, dst / "hosts")
            print("  [已备份] hosts文件")
        except PermissionError:
            print("  [跳过] hosts文件需要管理员权限")

    # Windows Terminal配置
    wt_packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
    if wt_packages.exists():
        for pkg in wt_packages.iterdir():
            if "WindowsTerminal" in pkg.name:
                settings = pkg / "LocalState" / "settings.json"
                if settings.exists():
                    safe_copy(settings, dst / "terminal_settings.json", "Windows Terminal配置")

    # 开始菜单布局
    layout_path = dst / "start_layout.xml"
    run_cmd(
        f'powershell -Command "Export-StartLayout -Path \'{layout_path}\'"',
        "开始菜单布局",
    )


# ========== 软件列表备份 ==========

def backup_software():
    """备份已安装软件列表"""
    print("\n" + "="*50)
    print("备份软件列表")
    print("="*50)

    dst = BACKUP_DIR / "software"
    dst.mkdir(parents=True, exist_ok=True)

    # 已安装程序
    output = run_cmd(
        'powershell -Command "Get-ItemProperty HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* | '
        'Where-Object { $_.DisplayName } | Select-Object DisplayName, DisplayVersion, Publisher | '
        'ConvertTo-Json"',
        "已安装程序列表",
    )
    if output:
        (dst / "installed_programs.json").write_text(output, encoding="utf-8")

    # Chocolatey
    run_cmd("choco list --local-only", "Chocolatey包")
    output = run_cmd("choco list --local-only --limit-output", "Chocolatey包(简洁)")
    if output:
        (dst / "choco_list.txt").write_text(output, encoding="utf-8")

    # Scoop
    output = run_cmd("scoop list", "Scoop包")
    if output:
        (dst / "scoop_list.txt").write_text(output, encoding="utf-8")

    # pip
    output = run_cmd("pip list --format=freeze", "pip包")
    if output:
        (dst / "pip_requirements.txt").write_text(output, encoding="utf-8")

    # uv tools
    output = run_cmd("uv tool list", "uv工具")
    if output:
        (dst / "uv_tools.txt").write_text(output, encoding="utf-8")

    # npm全局
    output = run_cmd("npm list -g --depth=0", "npm全局包")
    if output:
        (dst / "npm_global.txt").write_text(output, encoding="utf-8")


# ========== 桌面备份 ==========

def backup_desktop():
    """备份桌面快捷方式和文件列表"""
    print("\n" + "="*50)
    print("备份桌面")
    print("="*50)

    dst = BACKUP_DIR / "desktop"
    desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"

    if not desktop.exists():
        print("  桌面路径不存在")
        return

    # 记录桌面文件列表（不复制文件本身，filesync会处理）
    items = []
    for item in desktop.iterdir():
        items.append({
            "name": item.name,
            "type": "dir" if item.is_dir() else "file",
            "suffix": item.suffix if item.is_file() else "",
        })

    (dst / "desktop_items.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  [已记录] 桌面文件列表 ({len(items)}项)")

    # 复制快捷方式（.lnk文件）
    lnk_count = 0
    for lnk in desktop.glob("*.lnk"):
        try:
            shutil.copy2(lnk, dst / lnk.name)
            lnk_count += 1
        except Exception:
            pass
    if lnk_count:
        print(f"  [已备份] 快捷方式 ({lnk_count}个)")


# ========== 生成恢复脚本 ==========

def generate_restore_script():
    """生成一键恢复脚本"""
    print("\n" + "="*50)
    print("生成恢复脚本")
    print("="*50)

    dst = BACKUP_DIR / "restore_env.bat"

    script = f"""@echo off
chcp 65001 >nul
echo ============================================
echo   系统环境恢复脚本
echo   备份时间: {TIMESTAMP}
echo ============================================
echo.
echo ⚠️ 此脚本将恢复注册表和系统设置！
echo ⚠️ 请以管理员身份运行！
echo.
pause

echo.
echo [1/6] 恢复注册表...
"""

    # 注册表恢复
    reg_dir = BACKUP_DIR / "system" / "registry"
    if reg_dir.exists():
        for reg_file in reg_dir.glob("*.reg"):
            script += f'echo 导入 {reg_file.name}...\n'
            script += f'reg import "{reg_file}"\n'

    script += f"""
echo.
echo [2/6] 恢复hosts文件...
set /p restore_hosts="是否恢复hosts文件? (y/n): "
if /i "%restore_hosts%"=="y" (
    copy /y "{BACKUP_DIR / "system" / "hosts"}" "C:\\Windows\\System32\\drivers\\etc\\hosts"
    echo hosts已恢复
)

echo.
echo [3/6] 恢复Windows Terminal配置...
echo 请手动将 terminal_settings.json 复制到:
echo   %%LOCALAPPDATA%%\\Packages\\Microsoft.WindowsTerminal_8wekyb3d8bbwe\\LocalState\\settings.json

echo.
echo [4/6] 恢复浏览器书签...
echo 请手动将 Bookmarks 文件复制到:
echo   %%LOCALAPPDATA%%\\Google\\Chrome\\User Data\\Default\\Bookmarks

echo.
echo [5/6] 恢复游戏存档...
echo 请确认游戏已安装后，将 game_saves 目录下的存档复制到对应位置

echo.
echo [6/6] 安装软件...
echo 请参考 software/ 目录下的列表手动安装

echo.
echo ============================================
echo   恢复完成！
echo   详细清单请查看 manual_checklist.md
echo ============================================
pause
"""

    dst.write_text(script, encoding="utf-8")
    print(f"  [已生成] {dst}")


def generate_checklist():
    """生成手动确认清单"""
    print("\n" + "="*50)
    print("生成手动确认清单")
    print("="*50)

    dst = BACKUP_DIR / "manual_checklist.md"

    content = f"""# 系统环境恢复 - 手动确认清单

备份时间: {TIMESTAMP}

## 需手动恢复的项目

### 浏览器
- [ ] Chrome密码: chrome://password-manager/settings → 导出 → 在新电脑导入
- [ ] Chrome扩展: 参考 browser/extensions_list.json 逐个安装
- [ ] 油猴脚本: 检查是否需要重新安装

### 游戏存档
- [ ] 确认游戏已安装
- [ ] 复制 game_saves/ 下对应目录到存档位置
- [ ] Steam云存档会自动同步，本地存档需手动复制

### 系统设置
- [ ] Windows激活
- [ ] 显示缩放比例
- [ ] 深色/浅色模式
- [ ] 输入法配置
- [ ] 默认浏览器
- [ ] 默认文件关联：用 assoc_diff.py 看被改了什么，assoc_restore.py --apply 分档恢复
      （经典关联自动；商店/UWP 类走设置页手动重选，勿指望 reg import UserChoice）
- [ ] 任务栏位置和固定项

### 软件
- [ ] 参考 software/installed_programs.json 安装必要软件
- [ ] pip: pip install -r software/pip_requirements.txt
- [ ] Chocolatey: 参考 software/choco_list.txt
- [ ] Scoop: 参考 software/scoop_list.txt

### 其他
- [ ] VPN/代理设置
- [ ] Git配置: git config --global user.name/email
- [ ] SSH密钥: ~/.ssh/（已备 credentials/dotfiles/.ssh）
- [ ] 自定义字体
- [ ] 右键菜单增强工具

## 迁移专用恢复（本轮新增导出组，多数只能在原机导出、新机上按其方式还原）

### 凭据 credentials/
- [ ] WiFi: 新机 `netsh wlan add profile filename="credentials\\wifi\\*.xml"`（XML 含明文密码）
- [ ] 浏览器隐私: 用 HackBrowserData 的 `restore` + browser/chrome/*-masterkeys.json 跨机解密；
      或直接把 browser/chrome/readable/*.csv 当清单手动重录
- [ ] SSH/AWS/npm/git 凭据: 从 credentials/dotfiles/ 拷回对应 ~ 路径
- [ ] 凭据管理器: 见 credentials/credential_manager_README.txt（无法批量导，手动核对）

### 外观 appearance/
- [ ] 壁纸: 拷 appearance/wallpaper/ 图到新机后在设置里指定（wallpaper.json 记了原路径/样式）
- [ ] 主题: 深浅色/强调色见 theme.json，themes.reg 可导入
- [ ] 字体: 拷 appearance/user_fonts/ 到新机（系统自带字体无需管）
- [ ] 输入法词库: 拷 appearance/ime/ 回对应目录，或走输入法云同步重登

### 系统策略 policy/
- [ ] 防火墙: 提权后 `netsh advfirewall import policy\\firewall.wls`（导出当时可能需管理员）
- [ ] 计划任务: 见 scheduled_tasks.xml（不含密码，需手动重建带密码任务）
- [ ] 打印机: 见 printers.json，驱动新机重装
- [ ] VPN: 拷 policy/vpn_pbk/ 到 %APPDATA%\\Microsoft\\Network\\Connections\\PBK

### 开发工具 devtools/（均为"配置"，软件本体需先装）
- [ ] 装好 IDE 后，把 devtools/<IDE>/config/ 覆盖回其 User 目录
- [ ] 按 *_extensions.txt 重装扩展；JetBrains 还原 options/
- [ ] WSL 发行版/ AutoHotkey / Scancode 改键按各 *_note 手动处理
"""

    dst.write_text(content, encoding="utf-8")
    print(f"  [已生成] {dst}")


# ========== 主入口 ==========

def main():
    parser = argparse.ArgumentParser(description="系统环境备份工具")
    parser.add_argument("--browser", action="store_true", help="只备份浏览器")
    parser.add_argument("--games", action="store_true", help="只备份游戏存档")
    parser.add_argument("--system", action="store_true", help="只备份系统设置")
    parser.add_argument("--software", action="store_true", help="只备份软件列表")
    parser.add_argument("--desktop", action="store_true", help="只备份桌面")
    parser.add_argument("--assoc", action="store_true", help="只备份文件关联（每用户默认程序）")
    parser.add_argument("--settings", action="store_true", help="只跑设置看门狗快照（PATH/浏览器/协议/自启 等纳管项）")
    parser.add_argument("--credentials", action="store_true", help="只备份凭据/密钥(明文): WiFi/SSH/浏览器隐私")
    parser.add_argument("--appearance", action="store_true", help="只备份壁纸/主题/字体/输入法词库")
    parser.add_argument("--policy", action="store_true", help="只备份防火墙/计划任务/打印机/VPN（不含大驱动）")
    parser.add_argument("--drivers", action="store_true", help="系统策略 + 第三方驱动导出（可达数 GB）")
    parser.add_argument("--devtools", action="store_true", help="只备份 IDE/WSL/改键/shell 配置")
    args = parser.parse_args()

    specific = (args.browser or args.games or args.system or args.software
                or args.desktop or args.assoc or args.settings
                or args.credentials or args.appearance or args.policy
                or args.drivers or args.devtools)
    if not specific:
        print("=" * 60)
        print("⚠️  全面备份功能未经测试！执行过程中可能遇到问题。")
        print("    请全程跟进并记录遇到的任何错误。")
        print("    注意：全量含凭据组，会以【明文】导出 WiFi 密码/浏览器隐私/token 到备份目录，")
        print("    filesync 会把它同步到移动硬盘 —— 如不愿明文入盘，请改用分模块备份。")
        print("=" * 60)

    print(f"备份目标: {BACKUP_DIR}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    if args.browser or not specific:
        backup_browser()
    if args.games or not specific:
        backup_game_saves()
    if args.system or not specific:
        backup_system()
    if args.software or not specific:
        backup_software()
    if args.desktop or not specific:
        backup_desktop()
    if args.assoc and not args.system:
        # --system 或全量时 backup_system() 已内含文件关联，避免重复
        backup_file_associations(BACKUP_DIR / "system" / "file_associations")
    if args.settings and not args.system:
        # --system/全量时 backup_system() 已内含看门狗快照，避免重复
        backup_settings_watch()

    # 迁移易漏项四组（全量默认包含；单独 flag 只跑对应组）
    if args.credentials or not specific:
        BG.backup_credentials(BACKUP_DIR)
    if args.appearance or not specific:
        BG.backup_appearance(BACKUP_DIR)
    if args.policy or args.drivers or not specific:
        # 驱动只有显式 --drivers 才导（数 GB）；全量/ --policy 均不含大驱动
        BG.backup_policy(BACKUP_DIR, with_drivers=args.drivers)
    if args.devtools or not specific:
        BG.backup_devtools(BACKUP_DIR)

    if not specific:
        generate_restore_script()
        generate_checklist()

    # 统计备份大小
    total = sum(f.stat().st_size for f in BACKUP_DIR.rglob("*") if f.is_file())
    print(f"\n{'='*50}")
    print(f"备份完成！总大小: {fmt_size(total)}")
    print(f"备份位置: {BACKUP_DIR}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()

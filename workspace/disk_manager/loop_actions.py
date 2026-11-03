"""disk_manager 组件 — 可选 agent_guide 条目。

本模块由主代码库的 _load_optional_guide_entries() 动态发现并加载。
主代码库不直接 import 本模块，删除 workspace/disk_manager/ 目录后主代码库仍正常工作。

导出接口：
    - GUIDE_REGISTRY_ENTRIES: dict, agent guide 条目（task_type → 决策摘要）
    - LOOP_TASK_DEFS:        dict, loop 任务定义（disk_manager.ssd_wear 写入/强度采样）
    - register(manager):     注册 SsdWearSnapshotAction 到 LoopManager
"""

import asyncio
import json
import os
import subprocess
import sys

from server.activity_tracker.loop_manager import Action, IntervalTrigger

_COMPONENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_COMPONENT_DIR))
_SSD_SCRIPT = os.path.join(_PROJECT_ROOT, "tools", "disk_io_audit", "ssd_wear_snapshot.py")


class SsdWearSnapshotAction(Action):
    """按窗口采一次 NVMe 累计写入 + harness 强度指纹，追加一行 CSV。

    为什么挂在 loop 上可以容忍后端重启：SMART 的 Data Units Written 是**盘级累计**，
    窗口多宽都能正确相减，所以后面的断档只会让窗口变宽，不会丢数据。
    脚本通过子进程跑：采样器崩了也带不动后端；psutil 遍历 + SMART IOCTL 都放线程里。
    """

    action_type = "ssd_wear_snapshot"

    async def execute(self, context: dict) -> dict:
        cfg = context.get("config") or {}
        if not os.path.exists(_SSD_SCRIPT):
            return {"success": False, "error": f"采样脚本不存在: {_SSD_SCRIPT}"}

        cmd = [sys.executable, _SSD_SCRIPT, "--json",
               "--skip-if-recent", str(int(cfg.get("skip_if_recent_secs", 600)))]
        csv_path = str(cfg.get("csv_path") or "").strip()
        if csv_path:
            cmd += ["--csv", csv_path]

        try:
            proc = await asyncio.to_thread(
                subprocess.run, cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180,
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "采样超时 180s（SMART IOCTL 可能卡死）"}

        lines = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.startswith("{")]
        if not lines:
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-300:]
            return {"success": False,
                    "error": f"采样无输出 (exit={proc.returncode}): {tail or 'empty'}"}
        try:
            data = json.loads(lines[-1])
        except ValueError:
            return {"success": False, "error": f"采样输出不是合法 JSON: {lines[-1][:200]}"}

        if data.get("skipped"):
            return {"success": True, "skipped": True,
                    "reason": f"距上次采集 {data.get('gap_secs')}s，跳过"}

        disks = data.get("disks") or {}
        d0 = disks.get("0") or {}
        dw = d0.get("dw_bytes")
        return {
            "success": bool(data.get("ok")),
            "win_secs": data.get("win_secs"),
            "cores": data.get("cores"),
            "exited_n": data.get("exited_n"),
            "disk0_dw_gb": round(dw / 1e9, 2) if isinstance(dw, (int, float)) else None,
            "disk0_gb_per_h": d0.get("gb_per_h"),
            "harness": {k: {"cpu_s": v.get("cpu_s"), "log_wr_mb": v.get("log_wr_mb"),
                            "log_n": v.get("log_n")}
                        for k, v in (data.get("harness") or {}).items()},
            "error": None if data.get("ok") else "读不到任何 NVMe SMART",
        }


LOOP_TASK_DEFS: dict = {
    "disk_manager": [
        {
            "sub_id": "ssd_wear",
            "action": "ssd_wear_snapshot",
            "trigger": lambda c, tz: IntervalTrigger(
                c.get("ssd_wear_interval", 1800),
                first_run_delay=c.get("ssd_wear_first_run_delay", 300),
            ),
        },
    ],
}


def register(manager) -> None:
    """注册 SSD 写入/强度采样 action 到 LoopManager。"""
    manager.register_action("ssd_wear_snapshot", SsdWearSnapshotAction())


GUIDE_REGISTRY_ENTRIES: dict = {
    "adhoc.disk_cleanup": {
        "skill": "disk_manager",
        "name": "磁盘管理",
        "skill_file": "workspace/disk_manager/SKILL.md",
        # 移除单独"磁盘"（太泛，用户说"磁盘"可能指很多场景）
        # 已有"清理硬盘"/"磁盘清理"/"空间不足"覆盖磁盘管理场景
        "keywords": ["清理硬盘", "磁盘清理", "空间不足", "备份系统", "环境备份", "游戏存档备份", "SpaceSniffer", "sns快照", "文件关联", "默认打开程序", "默认应用被改", "后缀关联", "恢复关联", "环境变量被改", "PATH被篡改", "默认浏览器被改", "开机自启", "自启项", "URL协议", "设置看门狗", "Winlogon", "登录劫持", "开机shell", "Userinit", "登录后门", "系统迁移", "换机", "WiFi密码", "浏览器密码导出", "壁纸备份", "输入法词库", "防火墙规则导出", "计划任务导出", "驱动离线备份", "IDE配置迁移", "SSD写入", "写入量", "TBW", "固态寿命", "耐久", "高强度使用"],
        "description": "空间扫描+快照导入+清理建议+全面备份（含文件关联/设置看门狗，及迁移易漏四组：凭据/外观/策略/开发配置）",
        "memory_key": "disk_manager",
        "first_action": "读取 memory_get('disk_manager')；询问用户要清理还是备份。清理：有 SpaceSniffer .sns 快照则先 parse_sns.py 导入（等效全盘 scan、零磁盘 IO），否则走 scan→drill→find→dry-run→delete 闭环",
        "workflow_summary": "清理：0.有 .sns 快照先 parse_sns.py 导入（--cache 复用后续查询） 1.scan 紧凑tree 2.drill/find 下钻定位 3.dry-run 出回执 4.用户勾选 5.delete 走回收站 | 备份：L1文件+L2环境+L3恢复",
        "mcp_tools_priority": [
            "memory_get('disk_manager')",
            "exec_python (parse_sns.py parse <快照.sns> --json — 有 SpaceSniffer 快照时优先，等效全盘 scan)",
            "exec_python (scan_disk.py scan/tree/files/find/caches --json，快照导入后全部 --cache 复用)",
            "exec_python (cleanup.py --dry-run --json → --delete --items <ids> --json)",
        ],
        "key_pitfalls": [
            "铁律：所有删除/移动操作必须用户审核确认，绝不擅自操作",
            "清理前先 --dry-run 出逐项回执，用 AskUserQuestion 让用户勾选，再 --delete --items <ids>",
            "scan_disk.py 子命令：scan/tree/files/find/caches/dups/resume，旧 --overview/--deep/--large/drill 已移除",
            ".sns 快照导入失败（截断/栈不平衡报错）说明快照损坏，不要改用部分数据",
            "删除走 ctypes 回收站（可恢复），--delete 必须基于 dry-run 回执的 item id",
            "文件关联：snapshot/diff/restore 三件套；恢复勿用 reg import UserChoice（哈希校验失败被忽略），restore 走 set classic+del UserChoice 哈希无关两步法，商店/UWP 类引导 ms-settings 手动重选",
            "设置看门狗：settings_watch/watch.py snapshot|diff|restore 统一驱动 文件关联·PATH/环境变量·默认浏览器/URL协议·开机自启·Winlogon登录劫持(Shell/Userinit) 等条目，新增一类=items.py 里 @register 一个 provider；diff 有改动返回码1；自动档只碰 HKCU，系统级PATH/机器级协议/默认浏览器/机器级Winlogon 只报告或引导 ms-settings（同 UserChoice 哈希无关杠杆）",
            "迁移易漏四组(backup_groups.py，全量自动跑)：凭据组明文导出密码/token 须先确认；"
            "浏览器隐私用 HackBrowserData(archive+dumpkeys 才能跨机解密)；devtools 只拷配置白名单"
            "不拷 globalStorage/workspaceStorage；驱动默认不导(数GB)，--drivers 才导",
        ],
        "prerequisites": ["后端运行中"],
        "level": "full",
    },
}

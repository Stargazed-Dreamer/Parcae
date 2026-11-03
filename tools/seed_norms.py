"""DL1 红线种子：把候选 norms 写入 facts（**默认 dry-run**）。

用法：
    uv run python tools/seed_norms.py                 # 只读，打印将写入什么
    uv run python tools/seed_norms.py --apply         # 只补缺失的 key，不覆盖已有
    uv run python tools/seed_norms.py --apply --force # 覆盖已有 key（谨慎）

设计：workspace/local_butler/30_架构与设计/14_记忆上下文红线_DL1实施设计_20261027.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.memory.store import MemoryStore

PROJECT_ID = "parcae"

SEED_NORMS: list[dict] = [
    {
        "key": "norm_secret_management",
        "scope": "global",
        "title": "密钥管理",
        "value": (
            "密钥只能经 lib/secret 读取（LLM 密钥走 data/llm/keys.json，其他走 "
            "data/secret/secrets.toml）；禁止硬编码、写入 config.toml、在日志或错误信息中回显。"
        ),
        "evidence_refs": ["AGENTS.md#密钥统一管理"],
    },
    {
        "key": "norm_recoverable_delete",
        "scope": "global",
        "title": "可恢复删除",
        "value": (
            "删除用户文件一律走 tools/disk/recycle.py 送回收站；禁止用 rm/del 物理删除。"
            "删除前必须确认目标与影响面。"
        ),
        "evidence_refs": ["AGENTS.md#删除执行纪律"],
    },
    {
        "key": "norm_user_only_file",
        "scope": "global",
        "title": "用户专用文件",
        "value": (
            "USER_ONLY_PROJECT_OPERATIONS_AGENT_DO_NOT_READ_OR_EDIT.md 属用户专用："
            "agent 禁止读取、修改、删除，也不得绕过它指明的 .dcg.toml / .dcg/ / "
            "config.toml / workspace 用户数据。"
        ),
        "evidence_refs": ["AGENTS.md#用户专用文件"],
    },
    {
        "key": "norm_disk_cleanup",
        "scope": "global",
        "title": "磁盘清理",
        "value": (
            "任何磁盘清理先列完整清单（路径+大小）交用户审核，明确同意后才执行；"
            "agent 自己创建的临时脚本除外。"
        ),
        "evidence_refs": ["AGENTS.md#磁盘清理规则"],
    },
    {
        "key": "norm_remote_write",
        "scope": "global",
        "title": "远端写操作",
        "value": (
            "对远端仓库的写操作（push / PR / issue / 评论）必须单独授权；"
            "未经授权不得执行，也不得用 force push 绕过。"
        ),
        "evidence_refs": ["AGENTS.md#Executing actions with care"],
    },
    {
        "key": "norm_git_staging",
        "scope": f"project:{PROJECT_ID}",
        "title": "Git 暂存",
        "value": (
            "只用按文件名 git add；禁止 git add -A / git stash；git 命令加 --no-pager，"
            "避免分页挂起。并行会话的产物不 stage。"
        ),
        "evidence_refs": ["AGENTS.md#并行会话纪律"],
    },
    {
        "key": "norm_test_gate",
        "scope": f"project:{PROJECT_ID}",
        "title": "测试闸门",
        "value": (
            "server/ client/ lib/ 变更后必须跑 smoke 端点测试（≥500 即失败）与 pyright "
            "基线对比；workspace/ tools/ 脚本改动用显式路径跑 pyright。"
        ),
        "evidence_refs": ["AGENTS.md#提交前机械防线"],
    },
    {
        "key": "norm_bat_encoding",
        "scope": f"project:{PROJECT_ID}",
        "title": "bat 编码",
        "value": (
            "bat/cmd 必须 GBK + CRLF + 无 BOM；写完跑 "
            ".agents/skills/bat_writing/scripts/fix_bat_encoding.py <文件> --check。"
        ),
        "evidence_refs": ["AGENTS.md#API 常见陷阱"],
    },
    {
        "key": "norm_browser_profile",
        "scope": f"project:{PROJECT_ID}",
        "title": "调试浏览器",
        "value": (
            "chrome_debug 是用户真实数据：一个 profile 只许 Chrome 打开；"
            "动前先跑 tools/browser/profile_guard.py backup 并自检。"
        ),
        "evidence_refs": ["docs/browser-profile-safety.md"],
    },
]


def _preview(n: dict) -> dict:
    text = f"- {n['title']}：{n['value']}"
    return {
        "key": n["key"],
        "scope": n["scope"],
        "title": n["title"],
        "chars": len(text),
        "must_read": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="DL1 红线种子（默认 dry-run）")
    ap.add_argument(
        "--db",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "memory", "memory.db",
        ),
        help="SQLite 路径（默认 data/memory/memory.db）",
    )
    ap.add_argument("--apply", action="store_true", help="真正写入；默认只读")
    ap.add_argument("--force", action="store_true", help="配合 --apply：覆盖已有 key")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出清单")
    args = ap.parse_args()

    store = MemoryStore(args.db)
    store.initialize()
    try:
        before = store.conn.execute(
            "select count(*) from facts where kind='norm' and archived_at is null"
        ).fetchone()[0]
        actions: list[dict] = []
        for n in SEED_NORMS:
            row = store.conn.execute(
                "select key from facts where key = ?", (n["key"],)
            ).fetchone()
            action = "create"
            if row:
                action = "overwrite" if (args.apply and args.force) else "exists"
            item = _preview(n) | {"action": action}
            actions.append(item)
            if args.apply and action in ("create", "overwrite"):
                store.upsert_fact(
                    n["key"],
                    n["value"],
                    source="manual",
                    fact_type="project",
                    title=n["title"],
                    summary=n["value"][:120],
                    scope=n["scope"],
                    kind="norm",
                    origin="system",
                    sensitivity="public",
                    evidence_refs=n["evidence_refs"],
                    must_read=1,
                )

        after = store.conn.execute(
            "select count(*) from facts where kind='norm' and archived_at is null"
        ).fetchone()[0]
        result = {
            "db": os.path.abspath(args.db),
            "applied": bool(args.apply),
            "force": bool(args.force),
            "norms_before": before,
            "norms_after": after,
            "chars_total": sum(a["chars"] for a in actions),
            "items": actions,
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"db = {result['db']}")
            print(f"norms: {before} -> {after} | 计划写入 {len(actions)} 条 | "
                  f"合计 {result['chars_total']} 字符 | applied={result['applied']}")
            for a in actions:
                flag = {"create": "+", "overwrite": "~", "exists": "="}[a["action"]]
                print(f"  {flag} {a['key']:<28} {a['scope']:<24} {a['chars']:>4} 字")
            if not args.apply:
                print("\n[dry-run] 未写入。确认清单后加 --apply；已有 key 默认不覆盖。")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())

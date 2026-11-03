"""DL3 端到端 smoke：收尾提交 → 去重 → 落库 → 下一轮能读到。

**只跑临时库**，不碰 ``data/memory/memory.db``。
用法：``uv run python tools/memory_wrapup_smoke.py``

设计：workspace/local_butler/30_架构与设计/12_记忆写入漏斗_DL3实施设计_20261027.md
四条判据各打一行：① 链路真跑 ② 幂等 ③ 不冒充原话 ④ 拒收可见。
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.memory.store import MemoryStore
from server.memory.wrapup import list_pending_candidates, submit_wrapup


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="dl3_smoke_")
    db = os.path.join(tmpdir, "memory.db")
    store = MemoryStore(db)
    store.initialize()

    payload = {
        "run_id": "smoke-r1",
        "project_id": "Parcae",
        "quotes": [{"text": "删除之前一定要先 dry run，别边删边报"}],
        "candidates": [
            {
                "content": "删除前必须 dry run：先只打印路径与体积，等确认再动",
                "title": "删除前 dry run",
                "summary": "先打印清单，等确认",
                "kind": "norm",
                "scope": "global",
                "sensitivity": "private-eng",
                "must_read": 1,
                "evidence_refs": ["~/.agents/memory/MEMORY.md:12"],
            },
            {
                "content": "同盘回收站不释放磁盘空间，可用空间数字才是判据",
                "title": "回收站不释放同盘空间",
                "kind": "pitfall",
                "scope": "global",
                "sensitivity": "private-eng",
            },
            {
                # 故意坏输入：kind 不在枚举内 → 应被拒且留档
                "content": "这条应被拒",
                "kind": "made_up_kind",
            },
        ],
    }

    print("=" * 68)
    print("[1] 首次收尾提交")
    first = submit_wrapup(store, payload)
    print("    quotes :", first["quotes"])
    print("    cands  :", {k: first["candidates"][k] for k in ("total", "accepted", "merged", "duplicate", "rejected")})
    for r in first["results"]:
        print("      #%d %-9s %s" % (r["index"], r["status"], r.get("reason") or r.get("fact_key")))

    print("-" * 68)
    print("[2] 同一份 payload 再提交一次（幂等判据）")
    second = submit_wrapup(store, payload)
    print("    cands  :", {k: second["candidates"][k] for k in ("total", "accepted", "merged", "duplicate", "rejected")})

    print("-" * 68)
    print("[3] 落库核对")
    n_facts = store.conn.execute("select count(*) from facts").fetchone()[0]
    n_quotes = store.conn.execute("select count(*) from source_quotes").fetchone()[0]
    origins = store.conn.execute(
        "select origin, count(*) from facts group by origin").fetchall()
    print("    facts=%d  source_quotes=%d  origin=%s" % (n_facts, n_quotes, origins))

    print("-" * 68)
    print("[4] 下一轮（另一个 run）读上下文")
    hits = store.list_facts_for_context(scope="global")
    for h in hits:
        print("    [%s] must_read=%s  %s" % (h["kind"], h["must_read"], (h["summary"] or h["value"])[:40]))
    must = store.list_facts_for_context(scope="global", must_read_only=True)
    print("    必读条目数 =", len(must))

    print("-" * 68)
    print("[5] 被拒候选可见（DL5 待审候选）")
    for c in list_pending_candidates(store):
        print("    reason=%s  payload.kind=%s" % (c["reason"], (c["payload"] or {}).get("kind")))

    # ── 判据 ────────────────────────────────────────────────
    checks = [
        ("首次 accepted=2", first["candidates"]["accepted"] == 2),
        ("首次 rejected=1", first["candidates"]["rejected"] == 1),
        ("二次全 duplicate", second["candidates"]["duplicate"] == 2
         and second["candidates"]["accepted"] == 0),
        ("facts 行数不变（2）", n_facts == 2),
        ("原话只落一条", n_quotes == 1),
        ("有 origin=user_quote", any(o == "user_quote" for o, _ in origins)),
        ("必读条目 = 1", len(must) == 1),
        ("被拒候选可查", len(list_pending_candidates(store)) == 1),
    ]
    print("=" * 68)
    ok = True
    for name, passed in checks:
        print("  %s %s" % ("PASS" if passed else "FAIL", name))
        ok = ok and passed

    store.close()
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("=" * 68)
    print("结果：", "全部通过" if ok else "有 FAIL，需修")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

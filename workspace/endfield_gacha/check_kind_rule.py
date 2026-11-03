"""终末地 kind 判据回归检查：用已入库的 raw 离线验分类，不需要 token/浏览器。

跑法（仓库根）：uv run python workspace/endfield_gacha/check_kind_rule.py
数据来源：raw_20261030（draw + gift_intel_book 两种 kind）与 raw_20260615（无 kind 的老形状）。
"""
import copy
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from workspace.endfield_gacha import endfield_gacha as eg

RAW_NEW = REPO / "workspace/endfield_gacha/raw/raw_20261030_145214_251567.json"
RAW_LEGACY = REPO / "workspace/endfield_gacha/raw/raw_20260615_011105.json"

ok = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  <- {detail}" if detail and not cond else ""))
    ok = ok and cond


raw = json.load(open(RAW_NEW, encoding="utf-8"))
chars = eg.process_char_records(raw["char"])
weapons = eg.process_weapon_records(raw["weapon"])

gifts = [r for r in chars if r.get("kind") == "gift_intel_book"]
check("转换后 gift 条目保留 kind", len(gifts) == 1, f"{len(gifts)}")
check("转换后 gift 条目保留 nameText", bool(gifts) and gifts[0]["nameText"] == "寻访情报书", str(gifts))
check("gift 判为非抽卡", bool(gifts) and eg.classify_pull(gifts[0]) == (False, "gift_intel_book"))
draws = [r for r in chars if r.get("kind") == "draw"]
check("draw 判为抽卡", bool(draws) and all(eg.classify_pull(r)[0] for r in draws))
check("weapon draw 也判为抽卡", all(eg.classify_pull(r)[0] for r in weapons))
check("kind 总数对齐 raw", len(gifts) + len(draws) == len(chars), f"{len(gifts)}+{len(draws)} vs {len(chars)}")

legacy = eg.process_char_records(json.load(open(RAW_LEGACY, encoding="utf-8"))["char"])
check("老数据整批无 kind", all(not r.get("kind") for r in legacy), f"{len(legacy)}")
check("老数据按稀有度全部算抽卡", all(eg.classify_pull(r)[0] for r in legacy))

# 合成一条"带合法稀有度的未知 kind"——正是纯 rarity 判据接不住的那种
mystery = copy.deepcopy(draws[0])
mystery["kind"] = "mystery_box"
mystery["seqId"] = "999001"
check("未知 kind 判为非抽卡", eg.classify_pull(mystery) == (False, "mystery_box"))

s1 = eg.generate_summary(chars, weapons, {})
s2 = eg.generate_summary(chars + [mystery], weapons, {})
check("未知 kind 进了单列", s2["non_pull_count"] == s1["non_pull_count"] + 1, f"{s1['non_pull_count']} -> {s2['non_pull_count']}")
check("未知 kind 报进 non_pull_unknown_kinds", s2["non_pull_unknown_kinds"] == ["mystery_box"], str(s2["non_pull_unknown_kinds"]))
check("未知 kind 不涨总抽数", s2["pull_total"] == s1["pull_total"], f"{s1['pull_total']} -> {s2['pull_total']}")
check("带★6 的未知 kind 不混进六星名单", len(s2["six_star_names"]) == len(s1["six_star_names"]))
check("单列条目带得出名字", (s1["non_pull_entries"][0].get("name") or "") != "", str(s1["non_pull_entries"]))
check("总数恒等式 total = pull + non_pull", s1["total"] == s1["pull_total"] + s1["non_pull_count"])
check("稀有度只数抽卡（★? 不再出现在分布里）", "★?" not in s1["rarity_count"] and sum(s1["rarity_count"].values()) == s1["pull_total"])

# 反向腿：摘掉 kind 判据（退回纯 rarity），未知 kind 必须被误算成抽卡，否则等于测空气
real = eg.classify_pull
eg.classify_pull = lambda rec: (rec.get("rarity") in (4, 5, 6), "")
s3 = eg.generate_summary(chars + [mystery], weapons, {})
eg.classify_pull = real
check("摘掉 kind 判据后 mystery 被误算成抽卡（反向腿）",
      s3["pull_total"] == s1["pull_total"] + 1 and s3["non_pull_unknown_kinds"] == [],
      str(s3["non_pull_unknown_kinds"]))

eg.print_summary(s1)
print("\n===", "ALL PASS" if ok else "HAS FAIL", "===")
sys.exit(0 if ok else 1)

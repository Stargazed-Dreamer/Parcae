"""审核界面说明智能推荐（counterparty hints）回归测试。

背景：旧实现 _on_category_changed 用 clear()+addItems() 重建说明下拉框，
QComboBox 自动选中 index 0，而 config_descriptions 按 Unicode 排序使 ASCII
值（如 KFC）恒排最前 —— 用户看到"选大类=吃 → 自动填出 KFC"，与交易名称无关。
修复后：推荐项置顶 + 自动填 top-1，无命中则留空，绝不再落回第 0 项。
"""

from pathlib import Path

from workspace.accounting.panel_widgets import compute_desc_hints
from workspace.accounting.store import AccountingStore


def _store(tmp_path: Path) -> AccountingStore:
    store = AccountingStore(str(tmp_path / "accounting.db"))
    store.initialize()
    return store


def _tx(counterparty: str, category: str, description: str, time: str = "2026-06-01 10:00:00"):
    return {
        "source": "test",
        "time": time,
        "direction": "支出",
        "amount": 10,
        "counterparty": counterparty,
        "product": "",
        "trade_type": "商户消费",
        "category": category,
        "description": description,
        "mark": "✓",
        "month_key": time[:7].replace("-", "."),
        "map_key": f"商户消费-{counterparty}",
    }


def test_counterparty_hints_rank_by_history_count(tmp_path: Path):
    """含『华南师范』+ 大类=吃 → 应推荐历史最高频的『兰州拉面』，而非 KFC。"""
    store = _store(tmp_path)
    records = []
    for i in range(8):
        records.append(_tx("正宗兰州拉面佛山华南师范大学店", "吃", "兰州拉面",
                           f"2026-06-0{1 + i} 12:00:00"))
    for i in range(4):
        records.append(_tx("华南师范大学", "吃", "食堂餐费", f"2026-06-1{ i} 12:00:00"))
    records.append(_tx("华南师范大学", "吃", "麻辣香锅"))
    records.append(_tx("华南师范大学", "生活", "宿舍水电费"))
    # KFC 与华南师范无关，属于其它对方
    records.append(_tx("KFC", "吃", "KFC"))
    store.upsert_transactions(records)

    hints = store.get_counterparty_hints()
    ranked = compute_desc_hints(hints, "华南师范大学", "吃")

    assert ranked[0] == "食堂餐费"          # 华南师范大学 自身历史最高频
    assert "麻辣香锅" in ranked
    assert "KFC" not in ranked             # ⛔ 反向锚：绝不能推荐出 KFC


def test_hints_fuzzy_match_across_similar_names(tmp_path: Path):
    """『华南师范商业』这类近似名应通过包含匹配命中『华南师范大学』的历史说明。"""
    store = _store(tmp_path)
    store.upsert_transactions([
        _tx("华南师范大学", "吃", "食堂餐费", "2026-06-01 12:00:00"),
        _tx("华南师范大学", "吃", "食堂餐费", "2026-06-02 12:00:00"),
    ])
    ranked = compute_desc_hints(store.get_counterparty_hints(), "华南师范商业", "吃")
    assert ranked[0] == "食堂餐费"


def test_hints_scoped_to_category(tmp_path: Path):
    """同一对方在不同大类下的说明不得串味。"""
    store = _store(tmp_path)
    store.upsert_transactions([
        _tx("cool", "吃", "豆奶", "2026-06-01 12:00:00"),
        _tx("cool", "宿舍共享", "群收款", "2026-06-02 12:00:00"),
    ])
    hints = store.get_counterparty_hints()
    assert compute_desc_hints(hints, "cool", "吃") == ["豆奶"]
    assert compute_desc_hints(hints, "cool", "宿舍共享") == ["群收款"]


def test_hints_empty_or_skip_counterparty_returns_empty(tmp_path: Path):
    store = _store(tmp_path)
    store.upsert_transactions([
        _tx("KFC", "吃", "KFC"),
        # 空说明的历史记录不进频次表
        _tx("无名商户", "吃", "", "2026-06-03 12:00:00"),
        # 空大类（未分类）的历史记录不进频次表
        _tx("未分类商户", "", "某说明", "2026-06-04 12:00:00"),
    ])
    hints = store.get_counterparty_hints()
    assert compute_desc_hints(hints, "", "吃") == []
    assert compute_desc_hints(hints, "/", "吃") == []      # 跳过项占位名
    assert compute_desc_hints(hints, "不存在对方", "吃") == []
    assert compute_desc_hints({}, "KFC", "吃") == []
    assert "无名商户" not in hints
    assert "未分类商户" not in hints


def test_no_hit_does_not_guess(tmp_path: Path):
    """无历史命中时返回空列表 —— 调用方据此留空，而不是填下拉框第 0 项。"""
    store = _store(tmp_path)
    store.upsert_transactions([_tx("KFC", "吃", "KFC")])
    assert compute_desc_hints(store.get_counterparty_hints(), "乐友乐特价批发超市", "生活") == []

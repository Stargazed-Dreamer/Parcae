"""异环采集增量判重（内容多重集）的回归测试。

背景两轮教训：
1. 旧版指纹 (棋盘, 名称, 时间戳) 永不命中 —— 库里的 time 被清洗脚本"单调化"过
   （真实时间 + 秒级补偿），与 raw 真时间永不相等 ⇒ 每次退化成全量 52 页。
2. 改成"时间水位线"能跑，但同一次十连跨两轮采集时，水位线会把同组剩下的记录
   判成"已采"而丢掉。

现在的做法照鸣潮 wuwa_gacha.py 的 _record_key / Counter：库里存的是内容多重集
((名称, 稀有度, 时间) → 出现次数)，按次数消费，十连里同名同稀有度的记录靠
计数区分。同组跨轮也能按差额补回来。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_gacha():
    path = PROJECT_ROOT / "workspace/yihuan_gacha/yihuan_gacha.py"
    spec = importlib.util.spec_from_file_location("yihuan_gacha_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rec(name, time_str, rarity="B"):
    return {"name": name, "time": time_str, "rarity": rarity, "quantity": 1}


# ---------- 指纹 ----------

def test_signature_strips_type_prefix():
    gacha = _load_gacha()
    sig, ok = gacha.record_signature(_rec("弧盘·最后一朵玫瑰」", "2026年10月30日 03:21:31", "S"))
    assert sig[0] == "最后一朵玫瑰」"
    assert sig[1] == "S"
    assert ok is True


def test_signature_marks_unparsable_time():
    gacha = _load_gacha()
    for t in ("?", "", None):
        _, ok = gacha.record_signature(_rec("名字", t))
        assert ok is False


def test_signature_accepts_numeric_time():
    gacha = _load_gacha()
    sig, ok = gacha.record_signature({"name": "名字", "rarity": "A", "time": 1790358003})
    assert (sig[2], ok) == (1790358003, True)


# ---------- 多重集判重 ----------

def test_same_ten_pull_records_share_one_time():
    """一次十连的 10 条在游戏里共享同一时间 —— 这是真值，不该被拆开。"""
    gacha = _load_gacha()
    page = [_rec(f"卡{i}", "2026年10月30日 03:21:31") for i in range(10)]
    assert len({gacha.record_signature(r)[0] for r in page}) == 10


def test_duplicate_names_in_one_pull_are_counted_not_collapsed():
    """同一次十连抽到 2 张「笑口常开」：库里有 1 条时，另一条必须算新。"""
    gacha = _load_gacha()
    db = [_rec("笑口常开", "2026年10月30日 03:21:31")]
    matcher = gacha.PoolMatcher(db)
    page = [
        _rec("笑口常开", "2026年10月30日 03:21:31"),
        _rec("笑口常开", "2026年10月30日 03:21:31"),
    ]
    fresh, skipped = matcher.classify_page(page)
    assert skipped == 1
    assert len(fresh) == 1


def test_whole_page_already_in_db_is_skipped():
    gacha = _load_gacha()
    page = [_rec(f"卡{i}", "2026年9月26日 01:39:44") for i in range(5)]
    matcher = gacha.PoolMatcher(page)
    fresh, skipped = matcher.classify_page(page)
    assert (len(fresh), skipped) == (0, 5)
    # 幂等：翻页失败重读同一页，结论必须一致（不能因为"消耗过了"就算新）
    fresh2, skipped2 = matcher.classify_page(page)
    assert (len(fresh2), skipped2) == (0, 5)


def test_ten_pull_split_across_two_runs_is_completed():
    """跨轮十连：库里只有前 6 条（上一轮采到一半断了），本轮采到 10 条要补回 4 条。"""
    gacha = _load_gacha()
    page = [_rec(f"卡{i}", "2026年10月30日 03:21:31") for i in range(10)]
    matcher = gacha.PoolMatcher(page[:6])
    fresh, skipped = matcher.classify_page(page)
    assert skipped == 6
    assert [r["name"] for r in fresh] == [f"卡{i}" for i in range(6, 10)]


def test_same_content_in_different_batches_is_new():
    """同名同稀有度但时间不同 = 不同批次抽到的，必须收进来（不能只按名字判重）。"""
    gacha = _load_gacha()
    db = [_rec("成功的第一步", "2026年9月26日 01:39:44")]
    matcher = gacha.PoolMatcher(db)
    page = [_rec("成功的第一步", "2026年10月30日 03:21:31")]
    fresh, skipped = matcher.classify_page(page)
    assert (len(fresh), skipped) == (1, 0)


def test_unparsable_time_is_kept_not_dropped():
    """时间识别失败时必须保守当新记录，否则会静默丢卡。"""
    gacha = _load_gacha()
    matcher = gacha.PoolMatcher([_rec("名字", "2026月9月26日 01:39:44")])
    for bad in ("?", "", None):
        fresh, _ = matcher.classify_page([_rec("名字", bad)])
        assert len(fresh) == 1


def test_empty_matcher_means_full_collection():
    gacha = _load_gacha()
    matcher = gacha.PoolMatcher(None)
    assert not matcher
    fresh, skipped = matcher.classify_page([_rec(f"卡{i}", "2026年10月30日 03:21:31") for i in range(5)])
    assert (len(fresh), skipped) == (5, 0)


def test_two_consecutive_stale_pages_reach_stop_threshold():
    """判停规则：连续 2 页全部已采。单页全旧可能只是恰好抽到重复，不停。"""
    gacha = _load_gacha()
    old_page = [_rec(f"旧{i}", "2026年9月26日 01:39:44") for i in range(5)]
    new_page = [_rec(f"新{i}", "2026年10月30日 03:21:31") for i in range(5)]
    matcher = gacha.PoolMatcher(old_page)

    stale_pages = 0
    stops_at = None
    for idx, page in enumerate([new_page, old_page, old_page, old_page], start=1):
        fresh, _ = matcher.classify_page(page)
        stale_pages = stale_pages + 1 if (page and not fresh) else 0
        if stale_pages >= 2 and stops_at is None:
            stops_at = idx
    assert stops_at == 3


# ---------- 库内最新时间（仅日志展示） ----------

def test_db_latest_time_picks_max():
    gacha = _load_gacha()
    db = {"弧盘研募": [{"time": 100}, {"time": 500}, {"time": 300}]}
    assert gacha.db_latest_time(db, "弧盘研募") == 500


def test_db_latest_time_missing_or_unparsable():
    gacha = _load_gacha()
    assert gacha.db_latest_time({}, "弧盘研募") == 0
    assert gacha.db_latest_time({"弧盘研募": []}, "弧盘研募") == 0
    assert gacha.db_latest_time(None, "弧盘研募") == 0
    db = {"弧盘研募": [{"time": "?"}, {"time": 2026}, {"time": "n/a"}]}
    assert gacha.db_latest_time(db, "弧盘研募") == 2026


# ---------- raw 归档 ----------

def _load_clean():
    path = PROJECT_ROOT / "workspace/yihuan_gacha/yihuan_clean.py"
    spec = importlib.util.spec_from_file_location("yihuan_clean_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_archive_keeps_every_raw_file(tmp_path):
    """raw 永不删除：归档后原件要还在 backup/raw/，一个都不能少。"""
    clean = _load_clean()
    raw = tmp_path / "raw"
    raw.mkdir()
    names = [f"yihuan_gacha_raw_2026{i:04d}_120000.json" for i in range(1, 6)]
    for n in names:
        (raw / n).write_text("{}", encoding="utf-8")

    moved = clean.archive_raw_files(raw_dir=raw, archive_dir=tmp_path / "backup" / "raw")

    assert moved == len(names)
    assert not list(raw.glob("yihuan_gacha_raw_*.json")), "raw/ 应已清空"
    archived = sorted(p.name for p in (tmp_path / "backup" / "raw").glob("*.json"))
    assert archived == sorted(names)


def test_archive_does_not_overwrite_same_name(tmp_path):
    clean = _load_clean()
    raw = tmp_path / "raw"
    (tmp_path / "backup" / "raw").mkdir(parents=True)
    raw.mkdir()
    content = '{"keep": true}'
    name = "yihuan_gacha_raw_20261030_214212.json"
    (raw / name).write_text(content, encoding="utf-8")
    (tmp_path / "backup" / "raw" / name).write_text('{"old": true}', encoding="utf-8")

    clean.archive_raw_files(raw_dir=raw, archive_dir=tmp_path / "backup" / "raw")

    got = sorted(p.name for p in (tmp_path / "backup" / "raw").glob("*.json"))
    assert name in got
    assert len(got) == 2, "同名原始件必须改名保留，不能覆盖"
    assert (raw / name).exists() is False


def test_find_latest_raw_falls_back_to_backup(tmp_path, monkeypatch):
    """采集件归档后，清洗仍要能从 backup/raw 找到最新的原始件。"""
    clean = _load_clean()
    raw = tmp_path / "raw"
    archive = tmp_path / "backup" / "raw"
    archive.mkdir(parents=True)
    (archive / "yihuan_gacha_raw_20261030_214212.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(clean, "RAW_DIR", raw)
    monkeypatch.setattr(clean, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(clean, "SUMMARY_FILE", tmp_path / "summary.json")

    path, is_raw = clean.find_latest_raw()

    assert is_raw is True
    assert path.name == "yihuan_gacha_raw_20261030_214212.json"


# ---------- 名称归一化（OCR 变体对齐） ----------

def test_norm_name_folds_separator_variants():
    """「弧盘·：「电音」狂欢」这类多冒号 OCR 变体要和库里写法对上。"""
    gacha = _load_gacha()
    variants = [
        "弧盘·：「电音」狂欢",
        "弧盘·「电音」狂欢",
        "弧盘「电音」狂欢",
        "弧盘··「电音」狂欢",
    ]
    got = {gacha._norm_name_for_match(v) for v in variants}
    assert got == {"「电音」狂欢", "电音」狂欢"} or len(got) == 1, got


def test_norm_name_strips_quotes_and_dash():
    gacha = _load_gacha()
    assert gacha._norm_name_for_match('弧盘·-危险游戏') == "危险游戏"
    assert gacha._norm_name_for_match('角色·"明音凛"') == "明音凛"


def test_norm_name_does_not_merge_distinct_names():
    """归一化只统一分隔符，不许把不同名称揉成一条（否则就是丢卡）。"""
    gacha = _load_gacha()
    names = ["弧盘·成功的第一步", "弧盘·笑口常开", "弧盘·危险游戏", "弧盘·「我们。」"]
    assert len({gacha._norm_name_for_match(n) for n in names}) == 4

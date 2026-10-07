"""占位小时补回 + 失败原因取键链（wip_b074ac03）。

两条实测缺口：
1. hourly 整点总结写过占位后，调度器永远不再回头看那个小时 —— catchup 只认
   "没跑过的周期"，而占位是"跑过但没产出"。修法：Action.pending_retry_targets
   让动作自己申报待重做的时刻，catchup 把它并入补跑清单。
2. loop_manager 只读 result["error"]；没写 error 的 action（ScreenVL 把原因放在
   vl_status）在收件箱里退化成「错误: unknown」。修法：result_message 取键链，
   与 skipped 分支的 D11 链对齐。

全部用 tmp_path + mock，不碰真实 LLM / 真实 data 目录。
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from server.activity_tracker import loop_manager as lm
from server.activity_tracker.loop_actions import DailySummarizeAction, HourlySummarizeAction

# 同类对象按"调用时的模块属性"取：test_loop_manager_manifest.py 会 importlib.reload
# 这个模块，把名字 import 进来自会拿到被换掉的旧类，isinstance 判定跟着失真。


def _run(coro):
    return asyncio.run(coro)


def _top_of_hour() -> datetime:
    return datetime.now().replace(minute=0, second=0, microsecond=0)


# ==================== pending_retry_targets ====================

class TestPendingRetryTargets:
    """占位小时的补回申报：只对"重跑有意义"的占位排队"""

    def _dirs(self, tmp_path: Path) -> tuple[Path, Path]:
        raw = tmp_path / "raw"
        hourly = tmp_path / "hourly"
        raw.mkdir(parents=True, exist_ok=True)
        hourly.mkdir(parents=True, exist_ok=True)
        return raw, hourly

    def _context(self, raw: Path, hourly: Path) -> dict:
        return {
            "task_id": "activity_tracker.hourly_summarize",
            "config": {
                "raw_data_dir": str(raw),
                "hourly_data_dir": str(hourly),
            },
        }

    def _placeholder(self, hourly: Path, hour_dt: datetime, marker: str) -> Path:
        p = hourly / f"{hour_dt.strftime('%Y%m%d_%H')}.md"
        p.write_text(f"# {hour_dt:%Y-%m-%d %H:00} 活动总结\n\n> ⚠️ {marker}\n", encoding="utf-8")
        return p

    def _raw_data(self, raw: Path, hour_dt: datetime) -> None:
        record = {"ts": hour_dt.replace(minute=30).isoformat(), "windows": [{"title": "T"}]}
        with open(raw / f"windows_{hour_dt:%Y%m%d}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def test_llm_failure_placeholder_with_data_is_queued(self, tmp_path):
        raw, hourly = self._dirs(tmp_path)
        hour = _top_of_hour() - timedelta(hours=2)   # k=1 → tick = top-1h
        self._placeholder(hourly, hour, "本时段 LLM 调用失败（重试 3 次仍失败）")
        self._raw_data(raw, hour)

        targets = HourlySummarizeAction().pending_retry_targets(self._context(raw, hourly))

        assert len(targets) == 1
        assert targets[0] == pytest.approx((_top_of_hour() - timedelta(hours=1)).timestamp(), abs=1), \
            "申报的应是该时段的整点触发时刻"

    def test_no_data_placeholder_waits_until_data_arrives(self, tmp_path):
        raw, hourly = self._dirs(tmp_path)
        hour = _top_of_hour() - timedelta(hours=2)
        self._placeholder(hourly, hour, "本时段无采集数据（后端可能未运行）")

        action = HourlySummarizeAction()
        assert action.pending_retry_targets(self._context(raw, hourly)) == []

        self._raw_data(raw, hour)
        assert len(action.pending_retry_targets(self._context(raw, hourly))) == 1

    def test_content_filter_placeholder_is_not_queued(self, tmp_path):
        raw, hourly = self._dirs(tmp_path)
        hour = _top_of_hour() - timedelta(hours=2)
        self._placeholder(hourly, hour, "本时段因内容审核保护触发，LLM 拒绝生成详细总结")
        self._raw_data(raw, hour)

        assert HourlySummarizeAction().pending_retry_targets(self._context(raw, hourly)) == []

    def test_real_summary_is_not_queued(self, tmp_path):
        raw, hourly = self._dirs(tmp_path)
        hour = _top_of_hour() - timedelta(hours=2)
        (hourly / f"{hour:%Y%m%d_%H}.md").write_text(
            f"# {hour:%Y-%m-%d %H:00} 活动总结\n\n用户在做正事。\n", encoding="utf-8"
        )
        self._raw_data(raw, hour)

        assert HourlySummarizeAction().pending_retry_targets(self._context(raw, hourly)) == []

    def test_already_regenerated_hour_is_not_queued_twice(self, tmp_path):
        raw, hourly = self._dirs(tmp_path)
        hour = _top_of_hour() - timedelta(hours=2)
        self._placeholder(hourly, hour, "本时段 LLM 调用失败")
        (hourly / f"{hour:%Y%m%d_%H}_v2.md").write_text("# 真实总结\n", encoding="utf-8")
        self._raw_data(raw, hour)

        assert HourlySummarizeAction().pending_retry_targets(self._context(raw, hourly)) == []

    def test_window_and_cap(self, tmp_path):
        raw, hourly = self._dirs(tmp_path)
        action = HourlySummarizeAction()
        top = _top_of_hour()
        # 窗口外的占位（60 小时前）
        far = top - timedelta(hours=61)
        self._placeholder(hourly, far, "本时段 LLM 调用失败")
        self._raw_data(raw, far)
        assert action.pending_retry_targets(self._context(raw, hourly)) == []

        # 窗口内 5 个占位 → 每次最多补 REVISIT_MAX_PER_CATCHUP 个，且先补最近的
        for k in range(2, 7):
            hour = top - timedelta(hours=k)
            self._placeholder(hourly, hour, "本时段 LLM 调用失败")
            self._raw_data(raw, hour)
        targets = action.pending_retry_targets(self._context(raw, hourly))
        assert len(targets) == HourlySummarizeAction.REVISIT_MAX_PER_CATCHUP
        expected = {(top - timedelta(hours=k)).timestamp() for k in (1, 2, 3)}
        assert set(targets) == expected, "应优先补最近的占位时段，且不超过上限"

    def test_missing_hourly_dir_returns_empty(self, tmp_path):
        ctx = {"config": {"raw_data_dir": str(tmp_path), "hourly_data_dir": str(tmp_path / "nope")}}
        assert HourlySummarizeAction().pending_retry_targets(ctx) == []


# ==================== catchup 并入申报时刻 ====================

class _RevisitAction:  # 不继承 Action：模块可能被 reload，基类引用会失真
    """只申报一个待重做时刻的假动作；execute 记录收到的 catchup_target_ts"""

    action_type = "fake_revisit"

    def __init__(self, target_ts: float):
        self.target_ts = target_ts
        self.seen: list[float | None] = []

    def pending_retry_targets(self, context: dict) -> list[float]:
        return [self.target_ts]

    async def execute(self, context: dict) -> dict:
        self.seen.append(context.get("catchup_target_ts"))
        return {"success": True}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("server.activity_tracker.loop_manager.STATE_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr("server.inbox.get_store", lambda: MagicMock())
    return tmp_path


class TestCatchupUnion:
    def _task(self, action: "lm.Action") -> "lm.LoopTask":
        return lm.LoopTask(
            task_id="fake.revisit",
            trigger=lm.CronTrigger("0 * * * *", catchup=True),
            action=action,
            config={},
            source_segment="fake",
            # 刚刚跑过 ⇒ missed 为空，补跑只能来自 pending_retry_targets
            last_run_at=time.time(),
        )

    def test_placeholder_hour_is_caught_up_without_missed_ticks(self, isolated):
        tick = (_top_of_hour() - timedelta(hours=1)).timestamp()
        action = _RevisitAction(tick)
        mgr = lm.LoopManager({"enabled": True, "tasks": {}})
        task = self._task(action)

        did = _run(mgr._catchup_task_missed_runs(task))

        assert did is True, "没有错过的周期时也应按申报时刻补跑"
        assert action.seen == [tick]

    def test_no_revisit_targets_means_no_run(self, isolated):
        action = _RevisitAction(0.0)
        action.pending_retry_targets = lambda context: []   # type: ignore[method-assign]
        mgr = lm.LoopManager({"enabled": True, "tasks": {}})

        assert _run(mgr._catchup_task_missed_runs(self._task(action))) is False
        assert action.seen == []

    def test_broken_probe_does_not_kill_catchup(self, isolated):
        action = _RevisitAction((_top_of_hour() - timedelta(hours=1)).timestamp())

        def boom(context: dict) -> list[float]:
            raise RuntimeError("探测炸了")

        action.pending_retry_targets = boom   # type: ignore[method-assign]
        mgr = lm.LoopManager({"enabled": True, "tasks": {}})

        assert _run(mgr._catchup_task_missed_runs(self._task(action))) is False
        assert action.seen == []


# ==================== 失败原因取键链 ====================

class TestResultMessage:
    def test_error_key_wins(self):
        assert lm.result_message({"success": False, "error": "429 rate_limited"}) == "429 rate_limited"

    def test_vl_status_only_failure_is_not_unknown(self):
        """ScreenVL 改前的真实返回形状：没有 error，原因只在 vl_status 里"""
        msg = lm.result_message({
            "success": False,
            "vl_status": "failed_after_retries",
            "elapsed_ms": 1234,
        })
        assert msg == "failed_after_retries"
        assert msg != "unknown"

    def test_empty_string_error_falls_through(self):
        assert lm.result_message({"success": False, "error": "", "reason": "上游空响应"}) == "上游空响应"

    def test_keeps_unknown_as_floor(self):
        assert lm.result_message({"success": False}) == "unknown"

    def test_base_action_has_no_revisit_targets(self):
        assert lm.Action().pending_retry_targets({"config": {}}) == []


# ==================== 日报取最新版本，不喂占位文本 ====================

class TestDailyPicksNewestVersion:
    def test_v2_real_summary_replaces_placeholder(self, tmp_path, monkeypatch):
        hourly = tmp_path / "hourly"
        daily = tmp_path / "daily"
        hourly.mkdir(parents=True, exist_ok=True)
        daily.mkdir(parents=True, exist_ok=True)

        day = datetime.now() - timedelta(days=1)
        date_str = day.strftime("%Y%m%d")
        hour = day.replace(hour=13, minute=0, second=0, microsecond=0)
        (hourly / f"{date_str}_13.md").write_text(
            f"# {hour:%Y-%m-%d %H:00} 活动总结\n\n> ⚠️ 本时段 LLM 调用失败（重试 3 次仍失败）。\n",
            encoding="utf-8",
        )
        (hourly / f"{date_str}_13_v2.md").write_text(
            f"# {hour:%Y-%m-%d %H:00} 活动总结\n\n用户在编辑 report.py，跑了三轮回归。\n",
            encoding="utf-8",
        )

        prompts: list[str] = []

        def fake_call_llm(messages, **kwargs):
            prompts.append(messages[1]["content"])
            return {"ok": True, "content": "## 概述\n\n一天正常工作。\n" + "补充说明。" * 20}

        monkeypatch.setattr("server.llm_pool.call_llm", fake_call_llm)

        result = _run(DailySummarizeAction().execute({
            "task_id": "activity_tracker.daily_summarize",
            "config": {
                "hourly_data_dir": str(hourly),
                "daily_data_dir": str(daily),
                "daily_summarize_retry_backoff": 0,
            },
            "catchup_target_ts": datetime.now().replace(hour=5, minute=30).timestamp(),
        }))

        assert result["success"] is True
        assert len(prompts) == 1
        assert "编辑 report.py" in prompts[0]
        assert "本时段 LLM 调用失败" not in prompts[0], "日报不该把占位文本当小时总结喂进去"


# ==================== catchup 扫描结果的可见性 ====================

class TestCatchupScanIsVisible:
    """扫过就要留一行。

    2026-10-07 现场复验时，"重启后日志里 0 条 catchup"被我差点读成"机件没跑"，
    实际是 48h 窗口内确实没有待重做的时段——两种情形在日志里长得一模一样。
    """

    def _task(self, action: object, catchup: bool = True) -> "lm.LoopTask":
        return lm.LoopTask(
            task_id="fake.visibility",
            trigger=lm.CronTrigger("0 * * * *", catchup=catchup),
            action=action,  # type: ignore[arg-type]
            config={},
            source_segment="fake",
            # 刚刚跑过 ⇒ missed 为空，队列长度只由 action 的申报决定
            last_run_at=time.time(),
        )

    @staticmethod
    def _scan_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
        return [r.getMessage() for r in caplog.records if "扫描 missed=" in r.getMessage()]

    def test_empty_scan_still_leaves_a_line(self, caplog):
        import logging

        action = _RevisitAction(0.0)
        action.pending_retry_targets = lambda context: []   # type: ignore[method-assign]
        mgr = lm.LoopManager({"enabled": True, "tasks": {}})

        with caplog.at_level(logging.INFO):
            assert _run(mgr._catchup_task_missed_runs(self._task(action))) is False

        assert self._scan_lines(caplog) == [
            "Loop catchup: fake.visibility 扫描 missed=0 revisit=0 排队=0"
        ], "没活可干也得看得见，否则与「没跑到」同形"

    def test_declared_revisit_shows_up_in_the_counts(self, caplog):
        import logging

        tick = (_top_of_hour() - timedelta(hours=1)).timestamp()
        action = _RevisitAction(tick)
        mgr = lm.LoopManager({"enabled": True, "tasks": {}})

        with caplog.at_level(logging.INFO):
            assert _run(mgr._catchup_task_missed_runs(self._task(action))) is True

        assert any("扫描 missed=0 revisit=1 排队=1" in ln for ln in self._scan_lines(caplog))
        assert action.seen == [tick]

    def test_non_catchup_task_stays_silent(self, caplog):
        """启动时遍历 16 个任务，不该为不走 catchup 的那些各刷一行"""
        import logging

        action = _RevisitAction(0.0)
        mgr = lm.LoopManager({"enabled": True, "tasks": {}})

        with caplog.at_level(logging.INFO):
            assert _run(mgr._catchup_task_missed_runs(
                self._task(action, catchup=False))) is False

        assert self._scan_lines(caplog) == []

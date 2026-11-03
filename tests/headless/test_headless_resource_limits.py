"""headless-agent-session Ticket 11 验收测试：资源限制默认值 + 配置覆盖

覆盖 Ticket 11 acceptance（temp/sdd/headless-agent-session/tickets.md）：
- [x] 验证默认值生效：max_iterations=100, wall_clock_budget_secs=1800,
      judge_max_iterations=10, judge_wall_clock_budget_secs=300
- [x] 验证配置覆盖：config.toml 设小值，任务按配置值限制

B2（spec D2/D3）：max_budget_usd / judge_max_budget_usd 字段已删除，
本测试文件移除所有 budget_usd 相关断言（功能已移除，不再测试）。

butler-runtime T6 起：headless 不再在 8766 进程里组装 RunnerConfig，而是把
资源限制放进 submit 载荷交给 runtime。因此这里断言**提交载荷**（真正的对外行为），
而不是进程内 SessionFacade 的 RunnerConfig。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 确保 PROJECT_ROOT 在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fake_runtime import FakeRuntimeClient, assistant  # noqa: E402

from server.activity_tracker import headless_runner as hr  # noqa: E402
from server.activity_tracker.headless_runner import (  # noqa: E402
    DEFAULT_JUDGE_MAX_ITERATIONS,
    DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_WALL_CLOCK_BUDGET_SECS,
    HeadlessConfig,
    parse_headless_config,
    run_judge_agent,
    run_main_agent,
)

# ============================================================================
# Fixtures & helpers
# ============================================================================


def _install_fake_runtime(monkeypatch) -> FakeRuntimeClient:
    """把 headless_runner 的 runtime 客户端换成脚本替身。"""
    fake = FakeRuntimeClient()
    fake.on("headless", messages=[assistant("主 agent 完成")])
    fake.on(
        "headless_judge",
        messages=[assistant('{"verdict": "success", "reason": "ok"}')],
    )
    monkeypatch.setattr(hr, "_make_runtime_client", lambda config: fake)
    return fake


# ============================================================================
# Part 1: 默认值传播到 submit 载荷
# ============================================================================


class TestDefaultValuesPropagated:
    """Ticket 11: HeadlessConfig() 默认值应原样进入 submit 载荷。"""

    def test_main_agent_defaults_propagated_to_runner_config(self, tmp_db_path, monkeypatch):
        """run_main_agent 用 HeadlessConfig() 默认值时，提交值 = spec 默认值。"""
        fake = _install_fake_runtime(monkeypatch)

        config = HeadlessConfig()
        # 显式断言 HeadlessConfig 默认值（spec 我替领导拍的板）
        assert config.max_iterations == DEFAULT_MAX_ITERATIONS  # 100
        # B2: 删 max_budget_usd 字段，不再断言
        assert config.wall_clock_budget_secs == DEFAULT_WALL_CLOCK_BUDGET_SECS  # 1800

        async def run():
            return await run_main_agent(
                trigger_text="测试默认值",
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(fake.submits) == 1

        submitted = fake.submits[0]
        # 关键验收点：默认值原样传给 runtime
        assert submitted["max_iterations"] == DEFAULT_MAX_ITERATIONS, (
            f"默认 max_iterations 应为 {DEFAULT_MAX_ITERATIONS}, 实际 {submitted['max_iterations']}"
        )
        assert submitted["wall_clock_budget_secs"] == DEFAULT_WALL_CLOCK_BUDGET_SECS, (
            f"默认 wall_clock_budget_secs 应为 {DEFAULT_WALL_CLOCK_BUDGET_SECS}, "
            f"实际 {submitted['wall_clock_budget_secs']}"
        )

    def test_judge_agent_defaults_propagated_to_runner_config(self, tmp_db_path, monkeypatch):
        """run_judge_agent 用 HeadlessConfig() 默认值时，提交值 = judge spec 默认值。"""
        fake = _install_fake_runtime(monkeypatch)

        config = HeadlessConfig()
        # judge 默认值断言
        assert config.judge_max_iterations == DEFAULT_JUDGE_MAX_ITERATIONS  # 10
        # B2: 删 judge_max_budget_usd 字段，不再断言
        assert config.judge_wall_clock_budget_secs == DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS  # 300

        async def run():
            return await run_judge_agent(
                main_output=["任务完成"],
                trigger_text="测试 judge 默认值",
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(fake.submits) == 1

        submitted = fake.submits[0]
        assert submitted["max_iterations"] == DEFAULT_JUDGE_MAX_ITERATIONS, (
            f"judge 默认 max_iterations 应为 {DEFAULT_JUDGE_MAX_ITERATIONS}, "
            f"实际 {submitted['max_iterations']}"
        )
        assert submitted["wall_clock_budget_secs"] == DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS, (
            f"judge 默认 wall_clock_budget_secs 应为 {DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS}, "
            f"实际 {submitted['wall_clock_budget_secs']}"
        )

    def test_default_constants_match_spec(self):
        """默认常量与 spec 一致（防止后续误改默认值）。"""
        assert DEFAULT_MAX_ITERATIONS == 100
        # B2: 删 DEFAULT_MAX_BUDGET_USD 常量
        assert DEFAULT_WALL_CLOCK_BUDGET_SECS == 1800
        assert DEFAULT_JUDGE_MAX_ITERATIONS == 10
        # B2: 删 DEFAULT_JUDGE_MAX_BUDGET_USD 常量
        assert DEFAULT_JUDGE_WALL_CLOCK_BUDGET_SECS == 300


# ============================================================================
# Part 2: 配置覆盖传播到 submit 载荷
# ============================================================================


class TestConfigOverridePropagated:
    """Ticket 11: parse_headless_config(小值 dict) 应让提交载荷用配置值。"""

    def test_main_agent_override_smaller_values_propagated(self, tmp_db_path, monkeypatch):
        """config_dict 设小值（如 max_iterations=10）→ 提交载荷用配置值。"""
        fake = _install_fake_runtime(monkeypatch)

        # config_dict 设小值（模拟 config.toml 收紧资源限制）
        cfg_dict = {
            "trigger_text": "测试覆盖",
            "max_iterations": 10,
            # B2: 删 max_budget_usd 配置项
            "wall_clock_budget_secs": 300,
            "db_path": tmp_db_path,
        }
        config = parse_headless_config(cfg_dict)
        assert config.max_iterations == 10
        # B2: 删 max_budget_usd 字段，不再断言
        assert config.wall_clock_budget_secs == 300

        async def run():
            return await run_main_agent(
                trigger_text=config.trigger_text,
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(fake.submits) == 1

        submitted = fake.submits[0]
        assert submitted["max_iterations"] == 10, (
            f"覆盖 max_iterations 应为 10, 实际 {submitted['max_iterations']}"
        )
        assert submitted["wall_clock_budget_secs"] == 300, (
            f"覆盖 wall_clock_budget_secs 应为 300, 实际 {submitted['wall_clock_budget_secs']}"
        )

    def test_judge_agent_override_smaller_values_propagated(self, tmp_db_path, monkeypatch):
        """judge config_dict 设小值 → judge 提交载荷用配置值。"""
        fake = _install_fake_runtime(monkeypatch)

        cfg_dict = {
            "trigger_text": "测试 judge 覆盖",
            "judge_max_iterations": 3,
            # B2: 删 judge_max_budget_usd 配置项
            "judge_wall_clock_budget_secs": 60,
            "db_path": tmp_db_path,
        }
        config = parse_headless_config(cfg_dict)
        assert config.judge_max_iterations == 3
        # B2: 删 judge_max_budget_usd 字段，不再断言
        assert config.judge_wall_clock_budget_secs == 60

        async def run():
            return await run_judge_agent(
                main_output=["任务完成"],
                trigger_text=config.trigger_text,
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(fake.submits) == 1

        submitted = fake.submits[0]
        assert submitted["max_iterations"] == 3, (
            f"judge 覆盖 max_iterations 应为 3, 实际 {submitted['max_iterations']}"
        )
        assert submitted["wall_clock_budget_secs"] == 60, (
            f"judge 覆盖 wall_clock_budget_secs 应为 60, 实际 {submitted['wall_clock_budget_secs']}"
        )

    def test_main_and_judge_use_independent_config_fields(self, tmp_db_path, monkeypatch):
        """主 agent 和 judge agent 资源限制独立配置（不混用）。

        场景：main max_iterations=20, judge_max_iterations=2
        验证：两次 submit 各用各的字段。
        """
        fake = _install_fake_runtime(monkeypatch)

        cfg_dict = {
            "trigger_text": "测试独立配置",
            "max_iterations": 20,
            # B2: 删 max_budget_usd 配置项
            "wall_clock_budget_secs": 600,
            "judge_max_iterations": 2,
            # B2: 删 judge_max_budget_usd 配置项
            "judge_wall_clock_budget_secs": 30,
            "db_path": tmp_db_path,
        }
        config = parse_headless_config(cfg_dict)

        async def run():
            main_result = await run_main_agent(
                trigger_text=config.trigger_text,
                config=config,
            )
            # 主 agent 跑完后再跑 judge（模拟 HeadlessSessionAction 流程）
            judge_result = await run_judge_agent(
                main_output=main_result.last_assistant_messages or ["完成"],
                trigger_text=config.trigger_text,
                config=config,
            )
            return main_result, judge_result

        main_result, judge_result = asyncio.run(run())
        assert main_result.error == ""
        assert judge_result.error == ""
        assert len(fake.submits) == 2  # 一次 main + 一次 judge

        main_submit = fake.submits[0]
        judge_submit = fake.submits[1]

        # 主 agent 用 main 字段
        assert main_submit["max_iterations"] == 20
        assert main_submit["wall_clock_budget_secs"] == 600

        # judge 用 judge_* 字段（不是 main 字段）
        assert judge_submit["max_iterations"] == 2
        assert judge_submit["wall_clock_budget_secs"] == 30

    def test_partial_override_keeps_other_defaults(self, tmp_db_path, monkeypatch):
        """部分覆盖：只设 max_iterations，其他字段保持默认值。"""
        fake = _install_fake_runtime(monkeypatch)

        cfg_dict = {
            "trigger_text": "测试部分覆盖",
            "max_iterations": 50,  # 只设这一个
            "db_path": tmp_db_path,
        }
        config = parse_headless_config(cfg_dict)
        assert config.max_iterations == 50
        # 未覆盖的字段保持默认
        # B2: 删 max_budget_usd 字段，不再断言
        assert config.wall_clock_budget_secs == DEFAULT_WALL_CLOCK_BUDGET_SECS

        async def run():
            return await run_main_agent(
                trigger_text=config.trigger_text,
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(fake.submits) == 1

        submitted = fake.submits[0]
        assert submitted["max_iterations"] == 50  # 覆盖值
        assert submitted["wall_clock_budget_secs"] == DEFAULT_WALL_CLOCK_BUDGET_SECS  # 默认值


# ============================================================================
# Part 3: model_override 透传（spec 要求 judge 用 judge_model 字段）
# ============================================================================


class TestModelOverridePropagated:
    """Ticket 11: model / judge_model 字段透传到 submit 的 model_override。"""

    def test_main_agent_model_passed_to_runner_config(self, tmp_db_path, monkeypatch):
        """config.model 字段透传到 model_override。"""
        fake = _install_fake_runtime(monkeypatch)

        config = HeadlessConfig(
            trigger_text="测试 model",
            model="deepseek-chat",
        )

        async def run():
            return await run_main_agent(
                trigger_text=config.trigger_text,
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(fake.submits) == 1
        assert fake.submits[0]["model_override"] == "deepseek-chat"

    def test_judge_agent_judge_model_passed_to_runner_config(self, tmp_db_path, monkeypatch):
        """config.judge_model 字段透传到 judge 的 model_override。"""
        fake = _install_fake_runtime(monkeypatch)

        config = HeadlessConfig(
            trigger_text="测试 judge model",
            judge_model="deepseek-chat",
        )

        async def run():
            return await run_judge_agent(
                main_output=["完成"],
                trigger_text=config.trigger_text,
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(fake.submits) == 1
        assert fake.submits[0]["model_override"] == "deepseek-chat"  # judge_model 透传

    def test_empty_model_uses_default_tier(self, tmp_db_path, monkeypatch):
        """model="" 时 model_override=""（默认 tier，由 LLM 池决定）。"""
        fake = _install_fake_runtime(monkeypatch)

        config = HeadlessConfig()  # model 留空

        async def run():
            return await run_main_agent(
                trigger_text="测试默认 tier",
                config=config,
            )

        result = asyncio.run(run())
        assert result.error == ""
        assert len(fake.submits) == 1
        assert fake.submits[0]["model_override"] == ""  # 留空 = 默认 tier

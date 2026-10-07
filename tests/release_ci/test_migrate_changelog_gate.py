"""migrate_changelog 的发布口径复检闸门（wip_fe6da6fa 遗留 C）。

要挡的事：豁免按**路径**登记在 profile 的 [sensitive_line_skips] 里，
归档把已豁免的历史条目从 CHANGELOG.md 搬进 docs/changelog-archive.md 之后，
那条豁免就不再覆盖它 —— 下个发版周期 prepare 的 HIGH 会复活，而原来只有人脑
记得这件事。

拆成两层测：
- 探测真值：scan_release_counts 确实读到了那两个文件（scanned==2），
  并且命中明细里没有行内容（不脱敏不回显纪律）。
- 判据本体：compare_scan 是纯函数，用合成账本证明 HIGH 上涨会被拦、
  MEDIUM 上涨不会、原有命中不会被当成新增。
"""

import sys

import pytest

from tools.migrate_changelog import (
    _EXIT_RELEASE_SCAN_REGRESSION,
    _profile_name,
    compare_scan,
    scan_release_counts,
    verify_after_archive,
)


def _hit(path: str, line: int, rule: str, severity: str = "HIGH") -> dict:
    return {"path": path, "line": line, "rule": rule, "severity": severity}


def _ledger(counts: dict[str, int], hits: list[dict] | None = None) -> dict:
    return {"counts": counts, "hits": hits or [], "scanned": 2}


# ==================== 探测地板 ====================

class TestProbeFloor:
    def test_both_changelog_files_are_actually_read(self):
        result = scan_release_counts(_profile_name(None))
        assert result["scanned"] == 2, "探测没读到两个文件，比对就是在比空集"

    def test_hits_never_carry_line_content(self):
        result = scan_release_counts(_profile_name(None))
        for h in result["hits"]:
            assert set(h) == {"path", "line", "rule", "severity"}, h

    def test_profile_name_follows_policy(self):
        assert _profile_name("custom") == "custom"
        assert _profile_name(None)  # 读 release/policy.toml 的 default_profile


# ==================== 判据本体（合成账本） ====================

class TestCompareScan:
    def test_identical_ledger_passes(self):
        led = _ledger({"HIGH": 0, "MEDIUM": 7})
        code, lines = compare_scan(led, led)
        assert code == 0
        assert "OK" in lines[0]

    def test_high_reactivation_is_blocked_and_located(self):
        before = _ledger({"HIGH": 0})
        after = _ledger(
            {"HIGH": 1, "MEDIUM": 7},
            [_hit("docs/changelog-archive.md", 1412, "private-repository-reference")],
        )
        code, lines = compare_scan(before, after)
        assert code == _EXIT_RELEASE_SCAN_REGRESSION
        joined = "\n".join(lines)
        assert "docs/changelog-archive.md:1412" in joined
        assert "private-repository-reference" in joined
        assert "HIGH 从 0 涨到 1" in joined

    def test_medium_growth_alone_is_not_a_regression(self):
        before = _ledger({"HIGH": 0, "MEDIUM": 7})
        after = _ledger({"HIGH": 0, "MEDIUM": 40}, [_hit("CHANGELOG.md", 9, "local-absolute-path", "MEDIUM")])
        assert compare_scan(before, after)[0] == 0

    def test_pre_existing_high_is_not_listed_as_new(self):
        hit = _hit("docs/changelog-archive.md", 1412, "private-repository-reference")
        before = _ledger({"HIGH": 1}, [hit])
        after = _ledger({"HIGH": 1}, [hit])
        code, lines = compare_scan(before, after)
        assert code == 0
        assert not any("新增 HIGH" in line for line in lines)

    def test_flat_high_count_does_not_list_new_hits(self):
        """HIGH 总数没涨就放行 —— 换归属文件但数量相同，prepare 的判据看的是 HIGH 数"""
        before = _ledger({"HIGH": 1}, [_hit("CHANGELOG.md", 30, "private-repository-reference")])
        after = _ledger({"HIGH": 1}, [_hit("docs/changelog-archive.md", 30, "private-repository-reference")])
        code, lines = compare_scan(before, after)
        assert code == 0
        assert not any("新增 HIGH" in line for line in lines)


# ==================== verify 的接线 ====================

class TestVerifyWiring:
    def test_probe_failure_fails_closed(self, monkeypatch):
        def boom(profile: str) -> dict:
            raise RuntimeError("profile 读不到")

        monkeypatch.setattr("tools.migrate_changelog.scan_release_counts", boom)
        code = verify_after_archive(_ledger({"HIGH": 0}), "public-full")
        assert code == _EXIT_RELEASE_SCAN_REGRESSION, "复检跑不起来不能算通过"

    def test_clean_archive_reports_ok(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "tools.migrate_changelog.scan_release_counts",
            lambda profile: _ledger({"HIGH": 0, "MEDIUM": 3}),
        )
        code = verify_after_archive(_ledger({"HIGH": 0, "MEDIUM": 3}), "public-full")
        assert code == 0
        assert "OK" in capsys.readouterr().out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

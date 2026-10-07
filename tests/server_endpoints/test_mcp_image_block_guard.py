"""MCP base64 泄漏防线回归测试

背景（2026-09-21 修复）：
- 一次 `browser_screenshot` 往 LLM 上下文塞了 181424 字符 JPEG base64。
- 根因：`mcp_gateway` 的 ImageContent 转换按**枚举响应形状**实现，只认顶层
  `mcp_image_block` 与 `result.mcp_image_block` 两种；而 browser 把标记放在
  `response["data"]` 下 → 两个分支都不命中 → 转换根本不触发。
- 兜底截断也确实触发了，但 `head=text[:47600]` 对无结构的 base64 等于没防
  （头部和中部一样脏）。
- 同因第二个 bug：`browser_write_lesson` 的 `_insert_section_content` 用
  `^## 已知坑$` 精确匹配，而 11/16 个已有 lessons 文件写的是
  `## 已知坑（与正常行为区分）` → 默认 section 在多数文件上永远追加不进去。

本测试锁住的行为：
1. 任意包装层下的 `{mcp_image_block, image}` 都能被摘出来（顶层/result/data/更深嵌套）
2. 摘取时不丢兄弟键（早期实现遍历到就 return，会漏掉后面的键）
3. 没有 image 块时原样返回，不误伤
4. 超长 base64 连续段被折成占位符，普通文本/短哈希不受影响
5. 带括号注解的标题能匹配，但"已知坑详解"这类不同章节不能被"已知坑"误匹配
   （`browser_screenshot` 默认值由 tests/browser/test_browser.py 锁，此处不重复）
"""

import pytest

from server.browser.site_lessons import _insert_section_content
from server.core.mcp_gateway import _collapse_base64_runs, _scrub_image_block

B64 = "AAH//x" * 10  # 短串，不该被当成图片块载荷
FAKE_IMG = "A" * 4096


def _resp(payload: dict) -> dict:
    return {**payload, "success": True, "elapsed_ms": 12}


# ========== 1. 形状不限 ==========

@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("top", id="顶层"),
        pytest.param("result", id="result嵌套"),
        pytest.param("data", id="data嵌套_本次事故形状"),
        pytest.param("deep", id="两层嵌套"),
    ],
)
def test_image_block_extracted_from_any_wrapper(shape: str) -> None:
    block = {"image": FAKE_IMG, "mime_type": "image/jpeg", "mcp_image_block": True}
    if shape == "top":
        data = _resp({**block, "path": "/tmp/x.png"})
    elif shape == "result":
        data = _resp({"result": block})
    elif shape == "data":
        data = _resp({"path": "/tmp/x.png", "data": block})
    else:
        data = _resp({"result": {"items": [block]}})

    summary, image, mime = _scrub_image_block(data)

    assert image == FAKE_IMG
    assert mime == "image/jpeg"
    # 摘干净：summary 里任何深度都不该再留 base64 或标记
    assert "mcp_image_block" not in repr(summary)
    assert FAKE_IMG not in repr(summary)


def test_sibling_keys_survive_scrub() -> None:
    """遍历到 image 块后，它后面/前面的兄弟键必须都还在（早期实现会漏）。"""
    data = {
        "alpha": 1,
        "data": {"mcp_image_block": True, "image": FAKE_IMG, "width": 1024},
        "beta": 2,
        "path": "/tmp/x.png",
    }
    summary, image, _ = _scrub_image_block(data)

    assert image == FAKE_IMG
    assert summary["alpha"] == 1
    assert summary["beta"] == 2
    assert summary["path"] == "/tmp/x.png"
    # 内层非图片键保留，只有 image/mcp_image_block 被摘
    assert summary["data"]["width"] == 1024
    assert "image" not in summary["data"]


def test_no_image_block_returns_unchanged() -> None:
    data = {"success": True, "nodes": [{"css": "a"}], "path": "/tmp/x.png"}
    summary, image, mime = _scrub_image_block(data)

    assert image is None
    assert mime is None
    assert summary == data


def test_missing_image_value_does_not_convert() -> None:
    """只有标记没有 image（或 image 非字符串）时不该误判成图片块。"""
    data = {"data": {"mcp_image_block": True, "image": None}}
    summary, image, _ = _scrub_image_block(data)

    assert image is None
    assert summary == data


# ========== 2. base64 折叠 ==========

def test_long_base64_run_collapsed() -> None:
    text = '{"image": "' + ("Zm9vYmFy" * 600) + '"}'
    out = _collapse_base64_runs(text)

    assert "base64 omitted" in out
    assert len(out) < len(text) / 10


def test_normal_text_and_short_hashes_untouched() -> None:
    text = '{"hash": "d41d8cd98f00b204e9800998ecf8427e", "msg": "ok 12345 done"}'
    assert _collapse_base64_runs(text) == text


def test_collapse_is_idempotent() -> None:
    text = "x" * 5000
    once = _collapse_base64_runs(text)
    assert _collapse_base64_runs(once) == once


# ========== 3. lessons 标题匹配 ==========

def test_suffixed_heading_matches() -> None:
    """11/16 个已有文件写的是「## 已知坑（与正常行为区分）」，必须能追加进去。"""
    body = "# 站点\n\n## 已知坑（与正常行为区分）\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n## 遗留问题\n\n（无）\n"
    new_body, found = _insert_section_content(body, "已知坑", "| 新坑 | 做法 |")

    assert found is True
    assert "| 新坑 | 做法 |" in new_body
    # 必须落在 已知坑 段内、遗留问题 之前
    assert new_body.index("新坑") < new_body.index("## 遗留问题")


def test_exact_heading_still_matches() -> None:
    body = "# 站点\n\n## 已知坑\n\n| a | b |\n\n## 遗留问题\n\n（无）\n"
    new_body, found = _insert_section_content(body, "已知坑", "| 新坑 |")

    assert found is True
    assert "| 新坑 |" in new_body


def test_ascii_paren_variant_matches() -> None:
    body = "# 站点\n\n## 已知坑 (与正常行为区分)\n\n内容\n\n## 遗留问题\n\n（无）\n"
    _, found = _insert_section_content(body, "已知坑", "| 新坑 |")
    assert found is True


def test_different_section_not_falsely_matched() -> None:
    """放宽只针对括号注解，不允许「已知坑」误匹配「已知坑详解」。"""
    body = "# 站点\n\n## 已知坑详解\n\n内容\n\n## 遗留问题\n\n（无）\n"
    _, found = _insert_section_content(body, "已知坑", "| 新坑 |")
    assert found is False


def test_absent_section_reports_false() -> None:
    body = "# 站点\n\n## 网站概况\n\n内容\n"
    _, found = _insert_section_content(body, "已知坑", "| 新坑 |")
    assert found is False


# 注：`browser_screenshot` 默认 return_base64=False 由
# tests/browser/test_browser.py::TestBrowserScreenshotModel::test_screenshot_request_defaults
# 锁住，此处不重复断言（避免同一事实两处手抄）。

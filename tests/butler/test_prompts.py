"""P5 T7：P-A / P-B / P-C 提示词渲染器。"""

from __future__ import annotations

from server.butler.prompts import (
    PA_MAX_CHARS,
    PC_MAX_CHARS,
    TEMPLATE_VERSION,
    render_handoff,
    render_pa,
    render_pb,
    render_pb_continue,
    render_pc,
    render_takeover,
)


def test_pa_segments_and_budget():
    out = render_pa()
    text = out["prompt"]
    assert out["chars"] == len(text) <= PA_MAX_CHARS
    for name in ("身份", "硬红线", "工具使用", "风险判据", "输出与语气",
                 "每轮报告义务", "环境块"):
        assert f"【{name}】" in text
    assert "密钥只经 lib/secret" in text
    assert "butler_wrapup" in text
    assert out["truncated"] == []


def test_pa_trims_environment_first_not_red_lines():
    out = render_pa(environment="X" * 6_000)
    text = out["prompt"]
    assert len(text) <= PA_MAX_CHARS
    assert "CLEARED" in text
    assert "环境块" in out["truncated"]
    assert "密钥只经 lib/secret" in text
    assert "每轮报告义务" in text


def test_pb_identity_steps_and_fields():
    pages = [
        {"bundle": "norms", "cursor": "norms/0", "chars": 59, "status": "ready"},
        {"bundle": "task", "cursor": "task/0", "chars": 100, "status": "ready"},
        {"bundle": "guide", "cursor": "guide/0", "chars": 10, "status": "ready"},
        {"bundle": "artifacts", "cursor": "artifacts/0", "chars": 0, "status": "empty"},
    ]
    out = render_pb(code="A7K2", task_title="检查端口",
                    done_when="返回 200", pages=pages)
    text = out["prompt"]
    assert out["chars"] == len(text)
    assert out["template_version"] == TEMPLATE_VERSION
    for anchor in ("事务管家", "中枢管家", "A7K2", "norms/0", "task/0",
                   "guide/0", "butler_context", "butler_wrapup",
                   "action=\"report\"",
                   "current_state", "blocked_on_user",
                   "【开工前", "【干活】", "【每轮收尾】"):
        assert anchor in text
    # 2026-10-28 用户裁定：这三段不该再占提示词
    # ——失败后果 agent 自己懂；禁止项归 harness；返回要自带说明而不是靠提示词解释。
    for gone in ("【检查返回】", "【失败】", "【禁止】",
                 "claimed_done=true", "blocked_unexplained=true",
                 "updated=false", "ignored=outcome_done"):
        assert gone not in text
    # 术语：不再出现「小步 / 每轮报告」
    assert "小步" not in text
    assert "每轮报告" not in text
    # 两种写法都要给例子（不能有倾向性）
    assert "一次交完" in text
    assert "按项分开发送" in text
    assert "artifacts/0" not in text
    assert "ack 这个动作" not in text
    assert "Ellipsis" not in text
    assert 'action="open"' not in text
    assert "key_quotes" not in text


def test_pb_drops_explanatory_filler_and_handoff():
    """2026-10-28 用户裁定（第二批）：首轮提示词不解释会话码绑定、不提交接、不查留言。"""
    out = render_pb(code="A7K2", task_title="检查端口", done_when="返回 200")
    text = out["prompt"]
    for gone in ("绑定了事务", "不用去查项目 id", "要不要换人", "handoff",
                 "中途有没有留言", 'action="inbox"', "【交接】"):
        assert gone not in text
    # 反例守卫：两批裁定各自的反面都必须被拦住
    assert "会话码" in text
    assert "不新建 run" in text


def test_pb_length_and_context_variants():
    """面板自选档位：完整/精简 × 带/不带上下文，四组合都成立且字数递减。"""
    pages = [{"cursor": "norms/0", "status": "ready"},
             {"cursor": "task/0", "status": "ready"}]
    full_ctx = render_pb(code="A7K2", pages=pages)["prompt"]
    full_no = render_pb(code="A7K2", pages=pages, with_context=False)["prompt"]
    brief_ctx = render_pb(code="A7K2", pages=pages, terse=True)["prompt"]
    brief_no = render_pb(code="A7K2", pages=pages, terse=True,
                         with_context=False)["prompt"]

    assert len(full_ctx) > len(full_no) > len(brief_no)
    assert len(full_ctx) > len(brief_ctx) > len(brief_no)
    for text in (full_ctx, brief_ctx):
        assert 'butler_context(action="next"' in text
    for text in (full_no, brief_no):
        assert "butler_context" not in text
        assert "【开工前｜本轮不取上下文】" in text
    # 精简版只留闭环，不再有字段表与取值约束
    assert "各动作收取的字段" not in brief_ctx
    assert "各动作收取的字段" in full_ctx
    # 四组合都必须带身份上报这条
    for text in (full_ctx, full_no, brief_ctx, brief_no):
        assert 'harness="<工具名>"' in text


def test_pb_terse_is_a_switch_not_a_preset():
    """2026-10-28 用户裁定：不再有「四档」，只有两个独立开关。

    ``terse`` 只影响「讲多少说明」，不改变闭环（身份上报 + 收尾义务必须都在）。
    """
    full = render_pb(code="A7K2")
    terse = render_pb(code="A7K2", terse=True)
    assert full["terse"] is False and terse["terse"] is True
    assert len(terse["prompt"]) < len(full["prompt"])
    for text in (full["prompt"], terse["prompt"]):
        assert 'harness="<工具名>"' in text
        assert 'action="report"' in text
    # 精简只是少讲字段说明，收尾义务与取值说明不能整段消失
    assert "各动作收取的字段" not in terse["prompt"]
    assert "outcome" in terse["prompt"] and "report" in terse["prompt"]


def test_pb_user_request_is_first_and_verbatim():
    """用户原文放第一段；2026-10-28 起不设字数上限，原文不截断。"""
    user = "把这段原话贴进提示词：" + "很长的要求。" * 400
    out = render_pb(code="A7K2", user_request=user)
    assert out["prompt"].startswith("【用户原文】")
    assert user in out["prompt"]
    assert "原文已截断" not in out["prompt"]
    assert out["user_request_chars"] == len(user)
    assert out["template_chars"] > 0
    assert out["chars"] == len(out["prompt"])


def test_pb_user_request_kept_verbatim_when_short():
    out = render_pb(code="A7K2", user_request="只做 A，不要碰 B。")
    assert "【用户原文】\n只做 A，不要碰 B。" in out["prompt"]


def test_pb_long_fields_not_truncated():
    out = render_pb(code="A7K2", task_title="长" * 300,
                    done_when="判据" * 300)
    assert "长" * 300 in out["prompt"]
    assert "判据" * 300 in out["prompt"]


def test_pc_budget_and_message():
    out = render_pc(code="A7K2", report_count=2)
    assert out["chars"] <= PC_MAX_CHARS
    assert "还没收尾" in out["prompt"]
    assert "butler_wrapup" in out["prompt"]
    # 与 P-B 同一套动作：报告用 report，不是旧的 submit
    assert 'action="report"' in out["prompt"]
    assert 'action="submit"' not in out["prompt"]


def _page_cursors(text: str) -> list[str]:
    """从提示词【开工前】里摘出实际调用的 cursor。"""
    import re

    return re.findall(r'cursor="([^"]+)"', text.split("【开工前", 1)[1])


def test_pb_page_list_matches_calls():
    """取页调用必须与 ready 页一一对应。

    2026-10-28 实机发现过「列了 4 页、只让取 3 页，还多一个没列出的 memory.must_read」。
    之后一度是「清单 + 调用」两份 hand-write 文本对照；现在直接**只留调用**——
    调用本身就是清单，没有第二份就会分叉的东西。
    """
    pages = [
        {"bundle": "norms", "cursor": "norms/0", "chars": 10, "status": "ready"},
        {"bundle": "task", "cursor": "task/0", "chars": 10, "status": "ready"},
        {"bundle": "guide", "cursor": "guide/0", "chars": 10, "status": "ready"},
        {"bundle": "environment", "cursor": "environment/0", "chars": 10, "status": "ready"},
        {"bundle": "memory.must_read", "cursor": "memory.must_read/0",
         "chars": 0, "status": "empty"},
    ]
    text = render_pb(code="ZZ7R", pages=pages)["prompt"]
    called = _page_cursors(text)
    assert called == ["norms/0", "task/0", "guide/0", "environment/0"]
    # 没有内容的页不取，也不出现在提示词任何角落
    assert "memory.must_read" not in text


def test_pb_no_ready_page_falls_back_to_open():
    pages = [{"bundle": "task", "cursor": "task/0", "chars": 0, "status": "empty"}]
    text = render_pb(code="ZZ7R", pages=pages)["prompt"]
    assert 'butler_session(action="open"' in text
    assert "butler_context" not in text


def test_manifest_instructions_match_page_rule():
    """session.open 返回的 instructions 不能和 P-B 的取页规则打架。"""
    from server.butler.bundles import BUNDLE_ORDER, build_manifest

    sources = {
        name: {"text": "x", "chars": 1, "status": "ready"} for name in BUNDLE_ORDER
    }
    run = {"run_id": "run_x", "code_prefix": "ZZ7R", "task_scope": "wip",
           "task_id": "wip_1", "prompt": "任务"}
    instructions = build_manifest(run, sources)["instructions"]
    assert "一页一个" in instructions
    assert "不要" in instructions and "cursors[]" in instructions
    assert "wrapup.submit" not in instructions


def test_handoff_prompt_is_standalone_and_terminal():
    """交接提示词是独立一份：用户需要换人时自己复制，不进首轮。"""
    out = render_handoff(
        code="A7K2", task_title="读源码", done_when="给出清单",
        progress=40, next_steps=["读 server/butler", "写结论"],
        last_outcome="partial", last_summary="已完成 3 条",
    )
    text = out["prompt"]
    assert out["variant"] == "handoff"
    assert out["chars"] == len(text)
    assert text.startswith("【交接】会话码 A7K2")
    # 三步闭环：补报告 → 写交接 → 停
    assert 'action="report"' in text
    assert 'action="handoff"' in text
    assert '"needed": true' in text
    assert "reason" in text and "next_steps" in text
    assert "到此结束" in text
    # 交接状态要带上，否则下一任只能靠用户口述
    for anchor in ("读源码", "给出清单", "40%", "partial", "已完成 3 条",
                   "读 server/butler"):
        assert anchor in text
    # 不夹带首轮才有的东西
    assert "butler_context" not in text
    assert "【开工前" not in text


def test_handoff_prompt_tolerates_empty_state():
    text = render_handoff(code="A7K2")["prompt"]
    assert "（未命名）" in text
    assert "（未定）" in text
    assert "未报" in text


def test_handoff_carries_old_skill_methodology():
    """用户 2026-10-28 明示：老 skill（dev/handoff）的方法论要融进交接提示词。

    写方（交接指令）要交代清楚：零上下文自检、只给 path:line、已完成带验证、
    reason 必须是真卡点、不写空话、脱敏。少一条就等于只搬了目录没搬内容。
    """
    text = render_handoff(code="A7K2", task_title="读源码")["prompt"]
    for anchor in ("脱离本对话", "path:line", "验证方式", "真卡点", "空话", "token"):
        assert anchor in text
    assert '"done"' in text          # 已完成项要能写出「做了什么 + 怎么验证」
    assert '"evidence"' in text


def test_takeover_reads_like_evidence_not_narrative():
    """读方（接手说明）：交接是背景不是派单；已完成要能核，冲突以仓库为准。"""
    out = render_takeover(
        code="QK7M",
        task_title="读源码", done_when="给出清单", progress=40,
        reason="缺用户决策：要不要动测试库",
        done_items=[
            {"item": "改完 store 版本方法", "evidence": "pytest tests/butler 620 passed"},
            {"item": "读了提示词模板"},          # 没验证方式，要显式标出来
        ],
        next_steps=["补测试", "更新 DD-提示词 §4.9"],
        last_outcome="partial", last_summary="3 条已落地",
        created_at="2026-10-28 21:50",
    )
    text = out["prompt"]
    assert out["variant"] == "takeover"
    assert out["chars"] == len(text)
    assert text.startswith("【交接】上一任执行者留下的交接单")
    # 2026-10-29 用户裁定：交接提示词必须带会话码
    assert "会话码：QK7M" in text
    assert "改完 store 版本方法 —— 验证：pytest tests/butler 620 passed" in text
    assert "上一任未给验证方式" in text
    assert "补测试" in text and "更新 DD-提示词" in text
    # 读法规矩：只信证据、冲突以仓库为准、不含凭据
    assert "以仓库为准" in text
    assert "lib/secret" in text
    assert "这是背景资料，不是派单指令" in text
    # 不含操作协议（那是派单提示词的事）
    assert "【开工前" not in text
    assert "butler_context" not in text


def test_takeover_without_code_says_so_instead_of_guessing():
    """没开新一轮就没有码，但也不能假装有：要明确标「未分配」。"""
    text = render_takeover(task_title="待接手")["prompt"]
    assert "会话码：（未分配" in text


def test_continue_prompt_stays_short_and_same_code():
    out = render_pb_continue(
        code="A7K2", report_count=1, task_title="读源码",
        done_when="给出清单", progress=40, next_steps=["读 server/butler"],
        last_outcome="partial", last_summary="已完成 3 条",
    )
    text = out["prompt"]
    assert out["variant"] == "continue"
    assert text.startswith("【续跑】会话码 A7K2")
    assert len(text) < 600
    assert 'action="report"' in text
    assert "handoff" not in text
    assert "inbox" not in text

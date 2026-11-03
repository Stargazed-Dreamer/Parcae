"""DL4 请求模型（7 入口的其中 4 个，action 作第一个字段）。"""

from __future__ import annotations

from pydantic import Field

from lib.schema import BaseSchema


class RunCreateRequest(BaseSchema):
    prompt: str = Field(..., min_length=1, description="任务原始要求（用户原话，会放进提示词第一段）")
    task_title: str | None = Field(None, description="可选；留空由执行 agent 根据原文补")
    done_when: str | None = Field(None, description="可选；留空由执行 agent 根据原文补")
    task_scope: str | None = Field(None, description="todos / wip / adhoc")
    task_id: str | None = None
    project_id: str | None = None
    harness: str | None = None
    owner: str | None = None
    handoff_id: str | None = None
    domain_id: str | None = None
    label: str | None = Field(
        None, max_length=40,
        description="这一路在做什么（如「修 bug-A」）。多路并行时靠它区分；空则面板显示会话码",
    )


class RunStateUpdateRequest(BaseSchema):
    """面板纠偏：改「这一路」的状态显示（不是给 agent 的接口）。"""

    label: str | None = Field(None, max_length=40)
    progress: int | None = Field(None, ge=0, le=100)
    next_steps: list[str] | None = None
    state_note: str | None = Field(None, max_length=500)


class RunPromptUpdateRequest(BaseSchema):
    """用户在面板里改派单提示词后主动保存。"""

    prompt_text: str = Field(..., min_length=1, description="完整提示词文本（含会话码）")


class RunPromptAppendRequest(BaseSchema):
    """面板「存为新版」：把当前正文另存成一个新版本号，历史版原样保留。

    与 ``RunPromptUpdateRequest`` 的区别：那个是改**已有某一版**的正文，
    这个是**追加**一版（版本号 +1），用来攒「这次想试另一种写法」。
    """

    prompt_text: str = Field(..., min_length=1, description="新版本的完整正文（含会话码）")
    note: str | None = Field(None, max_length=200,
                             description="这一版为什么攒；面板列表里显示")
    activate: bool = Field(True, description="存完是否设为当前生效版，默认是")


class RunHandoffPromptSaveRequest(BaseSchema):
    """交接提示词手改后保存；空串 = 清掉手改，回到实时渲染。"""

    prompt_text: str = Field("", description="改后的全文；传空串表示清除手改")


class RunArchiveRequest(BaseSchema):
    """把这一路收进归档视图（v15）。"""

    reason: str | None = Field(None, max_length=200,
                               description="归档原因；面板归档详情显示，可空")


class SessionAction(BaseSchema):
    action: str = Field(..., description="open / note")
    code: str = Field(..., min_length=4, description="会话码（4 位字母数字）")
    owner: str | None = None
    harness: str | None = None
    model: str | None = Field(None, description="执行 agent 自报的模型名；可为空")
    text: str | None = Field(None, description="note：要点/决策/踩坑")
    kind: str = Field("note", description="note 类型")
    client_note_id: str | None = Field(None, description="note 幂等键")


class ContextAction(BaseSchema):
    action: str = Field(..., description="本期只支持 next")
    code: str
    cursor: str | None = Field(None, description="bundle/page；不传从第一页开始")
    cursors: list[str] | None = Field(None, description="多 cursor：一次取多页")


class WrapupAction(BaseSchema):
    action: str = Field(
        ..., description="submit（兼容整包）/ report / task / memory / feedback / handoff"
    )
    code: str
    outcome: str = Field("", description="report/submit 必填：progress/done/partial/blocked/failed")
    summary: str = ""
    task_updates: dict = Field(default_factory=dict)
    memory_candidates: list[dict] = Field(default_factory=list)
    quotes: list = Field(default_factory=list, description="可选；默认不记 harness 原话")
    recall_feedback: list[dict] = Field(default_factory=list)
    handoff: dict = Field(default_factory=dict)
    artifacts: list = Field(default_factory=list)
    evidence: list = Field(default_factory=list, description="report：done 的证据")
    blocked_on_user: dict | None = None
    next_run_hint: str | dict | None = None
    idempotency_key: str | None = None
    # report 时 agent 自报执行身份；首轮必填，之后可省（面板靠它显示「这轮是谁在跑」）
    harness: str | None = Field(None, description="执行 agent 所在的工具/宿主，如 CodeBuddy / Cursor")
    model: str | None = Field(None, description="执行 agent 使用的模型名")


class MemoryWriteAction(BaseSchema):
    action: str = Field(..., description="本期只支持 write")
    content: str = Field(..., min_length=1)
    kind: str = "fact"
    scope: str = "global"
    title: str | None = None
    summary: str | None = None
    origin: str = "agent_extracted"
    sensitivity: str = "private-eng"
    must_read: int = 0
    evidence_refs: list[str] | None = None
    source_quote_ids: list[int] | None = None
    expires_at: str | None = None
    key: str | None = None
    fact_type: str | None = None

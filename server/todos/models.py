"""待办模块 Pydantic 模型"""

from enum import Enum
from typing import Literal, Optional
from pydantic import Field, model_validator

from lib.schema import BaseSchema

#: 结构轴（DL2）：决定到期行为，见 server/todos/classify.py
TaskClass = Literal["recurring_dated", "one_shot_dated", "small_undated", "long_undated"]
#: 目的轴（DL2）：决定交到哪一步（日常→全自动 / 一次性→基本自动 / 成长→辅助）
Purpose = Literal["daily", "one_shot", "growth"]


class TodoFrequency(str, Enum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"
    quarterly = "quarterly"


class TodoStatus(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    done = "done"
    blocked = "blocked"
    skipped = "skipped"
    archived = "archived"


class WipStatus(str, Enum):
    active = "active"
    paused = "paused"
    blocked = "blocked"
    completed = "completed"
    # 2026-10-28：面板要在 5 个状态之间直接切换，缺 archived 就只能混用 action，
    # 而 action 的 complete 会顺带把 progress 写到 100（用户明确不要这个副作用）。
    archived = "archived"


class TodoCreate(BaseSchema):
    title: str = Field(..., min_length=1, max_length=200)
    skill: Optional[str] = None
    type: str = Field("recurring", pattern="^(recurring|phased_recurring|triggered)$")
    frequency: Optional[TodoFrequency] = None
    condition: Optional[str] = None
    notes: Optional[str] = None
    related_memory_keys: list[str] = Field(default_factory=list)
    # phased_recurring 专属：阶段性周期任务的起止日期（ISO YYYY-MM-DD）
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    # triggered 专属：触发条件 JSON 字符串，如 {"event":"file_arrived","watch_dir":"...","pattern":"*.csv"}
    trigger_condition: Optional[str] = None
    # DL2 两轴：不填则由 classify 规则判（结构轴）或留空待标注（目的轴）
    task_class: Optional[TaskClass] = None
    purpose: Optional[Purpose] = None
    next_review_at: Optional[str] = None

    @model_validator(mode="after")
    def validate_type_specific_fields(self) -> "TodoCreate":
        """按 type 校验专属字段：
        - phased_recurring 必须有 start_date + end_date + frequency
        - triggered 必须有 trigger_condition
        """
        if self.type == "phased_recurring":
            missing = []
            if not self.start_date:
                missing.append("start_date")
            if not self.end_date:
                missing.append("end_date")
            if not self.frequency:
                missing.append("frequency")
            if missing:
                raise ValueError(
                    f"phased_recurring 类型必须提供 {', '.join(missing)}"
                )
        elif self.type == "triggered":
            if not self.trigger_condition:
                raise ValueError("triggered 类型必须提供 trigger_condition")
        return self


class TodoUpdate(BaseSchema):
    title: Optional[str] = None
    skill: Optional[str] = None
    status: Optional[TodoStatus] = None
    frequency: Optional[TodoFrequency] = None
    condition: Optional[str] = None
    condition_status: Optional[str] = Field(None, pattern="^(active|inactive)$")
    notes: Optional[str] = None
    related_memory_keys: Optional[list[str]] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    trigger_condition: Optional[str] = None
    task_class: Optional[TaskClass] = None
    purpose: Optional[Purpose] = None
    next_review_at: Optional[str] = None


class TodoDoneRequest(BaseSchema):
    done_date: Optional[str] = None  # ISO date, default today


class WipTaskCreate(BaseSchema):
    title: str = Field(..., min_length=1, max_length=200)
    priority: str = Field("medium", pattern="^(high|medium|low)$")
    goal: str = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    related_skills: list[str] = Field(default_factory=list)
    related_files: list[str] = Field(default_factory=list)
    current_state: dict = Field(default_factory=dict)
    blocked_reason: Optional[str] = None
    related_memory_keys: list[str] = Field(default_factory=list)
    extra_data: dict = Field(default_factory=dict)
    # DL2：有 due_at 即判 one_shot_dated；extra_data.one_session=true 即判 small_undated
    task_class: Optional[TaskClass] = None
    purpose: Optional[Purpose] = None
    due_at: Optional[str] = None
    done_when: Optional[str] = None
    next_review_at: Optional[str] = None


class WipTaskUpdate(BaseSchema):
    title: Optional[str] = None
    status: Optional[WipStatus] = None
    priority: Optional[str] = None
    goal: Optional[str] = None
    progress: Optional[int] = Field(None, ge=0, le=100)
    next_steps: Optional[list[str]] = None
    related_skills: Optional[list[str]] = None
    related_files: Optional[list[str]] = None
    current_state: Optional[dict] = None
    blocked_reason: Optional[str] = None
    related_memory_keys: Optional[list[str]] = None
    extra_data: Optional[dict] = None
    task_class: Optional[TaskClass] = None
    purpose: Optional[Purpose] = None
    due_at: Optional[str] = None
    done_when: Optional[str] = None
    next_review_at: Optional[str] = None


class TaskClassUpdate(BaseSchema):
    """人工改分类（PUT /tasks/{scope}/{id}/class）。只改传进来的字段。"""

    task_class: Optional[TaskClass] = None
    purpose: Optional[Purpose] = None
    due_at: Optional[str] = None
    done_when: Optional[str] = None
    next_review_at: Optional[str] = None

    @model_validator(mode="after")
    def require_one_field(self) -> "TaskClassUpdate":
        if not any(
            v is not None
            for v in (self.task_class, self.purpose, self.due_at, self.done_when, self.next_review_at)
        ):
            raise ValueError("至少改一项：task_class / purpose / due_at / done_when / next_review_at")
        return self


class TaskActionRequest(BaseSchema):
    """统一任务动作（T14）：todos 与 wip 共用一套词。"""

    action: str = Field(..., pattern="^(pause|resume|archive|unarchive|complete)$")
    reason: str | None = Field(None, max_length=200,
                               description="archive 时记下归档原因；面板归档详情显示，可空")


class WipBatchGetRequest(BaseSchema):
    """批量取 WIP 详情：一次拿几条，替代关掉 summary 全量拉取。"""
    ids: list[str] = Field(..., min_length=1, max_length=100)

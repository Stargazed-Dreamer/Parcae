"""事务分类（DL2）——结构轴四类 + 目的轴三档。

纯函数模块：只做判定与派生视图，不碰数据库（落库在 `store.py`）。
设计真源：`workspace/local_butler/30_架构与设计/13_事务分类_DL2实施设计_20261027.md`
口径出处：`20_决策过程/20_记忆第二轮/15` §5.2（四类与映射）+ `40_盘点与证据/20_事务盘点/30_汇总/08` §5（目的轴）

两个轴分工，不要混：
- **结构轴**决定"到期怎么表现"：要不要生成 occurrence、会不会过期、算不算残留、什么时候进复盘。
- **目的轴**决定"交到哪一步"：日常→完全自动 / 一次性→基本自动 / 成长→辅助。
- **L0-L5 管授权与停点，目的轴管委托形态**，两者不换算、不互相覆盖（E2 判"成长"
  和它标 L5 主动发起不矛盾：L5 管谁发起，成长三档管交到哪一步）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Optional

# ─── 结构轴 ───────────────────────────────────────────────────

STRUCT_RECURRING = "recurring_dated"
STRUCT_ONE_SHOT = "one_shot_dated"
STRUCT_SMALL = "small_undated"
STRUCT_LONG = "long_undated"

STRUCT_TYPES: tuple[str, ...] = (
    STRUCT_RECURRING,
    STRUCT_ONE_SHOT,
    STRUCT_SMALL,
    STRUCT_LONG,
)

STRUCT_LABELS: dict[str, str] = {
    STRUCT_RECURRING: "有日期循环",
    STRUCT_ONE_SHOT: "有日期一次性",
    STRUCT_SMALL: "无日期小任务",
    STRUCT_LONG: "无日期长期",
}

STRUCT_BEHAVIOR: dict[str, str] = {
    STRUCT_RECURRING: "到点生成 occurrence；没到不做；模板永不 done",
    STRUCT_ONE_SHOT: "过期转 overdue，不自动消失，直到有证据才算 done",
    STRUCT_SMALL: "不进过期，只排散在队列尾部的残留小事",
    STRUCT_LONG: "按期进复盘（next_review_at），老化只提示不催",
}

# ─── 目的轴 ───────────────────────────────────────────────────

PURPOSE_DAILY = "daily"
PURPOSE_ONE_SHOT = "one_shot"
PURPOSE_GROWTH = "growth"

PURPOSES: tuple[str, ...] = (PURPOSE_DAILY, PURPOSE_ONE_SHOT, PURPOSE_GROWTH)

PURPOSE_LABELS: dict[str, str] = {
    PURPOSE_DAILY: "日常",
    PURPOSE_ONE_SHOT: "一次性",
    PURPOSE_GROWTH: "成长",
}

# 目的轴只给"委托形态"，不给 L 档 —— 与 L0-L5 是两个刻度
PURPOSE_DELEGATION: dict[str, str] = {
    PURPOSE_DAILY: "完全自动（自己发起、做完报一句）",
    PURPOSE_ONE_SHOT: "基本自动（直接做完，交付物给你确认一次）",
    PURPOSE_GROWTH: "辅助（只做分析/材料/催办，判断与填写留给你）",
}

# ─── 派生视图（不是状态，不入库）────────────────────────────────

VIEW_OVERDUE = "overdue"
VIEW_DUE_SOON = "due_soon"
VIEW_SCHEDULED = "scheduled"
VIEW_NEVER_SCHEDULED = "never_scheduled"
VIEW_RESIDUAL = "residual"
VIEW_REVIEW = "review"
VIEW_RUNNING = "running"
VIEW_CLOSED = "closed"
VIEW_UNCLASSIFIED = "unclassified"

CLOSED_STATUSES: frozenset[str] = frozenset(
    {"completed", "done", "archived", "cancelled", "skipped"}
)

#: 长期任务多久没动就该进复盘（天）。改这里等于改"老化"的口径。
LONG_REVIEW_AFTER_DAYS = 14

#: 一次性任务提前多少天进"本周到期"（`15` §5.2 队列第 2 档）
ONE_SHOT_DUE_SOON_DAYS = 3

#: 高频例程的频度集合 —— 只有这几档才够格被推成"日常"
_ROUTINE_FREQUENCIES = frozenset({"daily", "weekly", "monthly"})


@dataclass(slots=True)
class Classification:
    """一条事务的分类结论。

    `task_class` 为 None 表示**映射不上四类**（`unmapped=True`），
    调用方必须把它列出来给用户判，不许挑一个最近的类别塞进去。
    """

    task_class: Optional[str] = None
    purpose: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    unmapped: bool = False
    purpose_unset: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_class": self.task_class,
            "task_class_label": STRUCT_LABELS.get(self.task_class or "", None),
            "purpose": self.purpose,
            "purpose_label": PURPOSE_LABELS.get(self.purpose or "", None),
            "purpose_unset": self.purpose_unset,
            "unmapped": self.unmapped,
            "notes": list(self.notes),
        }


# ─── 分类规则 ─────────────────────────────────────────────────


def classify_todo(todo: Mapping[str, Any]) -> Classification:
    """把一条周期待办（`todos` 表的一行）判进四类。

    映射依据（`15` §5.2）：`todos` 是**循环模板**。但模板里混着事件触发
    与没有频率的残行，这两种**没有对应类别**——事件触发既无频率也无日期，
    四类里没有它的位置；不猜，直接列出来。
    """
    ttype = str(todo.get("type") or "recurring").strip()
    freq = str(todo.get("frequency") or "").strip()
    notes: list[str] = []

    if ttype == "triggered":
        return Classification(
            notes=[
                "type=triggered（事件触发）：四类的循环判据是「有频率/日程」，"
                "事件触发既无频率也无日期 ⇒ 四类里没有它的位置",
                "要不要纳管有两种改法：把事件条件换成日程（→ recurring_dated），"
                "或标注明确的一次性日期（→ one_shot_dated）；不替你选",
            ],
            unmapped=True,
        )

    if not freq:
        return Classification(
            notes=[
                f"type={ttype} 但 frequency 为空：既不能说它是循环，也拿不到日程",
                "要么补 frequency（→ recurring_dated），要么明确一次性日期（→ one_shot_dated）",
            ],
            unmapped=True,
        )

    notes.append(f"type={ttype} + frequency={freq} ⇒ 有日期循环")
    if ttype == "phased_recurring":
        notes.append(
            "阶段性循环 %s~%s，超出 end_date 转 archived"
            % (todo.get("start_date") or "?", todo.get("end_date") or "?")
        )

    cls = Classification(task_class=STRUCT_RECURRING, notes=notes)

    condition = str(todo.get("condition") or "").strip()
    if freq in _ROUTINE_FREQUENCIES and not condition:
        cls.purpose = PURPOSE_DAILY
        cls.purpose_unset = False
        cls.notes.append("周/月级例程且无附加条件 ⇒ 目的轴推定「日常」（可改）")
    else:
        reason = "quarterly 是复盘节奏" if freq == "quarterly" else f"condition={condition!r}"
        cls.notes.append(f"{reason} ⇒ 目的轴留空，等你标注（不猜）")

    return cls


def classify_wip(task: Mapping[str, Any]) -> Classification:
    """把一条 WIP 任务判进四类。

    默认映射（`15` §5.2）：未完结的 WIP = `long_undated`（要跨会话持状态）。
    能判成别的只有两个**机器可查的信号**，不靠标题猜语义：
    - 有 `due_at` ⇒ `one_shot_dated`（有明确截止的一次性）
    - `extra_data.one_session is True` ⇒ `small_undated`（写明了一个会话能做完）
    """
    status = str(task.get("status") or "").strip()
    notes: list[str] = []

    if status in CLOSED_STATUSES:
        return Classification(
            task_class=STRUCT_LONG,
            notes=[f"status={status}：已完结，只作历史，不参与队列"],
            purpose_unset=False,
        )

    cls = Classification()
    due_at = str(task.get("due_at") or "").strip()
    extra = task.get("extra_data")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
    if not isinstance(extra, dict):
        extra = {}

    purpose = extra.get("purpose")
    if isinstance(purpose, str) and purpose in PURPOSES:
        cls.purpose = purpose
        cls.purpose_unset = False
        cls.notes.append(f"extra_data.purpose={purpose}（显式标注）")

    if due_at:
        cls.task_class = STRUCT_ONE_SHOT
        cls.notes.append(f"有截止 due_at={due_at} ⇒ 一次性有日期；过期只标 overdue，不自动消失")
        return cls

    if extra.get("one_session") is True:
        cls.task_class = STRUCT_SMALL
        cls.notes.append("extra_data.one_session=true ⇒ 一个会话内可做完")
        return cls

    cls.task_class = STRUCT_LONG
    cls.notes.append("无截止、未标 one_session ⇒ 默认映射到无日期长期（15 §5.2）")
    return cls


# ─── 派生视图 ─────────────────────────────────────────────────


def as_date(value: Any) -> Optional[date]:
    """把 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS' / datetime 统一成 date。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def due_view(item: Mapping[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    """派生"现在该不该管它"。返回 {view, days, why}。

    `days`：逾期为负数（越大越旧），未到期为正数，无日期为 None。
    `view` 全部是派生值，**不写库**（`15` §5.2：overdue/due_soon 是视图不是状态）。
    """
    now = now or datetime.now()
    today = now.date()
    status = str(item.get("status") or "").strip()
    task_class = str(item.get("task_class") or "").strip()

    if status in CLOSED_STATUSES:
        return {"view": VIEW_CLOSED, "days": None, "why": f"status={status}"}

    if not task_class:
        return {"view": VIEW_UNCLASSIFIED, "days": None, "why": "未分类（映射不上的那批）"}

    if task_class == STRUCT_RECURRING:
        if item.get("condition") and str(item.get("condition_status") or "") == "inactive":
            return {"view": VIEW_SCHEDULED, "days": None, "why": "condition 已停用，不催"}
        next_due = as_date(item.get("next_due_at"))
        if next_due is None:
            if not item.get("last_done_at"):
                return {
                    "view": VIEW_NEVER_SCHEDULED,
                    "days": None,
                    "why": "从未排期（无 next_due_at / last_done_at）——现有 due 逻辑会把它当到期",
                }
            return {"view": VIEW_SCHEDULED, "days": None, "why": "已完成过、无下次日期"}
        delta = (next_due - today).days
        if delta < 0:
            return {"view": VIEW_OVERDUE, "days": delta, "why": f"下次到期 {next_due} 已过"}
        if delta <= ONE_SHOT_DUE_SOON_DAYS:
            return {"view": VIEW_DUE_SOON, "days": delta, "why": f"下次到期 {next_due}"}
        return {"view": VIEW_SCHEDULED, "days": delta, "why": f"下次到期 {next_due}"}

    if task_class == STRUCT_ONE_SHOT:
        due = as_date(item.get("due_at"))
        if due is None:
            return {"view": VIEW_UNCLASSIFIED, "days": None, "why": "判成一次性但没有 due_at"}
        delta = (due - today).days
        if delta < 0:
            return {"view": VIEW_OVERDUE, "days": delta, "why": f"截止 {due} 已过，不自动消失"}
        if delta <= ONE_SHOT_DUE_SOON_DAYS:
            return {"view": VIEW_DUE_SOON, "days": delta, "why": f"截止 {due}"}
        return {"view": VIEW_SCHEDULED, "days": delta, "why": f"截止 {due}"}

    if task_class == STRUCT_SMALL:
        touched = as_date(item.get("updated_at")) or as_date(item.get("created_at"))
        age = (today - touched).days if touched else None
        return {"view": VIEW_RESIDUAL, "days": age, "why": "无日期小任务：不进过期，只排残留"}

    # 只剩 long_undated
    review_at = as_date(item.get("next_review_at"))
    if review_at is not None:
        delta = (review_at - today).days
        if delta <= 0:
            return {"view": VIEW_REVIEW, "days": delta, "why": f"复盘点 {review_at} 已到"}
        return {"view": VIEW_RUNNING, "days": delta, "why": f"下次复盘 {review_at}"}
    touched = as_date(item.get("updated_at")) or as_date(item.get("created_at"))
    age = (today - touched).days if touched else None
    if age is not None and age >= LONG_REVIEW_AFTER_DAYS:
        return {
            "view": VIEW_REVIEW,
            "days": -age,
            "why": f"{age} 天没动（阈值 {LONG_REVIEW_AFTER_DAYS}）",
        }
    return {"view": VIEW_RUNNING, "days": age, "why": "进行中"}


_VIEW_RANK: dict[str, int] = {
    VIEW_OVERDUE: 0,
    VIEW_DUE_SOON: 1,
    VIEW_NEVER_SCHEDULED: 2,
    VIEW_RESIDUAL: 3,
    VIEW_REVIEW: 4,
    VIEW_RUNNING: 5,
    VIEW_SCHEDULED: 6,
    VIEW_UNCLASSIFIED: 7,
    VIEW_CLOSED: 9,
}

_PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def queue_level(item: Mapping[str, Any], view: Mapping[str, Any]) -> int:
    """`15` §5.2 的队列档位（1 最该现在做，6 = 展示但不排进"现在做"）。

    档位顺序是**默认顺序**，调用方可以再按用户置顶覆盖；这里只给基线。
    """
    status = str(item.get("status") or "").strip()
    if view.get("view") == VIEW_CLOSED:
        return 0  # 不排队
    if status in ("paused", "blocked"):
        return 6
    cls = str(item.get("task_class") or "")
    v = str(view.get("view") or "")
    if cls == STRUCT_ONE_SHOT and v == VIEW_OVERDUE:
        return 1
    if cls == STRUCT_ONE_SHOT and v in (VIEW_DUE_SOON, VIEW_SCHEDULED):
        return 2 if v == VIEW_DUE_SOON else 5
    if cls == STRUCT_RECURRING and v in (VIEW_OVERDUE, VIEW_DUE_SOON, VIEW_NEVER_SCHEDULED):
        return 3
    if cls == STRUCT_SMALL:
        return 4
    if v == VIEW_REVIEW:
        return 5
    return 5


def build_queue(
    items: Iterable[Mapping[str, Any]], now: Optional[datetime] = None
) -> list[dict[str, Any]]:
    """把一批事务排成"现在该做什么"的顺序。

    入参每条需带：`id` / `table` / `title` / `status` / `task_class` / `priority`
    （缺的按默认值处理）。返回值里带派生视图，**不改入参**。
    """
    now = now or datetime.now()
    rows: list[dict[str, Any]] = []
    for item in items:
        view = due_view(item, now)
        level = queue_level(item, view)
        if level == 0:
            continue
        days = view.get("days")
        rows.append(
            {
                "level": level,
                "id": item.get("id"),
                "table": item.get("table"),
                "title": item.get("title"),
                "status": item.get("status"),
                "task_class": item.get("task_class"),
                "purpose": item.get("purpose"),
                "view": view.get("view"),
                "days": days,
                "why": view.get("why"),
                "priority": item.get("priority") or "medium",
            }
        )
    rows.sort(
        key=lambda r: (
            r["level"],
            0 if r["view"] == VIEW_OVERDUE else 1,
            r["days"] if isinstance(r["days"], int) else 0,
            _PRIORITY_RANK.get(str(r["priority"]), 1),
            str(r["id"]),
        )
    )
    return rows


# ─── 汇总（给报告/接口用）─────────────────────────────────────


def summarize(items: Iterable[Mapping[str, Any]], now: Optional[datetime] = None) -> dict[str, Any]:
    """按结构轴 / 目的轴 / 派生视图出计数，并列出映射不上的条目。"""
    now = now or datetime.now()
    by_class: dict[str, int] = {k: 0 for k in STRUCT_TYPES}
    by_purpose: dict[str, int] = {k: 0 for k in PURPOSES}
    by_view: dict[str, int] = {}
    purpose_unset: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []

    for item in items:
        cls = str(item.get("task_class") or "")
        if cls in by_class:
            by_class[cls] += 1
        purpose = str(item.get("purpose") or "")
        if purpose in by_purpose:
            by_purpose[purpose] += 1
        elif str(item.get("status") or "") not in CLOSED_STATUSES:
            purpose_unset.append(
                {"id": item.get("id"), "table": item.get("table"), "title": item.get("title")}
            )
        if not cls and str(item.get("status") or "") not in CLOSED_STATUSES:
            unmapped.append(
                {
                    "id": item.get("id"),
                    "table": item.get("table"),
                    "title": item.get("title"),
                    "notes": item.get("class_notes") or [],
                }
            )
        view = str(due_view(item, now).get("view") or "")
        by_view[view] = by_view.get(view, 0) + 1

    return {
        "total": sum(by_class.values()) + len(unmapped),
        "by_task_class": by_class,
        "by_task_class_label": {STRUCT_LABELS[k]: v for k, v in by_class.items()},
        "by_purpose": by_purpose,
        "by_purpose_label": {PURPOSE_LABELS[k]: v for k, v in by_purpose.items()},
        "by_view": by_view,
        "purpose_unset": purpose_unset,
        "unmapped": unmapped,
        "delegation": PURPOSE_DELEGATION,
        "behavior": STRUCT_BEHAVIOR,
    }

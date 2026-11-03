"""待办模块 FastAPI 路由

WIP 任务拆到独立 wip_router（prefix=/wip），与周期待办分离。
todos_router 的 /due 等固定路径仍需在 /{todo_id} 动态路由之前注册。
"""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from server.todos.models import (
    TodoCreate, TodoUpdate, TodoDoneRequest,
    WipTaskCreate, WipTaskUpdate, WipBatchGetRequest,
    TaskClassUpdate, TaskActionRequest,
)
from server.todos.store import TodosStore

logger = logging.getLogger("parcae.todos.router")
router = APIRouter(prefix="/todos", tags=["todos"])
wip_router = APIRouter(prefix="/wip", tags=["wip"])
tasks_router = APIRouter(prefix="/tasks", tags=["tasks"])

_todos_store: Optional[TodosStore] = None
_todos_store_lock = threading.Lock()


def get_todos_store() -> TodosStore:
    global _todos_store
    with _todos_store_lock:
        if _todos_store is None:
            from server.memory.config import get_memory_config
            cfg = get_memory_config()
            # 相对路径相对于项目根目录解析（不依赖 cwd；router.py 在 server/todos/，3 级 parent 到项目根）
            db_path = Path(cfg.db_path)
            if not db_path.is_absolute():
                db_path = Path(__file__).resolve().parent.parent.parent / db_path
            _todos_store = TodosStore(str(db_path))
            _todos_store.initialize()
        return _todos_store


# ─── 周期待办（todos_router）──────────────────────────────

@router.get("", operation_id="todos_list", summary="列出所有待办")
async def todos_list(
    status: Optional[str] = Query(None, description="按状态过滤"),
    todo_type: Optional[str] = Query(None, description="按类型过滤（当前仅支持 recurring）"),
):
    """列出所有待办，可按状态/类型过滤。"""
    store = get_todos_store()
    todos = await asyncio.to_thread(store.list_todos, status=status, todo_type=todo_type)
    return {"todos": todos, "total": len(todos)}


@router.post("", operation_id="todos_create", summary="创建待办")
async def todos_create(req: TodoCreate):
    """创建一个新待办。"""
    store = get_todos_store()
    todo = store.create_todo(req.model_dump())
    return todo


@router.get("/due", operation_id="todos_due", summary="获取到期待办")
async def todos_due():
    """返回所有到期的周期任务（含从未完成和已过 next_due_at 的）。"""
    store = get_todos_store()
    due = await asyncio.to_thread(store.get_due_todos)
    return {"due": due, "total": len(due)}


@router.get("/{todo_id}", operation_id="todos_get", summary="获取待办详情")
async def todos_get(todo_id: str):
    store = get_todos_store()
    todo = await asyncio.to_thread(store.get_todo, todo_id)
    if not todo:
        raise HTTPException(status_code=404, detail=f"待办 '{todo_id}' 不存在")
    return todo


@router.put("/{todo_id}", operation_id="todos_update", summary="更新待办")
async def todos_update(todo_id: str, req: TodoUpdate):
    store = get_todos_store()
    updates = req.model_dump(exclude_none=True)
    todo = store.update_todo(todo_id, updates)
    if not todo:
        raise HTTPException(status_code=404, detail=f"待办 '{todo_id}' 不存在")
    return todo


@router.delete("/{todo_id}", operation_id="todos_delete", summary="删除待办")
async def todos_delete(todo_id: str):
    store = get_todos_store()
    deleted = store.delete_todo(todo_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"待办 '{todo_id}' 不存在")
    return {"status": "deleted", "id": todo_id}


@router.post("/{todo_id}/done", operation_id="todos_mark_done", summary="标记待办完成")
async def todos_mark_done(todo_id: str, req: Optional[TodoDoneRequest] = None):
    """标记待办完成（更新 last_done_at + 计算 next_due_at，status 重置为 pending）。

    对周期任务：完成本次周期，等待下次到期。
    对一次性任务：可后续手动将 status 改为 done。
    对 phased_recurring：超出 end_date 后置 archived。
    对 triggered：重置 next_due_at=NULL，等待下次事件触发。
    """
    store = get_todos_store()
    done_date = req.done_date if req else None
    todo = store.mark_done(todo_id, done_date)
    if not todo:
        raise HTTPException(status_code=404, detail=f"待办 '{todo_id}' 不存在")
    return todo


@router.post("/{todo_id}/check_trigger", operation_id="todos_check_trigger",
             summary="检查 triggered 任务触发条件")
async def todos_check_trigger(todo_id: str, payload: dict):
    """手动检查 triggered 类型任务的触发条件是否满足。

    通常由 loop 的 todos_trigger_check action 自动调用；此端点供 agent 主动触发或测试。
    payload 示例：{"event": "file_arrived", "path": "E:/test/data.csv", "filename": "data.csv"}
    """
    store = get_todos_store()
    result = store.check_trigger(todo_id, payload)
    return result


# ─── WIP 任务（wip_router，prefix=/wip）──────────────────

# 响应首字段：让 agent 先读到用法建议，再看到数据本身
_WIP_LIST_GUIDANCE = (
    "默认 summary=true，只返回轻量字段（id/title/status/priority/progress/updated_at）；"
    "其中已完结条目（completed/done/archived）进一步压成 id/title(截断)/status/updated_at + closed=true，"
    "这类条目基本无用，不该占据上下文。"
    "需要详情时不要关掉 summary 整表拉取，改为指定查：单条 wip_get(task_id='wip_xxx')，"
    "多条 wip_get_many(ids=['wip_a','wip_b',...])。"
    "注意轻量模式只含上面这些索引字段，tags / next_steps / current_state / extra_data 等富字段不含在内"
    "（要按 tags 或 extra_data 里的键匹配，先用本接口挑出候选 id 再 wip_get_many 取回）。"
    "想缩小范围可加 status=active|paused|blocked|completed。"
)
_WIP_LIST_FULL_GUIDANCE = (
    "你正在用 summary=false 全量模式，输出体积约为轻量模式的一个数量级以上。"
    "若只需个别条目的详情，下次改用 wip_get(task_id) 或 wip_get_many(ids=[...])。"
)


@wip_router.get("", operation_id="wip_list", summary="列出所有 WIP 任务")
async def wip_list(
    status: Optional[str] = Query(None, description="按状态过滤"),
    summary: bool = Query(True, description="默认 true=只返回轻量字段（已完结条目进一步压成 id/title(截断)/status/updated_at + closed=true），避免长输出污染上下文。需要详情用 wip_get(task_id) 单条取或 wip_get_many(ids) 批量取，不要为此关掉本开关"),
):
    """列出所有 WIP 任务，可按状态过滤。

    - 默认 summary=true：轻量字段，已完结条目进一步压缩（基本无用，不该占上下文）
    - summary=false：完整字段（goal/next_steps/current_state/related_*/extra_data 等），输出量约为轻量模式的 10 倍，
      仅在确实需要对全部条目做批量处理时使用；看个别详情请改用 wip_get / wip_get_many
    - 响应首字段 guidance 为用法建议，非数据
    """
    store = get_todos_store()
    tasks = await asyncio.to_thread(store.list_wip, status=status, summary=summary)
    status_counts: dict[str, int] = {}
    for t in tasks:
        key = t.get("status") or "unknown"
        status_counts[key] = status_counts.get(key, 0) + 1
    return {
        "guidance": _WIP_LIST_GUIDANCE if summary else _WIP_LIST_FULL_GUIDANCE,
        "summary": summary,
        "total": len(tasks),
        "status_counts": status_counts,
        "tasks": tasks,
    }


@wip_router.post("/batch", operation_id="wip_get_many", summary="批量获取 WIP 任务详情")
async def wip_get_many(req: WipBatchGetRequest):
    """按 id 批量获取 WIP 任务详情（保序；不存在的 id 列在 missing 里）。

    配合 wip_list(summary=true) 的轻量摘要使用：先看摘要挑出关心的几条，再一次性取详情，
    避免为了看详情把 summary 关掉整表拉取。最多 100 个 id。
    """
    store = get_todos_store()
    result = await asyncio.to_thread(store.get_wip_many, req.ids)
    return {
        "total": len(result["tasks"]),
        "missing": result["missing"],
        "tasks": result["tasks"],
    }


@wip_router.post("", operation_id="wip_create", summary="创建 WIP 任务")
async def wip_create(req: WipTaskCreate):
    """创建一个新 WIP 任务。"""
    store = get_todos_store()
    task = store.create_wip(req.model_dump())
    return task


@wip_router.get("/{task_id}", operation_id="wip_get", summary="获取 WIP 任务详情")
async def wip_get(task_id: str):
    store = get_todos_store()
    task = await asyncio.to_thread(store.get_wip, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"WIP 任务 '{task_id}' 不存在")
    return task


@wip_router.put("/{task_id}", operation_id="wip_update", summary="更新 WIP 任务")
async def wip_update(task_id: str, req: WipTaskUpdate):
    store = get_todos_store()
    updates = req.model_dump(exclude_none=True)
    task = store.update_wip(task_id, updates)
    if not task:
        raise HTTPException(status_code=404, detail=f"WIP 任务 '{task_id}' 不存在")
    return task


@wip_router.delete("/{task_id}", operation_id="wip_delete", summary="删除 WIP 任务")
async def wip_delete(task_id: str):
    store = get_todos_store()
    deleted = store.delete_wip(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"WIP 任务 '{task_id}' 不存在")
    return {"status": "deleted", "id": task_id}


# ─── 事务分类（tasks_router，prefix=/tasks）─────────────────
#
# 结构轴四类决定到期表现，目的轴三档决定交到哪一步（见 server/todos/classify.py）。
# 这些端点只读 + 改分类，不负责"派活"；派活是 DL4 之后的事。

_TASKS_QUEUE_GUIDANCE = (
    "队列按 15 §5.2 的默认顺序：1 逾期的一次性 → 2 本周到期的一次性 → "
    "3 到期的周期 → 4 无日期小任务 → 5 长期复盘/进行中 → 6 paused/blocked（展示，不排进现在做）。"
    "view/days 是派生值，不写库；paused/blocked 永远排最后。"
)
_TASKS_CLASSIFICATION_GUIDANCE = (
    "结构轴：recurring_dated / one_shot_dated / small_undated / long_undated；"
    "目的轴：daily(日常→完全自动) / one_shot(一次性→基本自动) / growth(成长→辅助)。"
    "purpose_unset 与 unmapped 是**待你判**的清单：规则判不出就留空，不猜。"
)


@tasks_router.get("/queue", operation_id="tasks_queue", summary="按默认顺序出「现在该做什么」")
async def tasks_queue():
    """队列视图。paused/blocked 排最后，已完结不排队。"""
    store = get_todos_store()
    items = await asyncio.to_thread(store.task_queue)
    return {"guidance": _TASKS_QUEUE_GUIDANCE, "total": len(items), "queue": items}


@tasks_router.get("/classification", operation_id="tasks_classification",
                  summary="事务分类报告（两轴计数 + 待判清单）")
async def tasks_classification():
    """两轴计数 + 派生视图分布 + 映射不上 / 目的轴留空的清单。"""
    store = get_todos_store()
    report = await asyncio.to_thread(store.task_summary)
    return {"guidance": _TASKS_CLASSIFICATION_GUIDANCE, **report}


@tasks_router.put("/{scope}/{task_id}/class", operation_id="tasks_set_class",
                  summary="人工改事务分类")
async def tasks_set_class(scope: str, task_id: str, req: TaskClassUpdate):
    """scope=todos|wip。只改传进来的字段；非法取值直接 400，不静默忽略。"""
    table = {"todos": "todos", "wip": "wip_tasks"}.get(scope)
    if table is None:
        raise HTTPException(status_code=400, detail=f"scope 只能是 todos / wip，收到 {scope!r}")
    store = get_todos_store()
    payload = req.model_dump(exclude_none=True)
    try:
        task = await asyncio.to_thread(
            lambda: store.set_task_class(table, task_id, **payload)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not task:
        raise HTTPException(status_code=404, detail=f"{scope} '{task_id}' 不存在")
    return task


@tasks_router.get("/list", operation_id="tasks_list",
                  summary="面板用：tasks 单表轻量列表")
async def tasks_list(include_archived: bool = Query(True)):
    store = get_todos_store()
    items = await asyncio.to_thread(store.list_tasks_view, include_archived)
    return {"items": items, "count": len(items)}


@tasks_router.post("/{scope}/{task_id}/action", operation_id="tasks_action",
                   summary="统一任务动作（pause/resume/archive/unarchive/complete）")
async def tasks_action(scope: str, task_id: str, req: TaskActionRequest):
    """T14：todos 与 wip 共用一套状态词，不再分两套 update。"""
    if scope not in ("todos", "wip"):
        raise HTTPException(status_code=400, detail=f"scope 只能是 todos / wip，收到 {scope!r}")
    store = get_todos_store()
    try:
        task = await asyncio.to_thread(
            store.apply_task_action, scope, task_id, req.action, req.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not task:
        raise HTTPException(status_code=404, detail=f"{scope} '{task_id}' 不存在")
    return {"ok": True, "action": req.action, "task": task}


@tasks_router.post("/occurrences/materialize", operation_id="tasks_materialize",
                   summary="把到期的循环模板落成 occurrence（幂等）")
async def tasks_materialize():
    """幂等：同一 (模板, 到期日) 只落一条；重复调用返回 existing。"""
    store = get_todos_store()
    return await asyncio.to_thread(store.materialize_occurrences)


@tasks_router.get("/occurrences", operation_id="tasks_occurrences", summary="列出 occurrence")
async def tasks_occurrences(status: Optional[str] = Query(None, description="pending/done/missed")):
    store = get_todos_store()
    rows = await asyncio.to_thread(store.list_occurrences, status)
    return {"total": len(rows), "occurrences": rows}


@tasks_router.post("/occurrences/{occ_id}/complete", operation_id="tasks_complete_occurrence",
                   summary="完成一次 occurrence（推进模板，更早的 pending 判 missed）")
async def tasks_complete_occurrence(
    occ_id: str,
    evidence: Optional[str] = Query(None, description="完成证据（链接/路径/一句话）"),
    advance_template: bool = Query(True, description="是否同时推进模板的 next_due_at"),
):
    store = get_todos_store()
    out = await asyncio.to_thread(store.complete_occurrence, occ_id, evidence, advance_template)
    if out is None:
        raise HTTPException(status_code=404, detail=f"occurrence '{occ_id}' 不存在")
    return out


def get_status() -> dict:
    """供 /health 聚合"""
    try:
        store = get_todos_store()
        return store.get_stats()
    except Exception as e:
        logger.warning(f"todos 状态获取失败: {e}", exc_info=True)
        return {"available": False, "error": str(e)}

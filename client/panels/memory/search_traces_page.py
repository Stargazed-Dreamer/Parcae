"""检索追踪页 — 表格 + 详情 + 清理

Ticket 08：替换 Ticket 03 占位页，接入真实功能：
- 顶部筛选条：数量 QSpinBox（1-100，默认 20）+ 查询按钮 + 重置按钮
- 中间 TableWithDetail：5 列表格（trace_id / 查询 / 时间 / 命中数 / 耗时）
  - 命中数取 final_count（最终命中数，无则 0）
  - 耗时取 latency_ms（单位 ms，无则空）
- 行点击：row_activated → 异步拉 GET /memory/search/traces/{trace_id} → 渲染详情
  - 详情含 candidates（列表 + score）+ final_results（列表 + score）+ 其他字段
- 「高级操作」折叠区：「清理超过 N 天」按钮 + QSpinBox（0-365，默认 0=走后端配置）
  - 点击弹 ConfirmDialog.confirm(parent, "清理检索追踪", f"将删除超过 {days} 天的旧 trace，不可撤销！", "warning")
  - days > 0 时调 DELETE /memory/search/traces/cleanup?days=N
  - days = 0 时调 DELETE /memory/search/traces/cleanup（走后端 retention_days 配置）

业务术语中文化：本页无业务术语（只读 + 清理），技术术语保留英文无括注
（trace_id / query / candidates / final_results / score / latency_ms / days）

unknown-outcome-evidence T15：本页多一个数据源切换「上下文投影」。
检索追踪回答"召回了什么"，投影回答"召回之后真正上了上下文的是什么、被砍了哪些、
顺序被谁改过、内容指纹是什么"。两段合在一个视图里对照看，不新建面板。
"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from client.core.http_client import HttpClient
from client.core.http_worker import HttpWorker
from client.panels.memory._shared import ActionButtonsBar, TableWithDetail
from lib.ui import tokens
from lib.ui.theme import set_kind, set_text_role


class SearchTracesPage(QWidget):
    """检索追踪页

    只读 + 清理，不支持手动写入。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._http = HttpClient.instance()
        self._workers: set = set()
        self._all_rows: list[dict] = []
        self._current_trace_id: int | None = None
        # 数据源："traces"（检索过程）| "projections"（进入上下文那一段）
        self._source = "traces"
        self._build_ui()

    # —— UI 构建 ——

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(tokens.SPACE_LG, tokens.SPACE_LG,
                                   tokens.SPACE_LG, tokens.SPACE_LG)
        layout.setSpacing(tokens.SPACE_MD)

        self._title = QLabel("检索追踪")
        set_text_role(self._title, "heading")
        layout.addWidget(self._title)

        # 筛选条
        filter_row = QHBoxLayout()
        filter_row.setSpacing(tokens.SPACE_SM)

        filter_row.addWidget(self._label("看"))
        self._source_combo = QComboBox()
        self._source_combo.addItem("检索过程（为什么被召回）", "traces")
        self._source_combo.addItem("上下文投影（召回后上了什么）", "projections")
        self._source_combo.currentIndexChanged.connect(self._on_source_changed)
        self._source_combo.setToolTip(
            "检索 trace 到「召回返回」为止；投影 trace 记的是召回之后：\n"
            "入选/丢弃/顺序被谁改过/内容指纹/有效性标记"
        )
        filter_row.addWidget(self._source_combo)

        filter_row.addWidget(self._label("数量"))
        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(1, 100)
        self._limit_spin.setValue(20)
        filter_row.addWidget(self._limit_spin)

        self._search_btn = QPushButton("查询")
        set_kind(self._search_btn, "primary")
        self._search_btn.clicked.connect(self.refresh)
        filter_row.addWidget(self._search_btn)

        self._reset_btn = QPushButton("重置")
        set_kind(self._reset_btn, "ghost")
        self._reset_btn.clicked.connect(self._on_reset)
        filter_row.addWidget(self._reset_btn)

        filter_row.addStretch()
        layout.addLayout(filter_row)

        # 表格 + 详情
        self._table = TableWithDetail()
        self._apply_source_columns()
        self._table.row_activated.connect(self._on_row_activated)
        layout.addWidget(self._table, 1)

        # 操作按钮条
        self._action_bar = ActionButtonsBar()
        # 清理超过 [spin] 天 [清理按钮] — spin + labels 必须先于按钮加
        self._action_bar.add_danger_widget(self._label("清理超过"))
        self._cleanup_days_spin = QSpinBox()
        self._cleanup_days_spin.setRange(0, 365)
        self._cleanup_days_spin.setValue(0)
        self._cleanup_days_spin.setToolTip(
            "0 = 走后端 retention_days 配置；>0 = 覆盖配置"
        )
        self._action_bar.add_danger_widget(self._cleanup_days_spin)
        self._action_bar.add_danger_widget(self._label("天"))
        self._cleanup_btn = self._action_bar.add_danger_action(
            "清理检索追踪",
            self._on_cleanup_clicked,
            confirm_title="清理检索追踪",
            confirm_message=self._build_cleanup_message,
            risk_level="warning",
        )
        layout.addWidget(self._action_bar)

        self._status_label = QLabel("等待查询")
        set_text_role(self._status_label, "tertiary")
        layout.addWidget(self._status_label)

    def _label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        set_text_role(lbl, "secondary")
        return lbl

    # —— 数据源切换（T15：检索过程 / 上下文投影） ——

    def _apply_source_columns(self) -> None:
        if self._source == "projections":
            self._table.set_columns([
                {"key": "id", "label": "proj_id", "width": 70},
                {"key": "surface", "label": "注入面", "width": 170},
                {"key": "task_type", "label": "场景", "width": 150},
                {"key": "selected_label", "label": "上屏", "width": 90},
                {"key": "ts", "label": "时间", "width": 160},
                {"key": "digest_label", "label": "内容指纹", "width": 120},
            ])
        else:
            self._table.set_columns([
                {"key": "id", "label": "trace_id", "width": 80},
                {"key": "query_trunc", "label": "查询", "width": 260},
                {"key": "ts", "label": "时间", "width": 160},
                {"key": "final_count", "label": "命中数", "width": 80},
                {"key": "latency_label", "label": "耗时", "width": 100},
            ])

    def _on_source_changed(self, _index: int) -> None:
        self._source = self._source_combo.currentData() or "traces"
        self._title.setText(
            "上下文投影（召回之后上了什么）" if self._source == "projections"
            else "检索追踪"
        )
        # 清理只针对检索 trace；投影没有清理入口（保留策略由维护器统一管）
        self._cleanup_btn.setEnabled(self._source == "traces")
        self._cleanup_days_spin.setEnabled(self._source == "traces")
        self._apply_source_columns()
        self.refresh()

    def _on_reset(self) -> None:
        self._limit_spin.setValue(20)
        self._cleanup_days_spin.setValue(0)
        self.refresh()

    def _build_cleanup_message(self) -> str:
        """根据当前 spin 值动态构建 ConfirmDialog 文案"""
        days = self._cleanup_days_spin.value()
        if days > 0:
            return f"将删除超过 {days} 天的旧 trace，不可撤销！"
        return "将删除超过后端配置天数（retention_days）的旧 trace，不可撤销！"

    # —— 公共 API ——

    def refresh(self) -> None:
        """按当前数据源拉列表：GET /memory/search/traces 或 /memory/projections"""
        params: dict[str, Any] = {"limit": self._limit_spin.value()}
        path = (
            "/memory/projections" if self._source == "projections"
            else "/memory/search/traces"
        )
        worker = HttpWorker("get", path, params=params, http_client=self._http)
        self._workers.add(worker)
        worker.finished.connect(lambda _=None, w=worker: self._workers.discard(w))
        worker.done.connect(self._on_list_done)
        worker.failed.connect(self._on_list_failed)
        worker.start()
        self._status_label.setText(
            "正在查询上下文投影 ..." if self._source == "projections"
            else "正在查询检索追踪 ..."
        )

    def apply_jump_params(self, params: dict) -> None:
        """接收 overview 跳转参数（如有 limit 覆盖）"""
        if not params:
            self.refresh()
            return
        if "limit" in params:
            try:
                self._limit_spin.setValue(int(params["limit"]))
            except (ValueError, TypeError):
                pass
        self.refresh()

    # —— 数据回调 ——

    def _on_list_done(self, data: Any) -> None:
        if not isinstance(data, dict):
            self._status_label.setText("返回数据格式异常")
            return
        if self._source == "projections":
            rows_raw = data.get("projections") or []
            if not isinstance(rows_raw, list):
                self._status_label.setText("返回数据格式异常")
                return
            self._all_rows = []
            for p in rows_raw:
                if not isinstance(p, dict):
                    continue
                selected = p.get("selected_keys")
                # 列表端点不返回 JSON 明细，用 injected_chars 粗算条数即可
                self._all_rows.append({
                    **p,
                    "task_type": p.get("task_type") or "-",
                    "selected_label": str(
                        len(selected) if isinstance(selected, list) else ""
                    ) or "-",
                    "digest_label": _truncate(str(p.get("injected_text_digest") or ""), 12),
                })
            self._status_label.setText(f"已加载 {len(self._all_rows)} 条投影记录")
            self._table.set_rows(self._all_rows)
            return

        traces = data.get("traces") or []
        if not isinstance(traces, list):
            self._status_label.setText("返回数据格式异常")
            return
        self._all_rows = []
        for t in traces:
            if not isinstance(t, dict):
                continue
            # 预计算展示字段
            t["query_trunc"] = _truncate(t.get("query", ""), 60)
            t["latency_label"] = _format_latency(t.get("latency_ms"))
            self._all_rows.append(t)
        self._status_label.setText(f"已加载 {len(self._all_rows)} 条 trace")
        self._table.set_rows(self._all_rows)

    def _on_list_failed(self, err: str) -> None:
        self._status_label.setText(f"查询失败：{err[:80]}")
        self._all_rows = []
        self._table.clear()

    def _on_row_activated(self, row: dict) -> None:
        """行点击 → 按数据源拉详情（trace 或 projection）+ 渲染"""
        record_id = row.get("id") if isinstance(row, dict) else None
        try:
            self._current_trace_id = int(record_id) if record_id is not None else None
        except (ValueError, TypeError):
            self._current_trace_id = None
        if self._current_trace_id is None:
            self._table.set_detail_markdown("")
            return
        path = (
            f"/memory/projections/{self._current_trace_id}"
            if self._source == "projections"
            else f"/memory/search/traces/{self._current_trace_id}"
        )
        worker = HttpWorker("get", path, http_client=self._http)
        self._workers.add(worker)
        worker.finished.connect(lambda _=None, w=worker: self._workers.discard(w))
        worker.done.connect(self._on_detail_done)
        worker.failed.connect(self._on_detail_failed)
        worker.start()

    def _on_detail_done(self, data: Any) -> None:
        if not isinstance(data, dict):
            self._table.set_detail_markdown("*详情数据格式异常*")
            return
        if self._source == "projections":
            self._table.set_detail_markdown(_render_projection_detail_md(data))
            return
        md = _render_trace_detail_md(data)
        self._table.set_detail_markdown(md)

    def _on_detail_failed(self, err: str) -> None:
        self._table.set_detail_markdown(f"*拉取详情失败：{err[:200]}*")

    # —— 清理操作 ——

    def _on_cleanup_clicked(self) -> None:
        """ConfirmDialog 确认后启动 DELETE /memory/search/traces/cleanup[?days=N]

        ConfirmDialog 由 add_danger_action 内部弹窗，确认后才回调到这里。
        """
        days = self._cleanup_days_spin.value()
        params: dict[str, Any] = {}
        if days > 0:
            params["days"] = days
        worker = HttpWorker(
            "delete",
            "/memory/search/traces/cleanup",
            params=params,
            http_client=self._http,
        )
        self._workers.add(worker)
        worker.finished.connect(lambda _=None, w=worker: self._workers.discard(w))
        worker.done.connect(self._on_cleanup_done)
        worker.failed.connect(self._on_cleanup_failed)
        worker.start()
        suffix = f"{days} 天" if days > 0 else "后端配置天数"
        self._status_label.setText(f"正在清理超过 {suffix} 的旧 trace ...")

    def _on_cleanup_done(self, data: Any) -> None:
        if not isinstance(data, dict):
            self._status_label.setText("清理返回数据格式异常")
            return
        result = data.get("result") or {}
        deleted = 0
        cutoff = ""
        if isinstance(result, dict):
            deleted = result.get("deleted", 0) or 0
            cutoff = result.get("cutoff", "") or ""
        self._status_label.setText(
            f"清理完成：删除 {deleted} 条 trace（截止 {cutoff}）"
        )
        # 清理后自动刷新列表
        self.refresh()

    def _on_cleanup_failed(self, err: str) -> None:
        self._status_label.setText(f"清理失败：{err[:80]}")


# —— 模块级辅助函数 ——

def _truncate(s: Any, n: int) -> str:
    """截断字符串到 n 字符"""
    if not s:
        return ""
    s_str = str(s)
    return s_str if len(s_str) <= n else s_str[:n - 1] + "…"


def _format_latency(ms: Any) -> str:
    """格式化 latency_ms 为可读字符串"""
    if ms is None or ms == "":
        return ""
    try:
        v = float(ms)
        if v < 1:
            return f"{v * 1000:.0f} μs"
        if v < 1000:
            return f"{v:.1f} ms"
        return f"{v / 1000:.2f} s"
    except (ValueError, TypeError):
        return str(ms)


def _render_projection_detail_md(detail: dict) -> str:
    """渲染单条上下文投影记录，回答"这次到底上了什么、谁改了顺序"。

    Args:
        detail: GET /memory/projections/{id} 返回的 dict
    """
    lines: list[str] = []
    proj_id = detail.get("id")
    lines.append(f"### 投影 #{proj_id}　{detail.get('ts', '')}")
    lines.append(f"- 注入面：`{detail.get('surface', '-')}`")
    lines.append(f"- 消费场景：`{detail.get('task_type') or '-'}`")
    trace_id = detail.get("trace_id")
    lines.append(
        f"- 关联检索 trace：`{trace_id if trace_id is not None else '（无：定向召回不产生检索 trace）'}`"
    )
    lines.append(
        f"- 注入文本：{detail.get('injected_chars', 0)} 字符　"
        f"digest `{'-' if not detail.get('injected_text_digest') else str(detail['injected_text_digest'])[:16]}`"
    )

    selected = detail.get("selected_keys") or []
    dropped = detail.get("dropped_keys") or []
    rank_before = detail.get("rank_before") or []
    rank_after = detail.get("rank_after") or []
    flags = detail.get("validation_flags") or {}

    lines.append("\n**上屏顺序（这就是模型实际看到的顺序）**")
    if isinstance(selected, list) and selected:
        for i, key in enumerate(selected, 1):
            doubt = flags.get(str(key)) if isinstance(flags, dict) else None
            lines.append(f"{i}. `{key}`" + (f"　⚠ {doubt}" if doubt else ""))
    else:
        lines.append("_（无）_")

    if isinstance(dropped, list) and dropped:
        lines.append("\n**被上限砍掉（召回到了但没上屏）**")
        for key in dropped:
            lines.append(f"- `{key}`")

    if isinstance(rank_before, list) and rank_before:
        same = [str(k) for k in rank_before[: len(selected)]] == [str(k) for k in selected]
        lines.append("\n**上游召回顺序**")
        lines.append("_与上屏顺序一致（下游没有重排）_" if same else "_与上屏顺序不同：注入面按业务档位重排过_")
        for i, key in enumerate(rank_before, 1):
            lines.append(f"{i}. `{key}`")

    if isinstance(rank_after, list) and rank_after and rank_after != selected:
        lines.append("\n**投影时记录的上屏顺序**")
        for i, key in enumerate(rank_after, 1):
            lines.append(f"{i}. `{key}`")

    if isinstance(flags, dict) and flags:
        lines.append("\n**有效性存疑项**")
        for key, reason in flags.items():
            lines.append(f"- `{key}`：{reason}")

    note = detail.get("note")
    if note:
        lines.append(f"\n_备注：{note}_")
    return "\n".join(lines)


def _render_trace_detail_md(detail: dict) -> str:
    """渲染单条 trace 详情为 markdown

    Args:
        detail: GET /memory/search/traces/{id} 返回的完整 dict
            含 id/ts/query/params/mode/latency_ms/bm25_count/vector_count/
            final_count/error/candidates/final_results 等字段

    Returns:
        markdown 字符串
    """
    lines: list[str] = []
    trace_id = detail.get("id", "")
    lines.append(f"# Trace #{trace_id}")
    lines.append("")

    # —— 元数据表 ——
    lines.append("## 元数据")
    lines.append("")
    lines.append("| 字段 | 值 |")
    lines.append("|---|---|")
    for k in ("ts", "query", "mode", "latency_ms", "bm25_count",
              "vector_count", "final_count", "error"):
        v = detail.get(k)
        if v is None or v == "":
            continue
        v_str = str(v).replace("|", "\\|")
        if len(v_str) > 100:
            v_str = v_str[:97] + "..."
        lines.append(f"| {k} | {v_str} |")
    lines.append("")

    # —— params ——
    params = detail.get("params")
    if params is not None:
        lines.append("## params")
        lines.append("")
        lines.append("```json")
        try:
            lines.append(json.dumps(params, ensure_ascii=False, indent=2))
        except (TypeError, ValueError):
            lines.append(str(params))
        lines.append("```")
        lines.append("")

    # —— candidates ——
    candidates = detail.get("candidates")
    if isinstance(candidates, list) and candidates:
        lines.append(f"## candidates（{len(candidates)} 条）")
        lines.append("")
        lines.append(_render_scored_list(candidates, "candidates"))
        lines.append("")

    # —— final_results ——
    final_results = detail.get("final_results")
    if isinstance(final_results, list) and final_results:
        lines.append(f"## final_results（{len(final_results)} 条）")
        lines.append("")
        lines.append(_render_scored_list(final_results, "final_results"))
        lines.append("")

    return "\n".join(lines)


def _render_scored_list(items: list, label: str) -> str:
    """渲染候选/最终结果列表（含 score 字段）

    每条用 ```json 代码块包裹，按 score 降序取前 10 条（避免详情过长）。
    """
    lines: list[str] = []
    # 取前 10 条避免详情区爆炸
    top = items[:10]
    for i, item in enumerate(top):
        if not isinstance(item, dict):
            lines.append(f"**{label}[{i}]**")
            lines.append("")
            lines.append("```")
            lines.append(str(item))
            lines.append("```")
            lines.append("")
            continue
        score = item.get("score")
        score_str = f"score={score}" if score is not None else "no score"
        lines.append(f"**{label}[{i}]** ({score_str})")
        lines.append("")
        lines.append("```json")
        try:
            lines.append(json.dumps(item, ensure_ascii=False, indent=2))
        except (TypeError, ValueError):
            lines.append(str(item))
        lines.append("```")
        lines.append("")
    if len(items) > 10:
        lines.append(f"*... 还有 {len(items) - 10} 条未展示*")
    return "\n".join(lines)

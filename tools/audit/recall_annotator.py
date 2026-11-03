"""记忆召回判卷标注器（A 通道：task_type → 必读块）。

出题单位是 task_type，候选池取自 `context_projections.rank_before` 的并集——
**包含那些从没上过屏的候选**。只看已上屏的 5 条打勾，考的是"能不能挑出系统已经
挑好的"，判卷人会被系统自己的答案锚定，所以这里把整池摊开盲评。

盲评口径（与 docs/memory-system.md「判卷纪律」同源）：
- 不显示分数、不显示 `matched_by`
- 不显示"系统当前上了屏哪几条"
- 顺序按 `(task_type, key)` 的 sha256 打乱：跨运行稳定，中断后能接着标

三档（键 1/2/3）：
- 必须：缺了它 agent 会做错事、重复踩坑、或丢掉用户已说过的偏好
- 有用：不影响对错，但省一次搜索/一次返工
- 无关：这个 task_type 不该为它腾上下文

**未标 ≠ 无关**：只有被点过名的条目进入判卷集，剩下的按"不主张相关"处理。
这样一趟只要挑出该读的那十来条，不用把 555 条逐个打勾；要算噪声率时再补标无关。
`N` 直接进下一池，`U` 撤销当前条（写一行 `unset`，仍以最后一行为准）。

落盘是 **JSONL 追加**，同一 (task_type, key) 以最后一行为准——重标留痕不覆盖历史，
免得手滑把一批标反了还查不回来。

用法:
    uv run python tools/audit/recall_annotator.py                 # 标注界面
    uv run python tools/audit/recall_annotator.py --stats         # 只看进度
    uv run python tools/audit/recall_annotator.py --min-seen 20   # 只对注入≥20次的出题
输出: tests/memory_eval/gold.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLD_PATH = ROOT / "tests" / "memory_eval" / "gold.jsonl"
LABELS = (("1", "must", "必须"), ("2", "useful", "有用"), ("3", "noise", "无关"))
_CODE_OF = {code: name for code, name, _ in LABELS}
_LABEL_OF = {name: lbl for _, name, lbl in LABELS}

# 标题字段优先级：KV 记忆的摘要在前，旧写入只有 content 的退到正文首行
_TITLE_FIELDS = ("summary", "name", "title", "description", "goal", "content")
# 没有专用标题字段的记忆（如 {diagnose,fix,gotcha}）按语义优先级退到描述性字段；
# 最后才退到任意字符串值——别把 PowerShell 命令当标题（reference_hyperv_launchtype_gotcha）。
_TITLE_FALLBACK_FIELDS = (
    "decision", "conclusion", "lesson", "why", "gotcha", "symptom",
    "problem", "rule", "preference", "note", "fix", "howto",
)
_MIN_POOL = 5  # 候选池小于这个数的 task_type 没有判卷意义
_DATE_LIKE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T].*)?$")


def _text_head(v: str) -> tuple[str, str] | None:
    lines = [x.strip() for x in v.strip().splitlines() if x.strip()]
    if not lines:
        return None
    head = lines[0].lstrip("#").strip()
    if len(head) < 2 or _DATE_LIKE.match(head):
        return None
    return head[:90], " ".join(lines[1:])[:220]


def resolve_db_path() -> Path:
    from server.memory.config import get_memory_config

    p = Path(get_memory_config().db_path)
    return p if p.is_absolute() else ROOT / p


def _title_of(raw: str) -> tuple[str, str]:
    """返回 (标题, 副行)。只取文本字段，不打印裸 JSON（docs/ui/patterns.md §8）。"""
    try:
        d = json.loads(raw)
    except Exception:
        got = _text_head((raw or "").strip())
        return got if got else ("(空记忆)", "")
    if not isinstance(d, dict):
        return str(d)[:90], ""
    for f in (*_TITLE_FIELDS, *_TITLE_FALLBACK_FIELDS):
        v = d.get(f)
        if isinstance(v, str) and v.strip():
            got = _text_head(v)
            if got:
                return got
    for v in d.values():
        if isinstance(v, str) and v.strip():
            got = _text_head(v)
            if got:
                return got
    return "(无标题)", "字段: " + " / ".join(list(d)[:8])


def build_questions(db: Path, min_seen: int) -> list[dict]:
    """按注入次数挑 task_type，每个出一池盲序候选。"""
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    per: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for task_type, rank_before in cur.execute(
        "SELECT task_type, rank_before FROM context_projections WHERE task_type IS NOT NULL"
    ):
        try:
            pool = json.loads(rank_before or "[]")
        except Exception:
            pool = []
        counts[task_type] = counts.get(task_type, 0) + 1
        per.setdefault(task_type, set()).update(str(k) for k in pool)

    titles = {k: _title_of(v) for k, v in cur.execute("SELECT key, value FROM facts")}
    conn.close()

    out: list[dict] = []
    for task_type, seen in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        if seen < min_seen:
            continue
        cand = sorted((k for k in per.get(task_type, ()) if k in titles),
                      key=lambda k: hashlib.sha256(f"{task_type}|{k}".encode()).hexdigest())
        if len(cand) < _MIN_POOL:
            continue
        out.append({
            "task_type": task_type,
            "injections": seen,
            "items": [{"key": k, "title": titles[k][0], "sub": titles[k][1]} for k in cand],
        })
    return out


def load_existing(path: Path) -> dict[tuple[str, str], str]:
    """同一 (task_type, key) 以最后一行为准；`unset`（撤销）从字典里删掉。"""
    done: dict[tuple[str, str], str] = {}
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            k = (str(r["task_type"]), str(r["key"]))
            label = str(r["label"])
            if label == "unset":
                done.pop(k, None)
            else:
                done[k] = label
        except Exception:
            continue  # 上次崩溃留下的半行：忽略，不影响其余
    return done


def append_label(path: Path, task_type: str, key: str, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"task_type": task_type, "key": key, "label": label,
           "ts": datetime.now().isoformat(timespec="seconds")}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _tally(qs: list[dict], done: dict[tuple[str, str], str]) -> tuple[int, int, dict[str, int]]:
    total = sum(len(q["items"]) for q in qs)
    marked = sum(1 for q in qs for it in q["items"] if (q["task_type"], it["key"]) in done)
    counts = {name: sum(1 for v in done.values() if v == name) for _, name, _ in LABELS}
    return total, marked, counts


def print_stats(db: Path, gold: Path, min_seen: int) -> int:
    qs = build_questions(db, min_seen)
    done = load_existing(gold)
    total, marked, counts = _tally(qs, done)
    if not qs:
        print(f"没有可出题的 task_type（阈值 --min-seen={min_seen}，池下限 {_MIN_POOL}）")
        return 0
    print(f"出题 task_type {len(qs)} 种 ｜候选条次 {total} ｜已标 {marked}（{marked / total:.1%}）")
    print("档位累计:", "  ".join(f"{_LABEL_OF[n]} {c}" for n, c in counts.items()))
    print(f"判卷集: {gold.relative_to(ROOT) if gold.is_absolute() else gold}")
    for q in qs:
        n = sum(1 for it in q["items"] if (q["task_type"], it["key"]) in done)
        print(f"  {q['task_type']:30s} 池 {len(q['items']):3d}  注入 {q['injections']:4d}  "
              f"已标 {n:3d}")
    return 0


def run_gui(db: Path, gold: Path, min_seen: int) -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import (
        QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
        QPushButton, QSizePolicy, QSplitter, QVBoxLayout, QWidget,
    )

    from lib.ui import EmptyState, apply_theme
    from lib.ui.theme import set_kind, set_text_role

    questions = build_questions(db, min_seen)
    done = load_existing(gold)

    app = QApplication(sys.argv)
    apply_theme(app)

    if not questions:
        w = EmptyState("inbox", "没有可出题的 task_type",
                       f"候选池不足 {_MIN_POOL} 条，或都低于 --min-seen={min_seen}")
        w.setWindowTitle("记忆召回判卷标注（A 通道）")
        w.show()
        return int(app.exec())

    class Annotator(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("记忆召回判卷标注（A 通道）")
            self.qi = 0
            self.ii = 0
            self._build()
            self._show_item()

        def _sc(self, key: str, handler) -> None:
            """PySide6 的 QShortcut 构造不接受 activated 关键字，先建再 connect。"""
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(handler)

        # ---------- 骨架 ----------
        def _build(self) -> None:
            self.task_list = QListWidget()
            self.task_items: list[QListWidgetItem] = []
            for q in questions:
                it = QListWidgetItem()
                self.task_list.addItem(it)
                self.task_items.append(it)
            self.task_list.currentRowChanged.connect(self._on_row)

            self.counter = QLabel()
            set_text_role(self.counter, "caption")

            self.item_title = QLabel()
            self.item_title.setWordWrap(True)
            self.item_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            set_text_role(self.item_title, "title")

            self.item_key = QLabel()
            self.item_key.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            set_text_role(self.item_key, "mono")

            self.item_sub = QLabel()
            self.item_sub.setWordWrap(True)
            self.item_sub.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            set_text_role(self.item_sub, "secondary")

            self.buttons: dict[str, QPushButton] = {}
            row = QHBoxLayout()
            for code, name, label in LABELS:
                b = QPushButton(f"{label}  [{code}]")
                b.setToolTip(f"标为「{label}」并前进到下一条（快捷键 {code}）")
                b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                b.clicked.connect(lambda _=False, c=code: self._mark(c))
                row.addWidget(b)
                self.buttons[code] = b
                self._sc(code, lambda c=code: self._mark(c))

            nav = QHBoxLayout()
            for text, tip, handler in (
                ("撤销本条  [U]", "清除这一条的标注（写一行 unset，仍以最后一行为准）",
                 lambda: self._mark("U")),
                ("下一池  [N]", "本池剩下的不再看，直接进下一个 task_type", self._next_pool),
            ):
                b = QPushButton(text)
                b.setToolTip(tip)
                b.clicked.connect(handler)
                set_kind(b, "ghost")
                nav.addWidget(b)
            nav.addStretch()
            self.hint = QLabel("没点过的条目按「不主张相关」处理，不算 gold 也不算噪声；"
                               "要算噪声率再回头补标「无关」")
            set_text_role(self.hint, "tertiary")
            self.hint.setWordWrap(True)

            card = QWidget()
            v = QVBoxLayout(card)
            v.addWidget(self.counter)
            v.addWidget(self.item_title)
            v.addWidget(self.item_key)
            v.addWidget(self.item_sub)
            v.addSpacing(8)
            v.addLayout(row)
            v.addLayout(nav)
            v.addWidget(self.hint)
            v.addStretch()

            split = QSplitter()
            split.addWidget(self.task_list)
            split.addWidget(card)
            split.setSizes([300, 700])
            split.setStretchFactor(1, 1)
            self.setCentralWidget(split)
            self.setMinimumSize(900, 480)

            self._sc("Right", self._next)
            self._sc("Left", self._prev)
            self._sc("Ctrl+Down", self._next_task)
            self._sc("N", self._next_pool)
            self._sc("U", lambda: self._mark("U"))
            self.statusBar().showMessage(
                "1/2/3 打标并前进 ｜ U 撤销本条 ｜ N 换下一池 ｜ ←/→ 翻条 ｜ "
                "未标＝不主张相关（不进判卷集）")

        # ---------- 渲染 ----------
        def _refresh_task_rows(self) -> None:
            for i, q in enumerate(questions):
                n = sum(1 for it in q["items"] if (q["task_type"], it["key"]) in done)
                self.task_items[i].setText(f"{q['task_type']}    {n}/{len(q['items'])}")
                self.task_items[i].setToolTip(
                    f"注入 {q['injections']} 次 ｜ 候选池 {len(q['items'])} 条 ｜ 已标 {n}")

        def _show_item(self) -> None:
            self._refresh_task_rows()
            q = questions[self.qi]
            self.task_list.blockSignals(True)
            self.task_list.setCurrentRow(self.qi)
            self.task_list.blockSignals(False)

            n = len(q["items"])
            self.ii = max(0, min(self.ii, n - 1))
            item = q["items"][self.ii]
            marked = sum(1 for it in q["items"] if (q["task_type"], it["key"]) in done)
            self.counter.setText(f"{q['task_type']} ｜第 {self.ii + 1}/{n} 条｜本池已标 {marked}")
            self.item_title.setText(item["title"])
            self.item_key.setText(item["key"])
            self.item_sub.setText(item["sub"] or "（没有续句）")

            prev = done.get((q["task_type"], item["key"]))
            for code, name, label in LABELS:
                b = self.buttons[code]
                b.setText(("● " if name == prev else "") + f"{label}  [{code}]")
                set_kind(b, "primary" if name == prev else "ghost")

        # ---------- 动作 ----------
        def _mark(self, code: str) -> None:
            q = questions[self.qi]
            item = q["items"][self.ii]
            if code == "U":
                name = "unset"
                done.pop((q["task_type"], item["key"]), None)
            else:
                name = _CODE_OF[code]
                done[(q["task_type"], item["key"])] = name
            append_label(gold, q["task_type"], item["key"], name)
            self._next()

        def _next(self) -> None:
            q = questions[self.qi]
            if self.ii + 1 < len(q["items"]):
                self.ii += 1
            elif self.qi + 1 < len(questions):
                self.qi += 1
                self.ii = 0
            else:
                self.statusBar().showMessage("已到最后一池最后一条——可以跑 --stats 看总进度")
                return
            self._show_item()

        def _next_pool(self) -> None:
            if self.qi + 1 < len(questions):
                self.qi += 1
                self.ii = 0
                self._show_item()
            else:
                self.statusBar().showMessage("已是最后一池")

        def _prev(self) -> None:
            if self.ii > 0:
                self.ii -= 1
            elif self.qi > 0:
                self.qi -= 1
                self.ii = len(questions[self.qi]["items"]) - 1
            self._show_item()

        def _next_task(self) -> None:
            if self.qi + 1 < len(questions):
                self.qi += 1
                self.ii = 0
                self._show_item()

        def _on_row(self, row: int) -> None:
            if row >= 0 and row != self.qi:
                self.qi = row
                self.ii = 0
                self._show_item()

    win = Annotator()
    win.show()
    return int(app.exec())


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="记忆召回判卷标注器（A 通道）")
    ap.add_argument("--stats", action="store_true", help="只打印进度，不开界面")
    ap.add_argument("--min-seen", type=int, default=10,
                    help="只对注入次数达标的 task_type 出题（默认 10）")
    ap.add_argument("--out", default=str(GOLD_PATH), help="判卷集 JSONL 路径")
    args = ap.parse_args(argv)

    db = resolve_db_path()
    if not db.exists():
        print(f"[红] 记忆库不存在：{db}")
        return 1
    gold = Path(args.out)
    if args.stats:
        return print_stats(db, gold, args.min_seen)
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        print("[提示] offscreen 渲染自检模式，不要在这里打标")
    return run_gui(db, gold, args.min_seen)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

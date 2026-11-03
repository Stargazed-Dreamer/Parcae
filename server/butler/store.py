"""DL4 会话码与 run/handoff 存储。

表在 ``server/memory/schema.py`` v9 里创建，与 todos/wip/facts 共用
``data/memory/memory.db``。这里只做 SQL，不碰记忆/任务业务。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from server.memory.store import MemoryStore

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉 I/O/0/1
_CODE_LENGTH = 4  # 2026-10-27 用户裁定：4 位数字字母混合够用，不够再加位
_CODE_TTL_DAYS = 30  # 只约束「创建后一直没开跑」；已 opened 的 run 不按时间过期


class ButlerError(Exception):
    """带可返回给调用方的错误码。"""

    def __init__(self, code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


def _json_or_empty(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _new_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


class ButlerStore:
    """会话/run/handoff/投递/事件的存储层（复用 MemoryStore 的连接与写锁）。"""

    def __init__(self, memory_store: MemoryStore):
        self.memory_store = memory_store
        self.conn = memory_store.conn
        self._lock = memory_store._write_lock

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        # 读也串行化：单 sqlite 连接上并发的 execute 与 commit 会互相重置游标，
        # 实测 4 线程并发 next_context 时 fetchone 偶发返回 None（误报 code_not_found）。
        with self._lock:
            cur = self.conn.execute(sql, params)
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        if row is None:
            return None
        return dict(zip(cols, row, strict=False))

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return [dict(zip(cols, r, strict=False)) for r in rows]

    # ── run ──────────────────────────────────────────────

    def create_run(
        self,
        prompt: str,
        *,
        task_scope: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
        harness: str | None = None,
        owner: str | None = None,
        handoff_id: str | None = None,
        domain_id: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        code = _new_code()
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        with self._lock:
            self.conn.execute(
                """INSERT INTO butler_runs
                   (run_id, code_hash, code_prefix, status, task_scope, task_id,
                    project_id, harness, owner, prompt, handoff_id, domain_id, label)
                   VALUES (?, ?, ?, 'created', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, _hash_code(code), code[:7], task_scope, task_id,
                 project_id, harness, owner, prompt, handoff_id, domain_id,
                 (label or "").strip()[:40] or None),
            )
            self.conn.commit()
        self.append_event(run_id, "run.created",
                          {"task_scope": task_scope, "task_id": task_id,
                           "label": (label or "").strip()[:40] or None})
        return {"run_id": run_id, "session_code": code, "status": "created"}

    # ---- 执行身份（harness / model）-------------------------------------

    def set_run_identity(self, run_id: str, *, harness: str | None = None,
                         model: str | None = None) -> None:
        """agent 自报的执行身份：只在有值时才写，不会把已知值覆盖成空。"""
        sets: list[str] = []
        params: list[Any] = []
        if harness:
            sets.append("harness = ?")
            params.append(harness)
        if model:
            sets.append("model = ?")
            params.append(model)
        if not sets:
            return
        params.append(run_id)
        with self._lock:
            self.conn.execute(
                f"UPDATE butler_runs SET {', '.join(sets)} WHERE run_id = ?",
                tuple(params),
            )
            self.conn.commit()

    # ---- 执行域状态（v14）-----------------------------------------------
    #
    # 2026-10-29 用户拍板：进度 / 下一步 / 现状是「这一路」的，写在 run 上；
    # 事务行的同名字段归用户手动。多路并发时各写各的行，天然无冲突。

    def set_run_state(
        self,
        run_id: str,
        *,
        label: str | None = None,
        progress: int | None = None,
        next_steps: Any = None,
        state_note: Any = None,
    ) -> None:
        """写本路状态；None = 这一项不动（不是清空）。

        进度允许上可下（用户纠偏），超出 0-100 直接夹住而不是报错——
        它是给人看的估计值，不是账。
        """
        sets: list[str] = []
        params: list[Any] = []
        if label is not None:
            sets.append("label = ?")
            params.append((label or "").strip()[:40] or None)
        if progress is not None:
            value = int(progress)
            sets.append("progress = ?")
            params.append(max(0, min(100, value)))
        if next_steps is not None:
            steps = next_steps if isinstance(next_steps, list) else [next_steps]
            sets.append("next_steps_json = ?")
            params.append(json.dumps([str(s)[:200] for s in steps][:10],
                                     ensure_ascii=False))
        if state_note is not None:
            note = state_note
            if isinstance(note, dict):  # current_state 历史上是对象
                note = (
                    note.get("summary") or note.get("note") or note.get("state")
                    or json.dumps(note, ensure_ascii=False)
                )
            sets.append("state_note = ?")
            params.append(str(note).strip()[:500] or None)
        if not sets:
            return
        params.append(run_id)
        with self._lock:
            self.conn.execute(
                f"UPDATE butler_runs SET {', '.join(sets)} WHERE run_id = ?",
                tuple(params),
            )
            self.conn.commit()

    # ---- 派单提示词版本（v13：多版本 + 回滚）-----------------------------
    #
    # 2026-10-29 用户裁定：「派单的有历史可回滚；交接的不需要」。
    # 所以这里只管派单提示词，交接文本永远是实时渲染的单份，不进版本表。
    #
    # 四个动作都允许，作用对象是任意版本：
    #   看 = GET 清单/正文；改 = 直接改写该版正文；删 = 删除该版；回滚 = 设为当前。
    # 当前生效版由 butler_runs.prompt_version 指向，正文同时抄进 prompt_text，
    # 老读侧（复制、重发）零改动。
    # 保留上限 10 版（10-28 用户原话），超出删最旧，且不删当前生效的那一版。

    PROMPT_VERSION_LIMIT = 10

    def _prompt_versions(self, run_id: str) -> list[dict[str, Any]]:
        return self._all(
            """SELECT version, chars, source, note, created_at
               FROM butler_prompt_versions WHERE run_id = ?
               ORDER BY version""",
            (run_id,),
        )

    def _ensure_baseline(self, run: dict[str, Any]) -> int | None:
        """老 run 没有版本行时，用现存 prompt_text 补 v1；返回当前生效版本号。"""
        run_id = run["run_id"]
        versions = self._prompt_versions(run_id)
        if not versions:
            text = (run.get("prompt_text") or "").strip()
            if not text:
                return None
            with self._lock:
                self.conn.execute(
                    """INSERT OR IGNORE INTO butler_prompt_versions
                       (run_id, version, prompt_text, chars, source, note)
                       VALUES (?, 1, ?, ?, ?, ?)""",
                    (run_id, run["prompt_text"], len(run["prompt_text"]),
                     run.get("prompt_source") or "initial",
                     "版本表启用前的 run，按现存正文回填"),
                )
                self.conn.execute(
                    "UPDATE butler_runs SET prompt_version = 1 "
                    "WHERE run_id = ? AND prompt_version IS NULL",
                    (run_id,),
                )
                self.conn.commit()
            return 1
        current = run.get("prompt_version")
        if not current:
            current = versions[-1]["version"]
            with self._lock:
                self.conn.execute(
                    "UPDATE butler_runs SET prompt_version = ? WHERE run_id = ?",
                    (current, run_id),
                )
                self.conn.commit()
        return int(current)

    def list_prompt_versions(self, run_id: str) -> dict[str, Any]:
        """版本清单（不含正文，避免一次拖回几十 KB）。"""
        run = self.get_run(run_id)
        current = self._ensure_baseline(run)
        versions = self._prompt_versions(run_id)
        for v in versions:
            v["is_current"] = v["version"] == current
        return {
            "run_id": run_id,
            "current": current,
            "latest": versions[-1]["version"] if versions else None,
            "limit": self.PROMPT_VERSION_LIMIT,
            "items": versions,
        }

    def get_prompt_version(self, run_id: str, version: int) -> dict[str, Any]:
        run = self.get_run(run_id)
        current = self._ensure_baseline(run)
        row = self._one(
            """SELECT run_id, version, prompt_text, chars, source, note, created_at
               FROM butler_prompt_versions WHERE run_id = ? AND version = ?""",
            (run_id, version),
        )
        if row is None:
            raise ButlerError("prompt_version_not_found", "该版本不存在", 404)
        data = dict(row)
        data["is_current"] = data["version"] == current
        return data

    def append_prompt_version(
        self,
        run_id: str,
        prompt_text: str,
        *,
        source: str,
        note: str | None = None,
        activate: bool = True,
    ) -> dict[str, Any]:
        """追加一个版本；默认同时设为当前生效版。"""
        text = prompt_text or ""
        if not text.strip():
            raise ButlerError("prompt_empty", "提示词不能为空")
        run = self.get_run(run_id)
        self._ensure_baseline(run)
        with self._lock:
            row = self.conn.execute(
                "SELECT MAX(version) FROM butler_prompt_versions WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            version = int(row[0] or 0) + 1
            self.conn.execute(
                """INSERT INTO butler_prompt_versions
                   (run_id, version, prompt_text, chars, source, note)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, version, text, len(text), source, note),
            )
            if activate:
                self.conn.execute(
                    """UPDATE butler_runs
                       SET prompt_text = ?, prompt_version = ?, prompt_source = ?
                       WHERE run_id = ?""",
                    (text, version, source, run_id),
                )
            # 只留最近 N 版；当前生效的那一版永不删（回滚后它可能不是最新的）
            keep_from = version - self.PROMPT_VERSION_LIMIT + 1
            if keep_from > 1:
                self.conn.execute(
                    """DELETE FROM butler_prompt_versions
                       WHERE run_id = ? AND version < ?
                         AND version <> COALESCE(
                               (SELECT prompt_version FROM butler_runs WHERE run_id = ?), -1)""",
                    (run_id, keep_from, run_id),
                )
            self.conn.commit()
        self.append_event(run_id, "prompt.versioned",
                          {"version": version, "source": source,
                           "chars": len(text), "activate": activate})
        return {"version": version, "chars": len(text), "source": source,
                "is_current": activate}

    def update_prompt_version(self, run_id: str, version: int,
                              prompt_text: str,
                              *, source: str | None = None) -> dict[str, Any]:
        """改写某一版正文（用户要求「别的版本也可以改」）。

        如果改的正是当前生效版，同步 run.prompt_text，避免两处不一致；
        传了 ``source`` 就一并更新来源标记（面板手改记 user_edit）。
        """
        text = prompt_text or ""
        if not text.strip():
            raise ButlerError("prompt_empty", "提示词不能为空")
        run = self.get_run(run_id)
        current = self._ensure_baseline(run)
        with self._lock:
            cur = self.conn.execute(
                """UPDATE butler_prompt_versions SET prompt_text = ?, chars = ?
                   WHERE run_id = ? AND version = ?""",
                (text, len(text), run_id, version),
            )
            if cur.rowcount == 0:
                raise ButlerError("prompt_version_not_found", "该版本不存在", 404)
            if current == version:
                if source:
                    self.conn.execute(
                        "UPDATE butler_runs SET prompt_text = ?, prompt_source = ? "
                        "WHERE run_id = ?",
                        (text, source, run_id),
                    )
                else:
                    self.conn.execute(
                        "UPDATE butler_runs SET prompt_text = ? WHERE run_id = ?",
                        (text, run_id),
                    )
            self.conn.commit()
        self.append_event(run_id, "prompt.edited",
                          {"version": version, "chars": len(text),
                           "is_current": current == version})
        return {"version": version, "chars": len(text),
                "is_current": current == version}

    def activate_prompt_version(self, run_id: str, version: int) -> dict[str, Any]:
        """回滚 / 切换：把某一版设为当前生效版（正文抄进 prompt_text）。"""
        src = self.get_prompt_version(run_id, version)
        with self._lock:
            self.conn.execute(
                """UPDATE butler_runs
                   SET prompt_text = ?, prompt_version = ?, prompt_source = 'rollback'
                   WHERE run_id = ?""",
                (src["prompt_text"], version, run_id),
            )
            self.conn.commit()
        self.append_event(run_id, "prompt.activated",
                          {"version": version, "chars": src["chars"]})
        return {"version": version, "chars": src["chars"], "is_current": True}

    def delete_prompt_version(self, run_id: str, version: int) -> dict[str, Any]:
        """删任意一版；删掉的若是当前版，自动切到剩下的最大版本。最后一版不可删。"""
        run = self.get_run(run_id)
        current = self._ensure_baseline(run)
        versions = self._prompt_versions(run_id)
        if not any(v["version"] == version for v in versions):
            raise ButlerError("prompt_version_not_found", "该版本不存在", 404)
        if len(versions) <= 1:
            raise ButlerError(
                "prompt_version_last", "只剩一个版本，删了就没有提示词了", 409,
            )
        with self._lock:
            self.conn.execute(
                "DELETE FROM butler_prompt_versions WHERE run_id = ? AND version = ?",
                (run_id, version),
            )
            new_current = current
            if current == version:
                row = self.conn.execute(
                    "SELECT version, prompt_text FROM butler_prompt_versions "
                    "WHERE run_id = ? ORDER BY version DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                new_current = int(row[0])
                self.conn.execute(
                    """UPDATE butler_runs
                       SET prompt_text = ?, prompt_version = ?, prompt_source = 'rollback'
                       WHERE run_id = ?""",
                    (row[1], new_current, run_id),
                )
            self.conn.commit()
        self.append_event(run_id, "prompt.version_deleted",
                          {"version": version, "current": new_current})
        return {"ok": True, "version": version, "current": new_current}

    def set_run_prompt_text(self, run_id: str, prompt_text: str, *,
                            source: str = "user_edit") -> dict[str, Any]:
        """改「当前生效版」的正文：面板上直接编辑提示词后保存走这里。

        与 append 的区别：不新增版本，只改当前这一版。
        """
        run = self.get_run(run_id)
        current = self._ensure_baseline(run)
        if current is None:
            return self.append_prompt_version(run_id, prompt_text, source=source)
        return self.update_prompt_version(run_id, current, prompt_text, source=source)


    def get_run_by_code(self, code: str) -> dict[str, Any]:
        run = self._one(
            "SELECT * FROM butler_runs WHERE code_hash = ?", (_hash_code(code),)
        )
        if not run:
            raise ButlerError("code_not_found", "会话码不存在", 404)
        return run

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._one("SELECT * FROM butler_runs WHERE run_id = ?", (run_id,))
        if not run:
            raise ButlerError("run_not_found", "run 不存在", 404)
        return run

    def list_runs(
        self,
        *,
        statuses: list[str] | None = None,
        task_scope: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
        archived: bool | None = None,
    ) -> list[dict[str, Any]]:
        """面板用列表；不返回 code_hash（会话码明文只在创建响应里出现一次）。

        ``archived``：None = 不过滤（老调用方默认）；False = 只要还在面板上的；
        True = 只要归档视图。找 takeover_run、算自动完成都得看得见归档的，
        所以默认不过滤。
        """
        where: list[str] = []
        params: list[Any] = []
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        if task_scope:
            where.append("task_scope = ?")
            params.append(task_scope)
        if task_id:
            where.append("task_id = ?")
            params.append(task_id)
        if archived is True:
            where.append("archived_at IS NOT NULL")
        elif archived is False:
            where.append("archived_at IS NULL")
        sql = (
            "SELECT run_id, code_prefix, status, task_scope, task_id, project_id, "
            "harness, owner, prompt, prompt_text, prompt_source, prompt_version, "
            "handoff_id, label, progress, next_steps_json, state_note, "
            "outcome, summary, "
            "model, created_at, opened_at, heartbeat_at, closed_at, report_count, "
            "last_report_at, blocked_on_user_json, next_run_hint_json, domain_id, "
            "archived_at, archive_reason "
            "FROM butler_runs"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, run_id DESC LIMIT ?"
        params.append(limit)
        return self._all(sql, tuple(params))

    def open_run(self, code: str, *, owner: str | None = None,
                 harness: str | None = None, model: str | None = None) -> dict[str, Any]:
        run = self.get_run_by_code(code)
        if run["status"] in ("closed", "abandoned", "failed"):
            raise ButlerError("run_closed", "run 已结束", 409)
        if run["status"] == "created":
            created = datetime.strptime(run["created_at"], "%Y-%m-%d %H:%M:%S")
            if datetime.now() - created > timedelta(days=_CODE_TTL_DAYS):
                raise ButlerError("code_expired", "会话码已过期（创建后 30 天未开跑）", 410)
        # 2026-10-29 用户拍板：**一个码只能被一个执行者持有**。
        # 执行者往往不自报 owner，所以这里用「自报身份」派生一个默认值：
        # 同一个人重连（harness/model 不变）能过，换个人来就被 409 挡住。
        # 局限写在 DD-run级状态与并发.md：同一 harness + 同一模型的两个实例
        # 在服务端看来是一个人，这条挡不住——那是身份问题，不是锁能解决的。
        effective_owner = owner or "/".join(
            x for x in ((harness or "").strip(), (model or "").strip()) if x
        ) or None
        held_by = (run.get("owner") or "").strip()
        # 只有「两边都知道是谁」才判冲突：不报身份的调用（例如 context.next）
        # 不该被自己的开跑挡在门外；报了什么身份就按什么身份认。
        if held_by and effective_owner and effective_owner != held_by:
            raise ButlerError(
                "owner_conflict",
                f"这个会话码已被 {held_by} 占用；换一路请新开一个 run",
                409,
            )

        with self._lock:
            self.conn.execute(
                """UPDATE butler_runs
                   SET status = CASE WHEN status = 'created' THEN 'opened'
                                     ELSE status END,
                       opened_at = COALESCE(opened_at, ?),
                       heartbeat_at = ?, owner = COALESCE(owner, ?),
                       harness = COALESCE(?, harness), model = COALESCE(?, model)
                   WHERE run_id = ?""",
                (_now(), _now(), effective_owner, harness, model, run["run_id"]),
            )
            if run.get("handoff_id"):
                self.conn.execute(
                    """UPDATE butler_handoffs
                       SET status = 'pending_archive', updated_at = ?
                       WHERE handoff_id = ? AND status = 'active'""",
                    (_now(), run["handoff_id"]),
                )
            self.conn.commit()
        self.append_event(run["run_id"], "run.opened",
                          {"owner": effective_owner, "harness": harness})
        return self.get_run(run["run_id"])

    def touch_run(self, run_id: str, *, active: bool = True) -> None:
        with self._lock:
            self.conn.execute(
                """UPDATE butler_runs
                   SET heartbeat_at = ?,
                       status = CASE WHEN ? AND status IN ('opened', 'idle')
                                     THEN 'running' ELSE status END
                   WHERE run_id = ?""",
                (_now(), 1 if active else 0, run_id),
            )
            self.conn.commit()

    def close_run(self, run_id: str, *, outcome: str, summary: str) -> None:
        with self._lock:
            self.conn.execute(
                """UPDATE butler_runs
                   SET status = 'closed', outcome = ?, summary = ?, closed_at = ?
                   WHERE run_id = ?""",
                (outcome, summary, _now(), run_id),
            )
            self.conn.commit()

    def close_run_by_user(self, run_id: str) -> None:
        """用户关闭 run：只改状态与时间，不动最近报告 outcome/summary。"""
        with self._lock:
            self.conn.execute(
                """UPDATE butler_runs
                   SET status = 'closed', closed_at = ?
                   WHERE run_id = ?""",
                (_now(), run_id),
            )
            self.conn.commit()
        self.append_event(run_id, "run.closed", {"by": "user"})

    def close_run_for_handoff(self, run_id: str, *, to_code: str) -> None:
        """交接即换人：原路收尾。

        不覆盖 `summary`（那是它最后一轮报告，是证据），只把状态与 outcome 落下。
        不收尾的话「所有未关的路都 done」永远不成立，事务就永远自动完成不了。
        """
        with self._lock:
            self.conn.execute(
                """UPDATE butler_runs
                   SET status = 'closed', outcome = 'handoff', closed_at = ?
                   WHERE run_id = ?""",
                (_now(), run_id),
            )
            self.conn.commit()
        self.append_event(run_id, "run.handoff", {"to": to_code})

    # ── 面板归档（v15） ──────────────────────────────
    #
    # 归档 = 「从事务面板收走」，只加时间戳，不动 status。
    # 想继续用这一路就恢复；归档期间它仍然能读、能看报告，只是不进列表。

    def archive_run(self, run_id: str, *, reason: str | None = None) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run.get("archived_at"):
            return {**run, "archived_at": run["archived_at"], "already": True}
        at = _now()
        with self._lock:
            self.conn.execute(
                "UPDATE butler_runs SET archived_at = ?, archive_reason = ? "
                "WHERE run_id = ?",
                (at, reason, run_id),
            )
            self.conn.commit()
        self.append_event(run_id, "run.archived", {"reason": reason})
        return {**self.get_run(run_id), "already": False}

    def restore_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run.get("archived_at"):
            return {**run, "already": True}
        with self._lock:
            self.conn.execute(
                "UPDATE butler_runs SET archived_at = NULL, archive_reason = NULL "
                "WHERE run_id = ?",
                (run_id,),
            )
            self.conn.commit()
        self.append_event(run_id, "run.restored", {})
        return {**self.get_run(run_id), "already": False}

    # ── 交接提示词手改（v15） ────────────────────────
    #
    # 交接正文平时是实时渲染的（跟着进度/下一步变）。用户改过的这一份存下来，
    # 在「还没交接」那一态顶替渲染结果；一旦开出交接单，正文来自交接单本身，
    # 这里存的东西自然不再参与——不需要额外的作废动作。

    def set_handoff_override(self, run_id: str,
                             text: str | None) -> dict[str, Any]:
        self.get_run(run_id)  # 不存在直接 404
        clean = (text or "").strip()
        with self._lock:
            self.conn.execute(
                "UPDATE butler_runs SET handoff_text_override = ? WHERE run_id = ?",
                (clean or None, run_id),
            )
            self.conn.commit()
        self.append_event(run_id, "handoff.override",
                          {"cleared": not clean, "chars": len(clean)})
        return {"ok": True, "run_id": run_id, "chars": len(clean),
                "cleared": not clean}

    def set_manifest(self, run_id: str, manifest: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE butler_runs SET manifest_json = ? WHERE run_id = ?",
                (json.dumps(manifest, ensure_ascii=False), run_id),
            )
            self.conn.commit()

    # ── 每轮报告（v10） ──────────────────────────────

    def get_report_by_idem(self, run_id: str,
                           idempotency_key: str | None) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        return self._one(
            "SELECT * FROM butler_reports WHERE run_id = ? AND idempotency_key = ?",
            (run_id, idempotency_key),
        )

    def latest_report_payload(self, run_id: str) -> dict[str, Any]:
        row = self._one(
            """SELECT payload_json FROM butler_reports
               WHERE run_id = ? ORDER BY attempt DESC LIMIT 1""",
            (run_id,),
        )
        if not row:
            return {}
        try:
            payload = json.loads(row.get("payload_json") or "{}")
            return payload if isinstance(payload, dict) else {}
        except (TypeError, ValueError):
            return {}

    def list_reports(self, run_id: str) -> list[dict[str, Any]]:
        return self._all(
            """SELECT attempt, outcome, summary, created_at
               FROM butler_reports WHERE run_id = ? ORDER BY attempt ASC""",
            (run_id,),
        )

    def run_counters(self, run_id: str) -> dict[str, Any]:
        """面板计数条：轮次 / 投递页数与字符 / 候选数 / 报告时间。"""
        run = self._one(
            """SELECT report_count, last_report_at, opened_at,
                      blocked_on_user_json, next_run_hint_json, status
               FROM butler_runs WHERE run_id = ?""",
            (run_id,),
        ) or {}
        delivery = self._one(
            """SELECT COUNT(*) AS pages, COALESCE(SUM(chars), 0) AS chars
               FROM (SELECT cursor, MAX(chars) AS chars
                     FROM butler_deliveries WHERE run_id = ? GROUP BY cursor)""",
            (run_id,),
        ) or {}
        cand = self._one(
            "SELECT COUNT(*) AS n FROM memory_candidates WHERE run_id = ?",
            (run_id,),
        ) or {}
        return {
            "report_count": int(run.get("report_count") or 0),
            "last_report_at": run.get("last_report_at"),
            "opened_at": run.get("opened_at"),
            "blocked_on_user": _json_or_empty(run.get("blocked_on_user_json")),
            "next_run_hint": _json_or_empty(run.get("next_run_hint_json")),
            "delivery_pages": int(delivery.get("pages") or 0),
            "delivery_chars": int(delivery.get("chars") or 0),
            "candidate_count": int(cand.get("n") or 0),
        }

    def append_report(
        self,
        run_id: str,
        *,
        attempt: int,
        outcome: str,
        summary: str,
        payload: dict[str, Any],
        result: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """追加一条报告，并把 run 置 idle（不关闭）。

        报告与 run 最新态在同一个写锁/commit 里完成；任务/记忆等副作用
        由调用方在此之前完成。
        """
        with self._lock:
            self.conn.execute(
                """INSERT INTO butler_reports
                   (run_id, attempt, outcome, summary, payload_json,
                    result_json, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, attempt, outcome, summary,
                 json.dumps(payload, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False), idempotency_key),
            )
            self.conn.execute(
                """UPDATE butler_runs
                   SET status = 'idle', outcome = ?, summary = ?,
                       report_count = ?, last_report_at = ?
                   WHERE run_id = ?""",
                (outcome, summary, attempt, _now(), run_id),
            )
            self.conn.commit()
        row = self._one(
            "SELECT * FROM butler_reports WHERE run_id = ? AND attempt = ?",
            (run_id, attempt),
        )
        return row or {}

    def set_run_json_fields(
        self,
        run_id: str,
        *,
        blocked_on_user: dict[str, Any] | None = None,
        next_run_hint: str | dict[str, Any] | None = None,
    ) -> None:
        """有值才写；None 表示本轮不更新该列。"""
        sets: list[str] = []
        params: list[Any] = []
        if blocked_on_user is not None:
            sets.append("blocked_on_user_json = ?")
            params.append(json.dumps(blocked_on_user, ensure_ascii=False))
        if next_run_hint is not None:
            # 统一成对象，避免字符串被再 json.dumps 成 "\"文本\""
            hint = {"text": next_run_hint} if isinstance(next_run_hint, str) else next_run_hint
            sets.append("next_run_hint_json = ?")
            params.append(json.dumps(hint, ensure_ascii=False))
        if not sets:
            return
        params.append(run_id)
        with self._lock:
            self.conn.execute(
                f"UPDATE butler_runs SET {', '.join(sets)} WHERE run_id = ?",
                tuple(params),
            )
            self.conn.commit()

    def record_feedback(
        self,
        run_id: str,
        bundle: str,
        item: dict[str, Any],
    ) -> bool:
        """把一条召回反馈挂到该 run 对应 bundle 的投递行。

        ``butler_deliveries`` 目前是页级行，没有 item 级明细；因此校验到
        「该 bundle 确实投给过本 run」，item_id 原样写进 feedback_note JSON，
        供 T6 的 bundle items 落账后升级为 item 级校验。
        """
        row = self._one(
            """SELECT id, feedback_note FROM butler_deliveries
               WHERE run_id = ? AND bundle = ? ORDER BY id DESC LIMIT 1""",
            (run_id, bundle),
        )
        if not row:
            return False
        try:
            items = json.loads(row.get("feedback_note") or "[]")
            if not isinstance(items, list):
                items = []
        except (TypeError, ValueError):
            items = []
        items.append(item)
        with self._lock:
            self.conn.execute(
                """UPDATE butler_deliveries
                   SET used = ?, useful = ?, feedback_note = ?
                   WHERE id = ?""",
                (
                    1 if item.get("used") else 0,
                    1 if item.get("useful") else 0,
                    json.dumps(items, ensure_ascii=False),
                    row["id"],
                ),
            )
            self.conn.commit()
        return True

    # ── notes（v10） ───────────────────────────────────

    def add_note(
        self,
        run_id: str,
        text: str,
        *,
        direction: str = "agent",
        kind: str = "note",
        client_note_id: str | None = None,
    ) -> dict[str, Any]:
        if client_note_id:
            existing = self._one(
                "SELECT * FROM butler_notes WHERE run_id = ? AND client_note_id = ?",
                (run_id, client_note_id),
            )
            if existing:
                return {**existing, "_duplicate": True}
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO butler_notes
                   (run_id, client_note_id, direction, kind, text)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, client_note_id, direction, kind, text),
            )
            note_id = cur.lastrowid
            self.conn.commit()
        self.append_event(run_id, "note.created", {
            "note_id": note_id, "direction": direction, "kind": kind,
        })
        return {**(self._one("SELECT * FROM butler_notes WHERE id = ?", (note_id,)) or {}),
                "_duplicate": False}

    # ── handoff ──────────────────────────────────────────

    def create_handoff(
        self,
        payload: dict[str, Any],
        *,
        title: str | None = None,
        task_scope: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
        run_id: str | None = None,
        created_by: str = "agent",
    ) -> dict[str, Any]:
        handoff_id = f"hof_{uuid.uuid4().hex[:12]}"
        with self._lock:
            self.conn.execute(
                """INSERT INTO butler_handoffs
                   (handoff_id, task_scope, task_id, project_id, run_id, status, title,
                    payload_json, created_by)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (handoff_id, task_scope, task_id, project_id, run_id, title,
                 json.dumps(payload, ensure_ascii=False), created_by),
            )
            self.conn.commit()
        self.append_event(run_id, "handoff.created",
                          {"handoff_id": handoff_id, "task_id": task_id})
        return self.get_handoff(handoff_id)

    def get_handoff(self, handoff_id: str) -> dict[str, Any]:
        handoff = self._one(
            "SELECT * FROM butler_handoffs WHERE handoff_id = ?", (handoff_id,)
        )
        if not handoff:
            raise ButlerError("handoff_not_found", "handoff 不存在", 404)
        try:
            handoff["payload"] = json.loads(handoff.get("payload_json") or "{}")
        except (ValueError, TypeError):
            handoff["payload"] = {}
        return handoff

    def get_active_handoff(self, *, task_scope: str | None = None,
                           task_id: str | None = None,
                           run_id: str | None = None) -> dict[str, Any] | None:
        """该事务（或该路）当前有效的交接单。

        `active` = 没人接；`pending_archive` = 已有人接、未收尾。
        `archived` / `superseded` 视为已消费，不再算交接态。

        2026-10-29 用户拍板：**交接挂 run**。多路并发时按 run 精确取，
        否则三路各自交接，面板不知道该渲染哪一份。
        老数据（v14 之前写的）没有 run_id，给了 run 也取不到，退回按事务取一次。
        """
        statuses = "('active', 'pending_archive')"
        if run_id:
            row = self._one(
                f"""SELECT * FROM butler_handoffs
                    WHERE run_id = ? AND status IN {statuses}
                    ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (run_id,),
            )
            if row:
                return self._handoff_row(row)
        if not (task_scope and task_id):
            return None
        row = self._one(
            f"""SELECT * FROM butler_handoffs
                WHERE task_scope = ? AND task_id = ? AND status IN {statuses}
                ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (task_scope, task_id),
        )
        return self._handoff_row(row) if row else None

    @staticmethod
    def _handoff_row(row: dict[str, Any]) -> dict[str, Any]:
        try:
            row["payload"] = json.loads(row.get("payload_json") or "{}")
        except (ValueError, TypeError):
            row["payload"] = {}
        return row

    def set_handoff_status(self, handoff_id: str, status: str) -> None:
        if status not in ("active", "pending_archive", "archived", "superseded"):
            raise ButlerError("bad_handoff_status", "非法 handoff 状态")
        with self._lock:
            self.conn.execute(
                """UPDATE butler_handoffs
                   SET status = ?, updated_at = ?,
                       archived_at = CASE WHEN ? = 'archived' THEN ? ELSE archived_at END
                   WHERE handoff_id = ?""",
                (status, _now(), status, _now(), handoff_id),
            )
            self.conn.commit()
        self.append_event(None, f"handoff.{status}", {"handoff_id": handoff_id})

    # ── delivery ─────────────────────────────────────────

    def record_delivery(self, run_id: str, bundle: str, cursor: str,
                        digest: str, chars: int) -> tuple[dict[str, Any], bool]:
        old = self._one(
            """SELECT * FROM butler_deliveries
               WHERE run_id = ? AND bundle = ? AND cursor = ?""",
            (run_id, bundle, cursor),
        )
        if old:
            if old["digest"] == digest:
                return old, False
            with self._lock:
                self.conn.execute(
                    """UPDATE butler_deliveries
                       SET digest = ?, chars = ?, delivered_at = ?
                       WHERE id = ?""",
                    (digest, chars, _now(), old["id"]),
                )
                self.conn.commit()
            return {**old, "digest": digest, "chars": chars}, True

        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO butler_deliveries
                   (run_id, bundle, cursor, digest, chars) VALUES (?, ?, ?, ?, ?)""",
                (run_id, bundle, cursor, digest, chars),
            )
            self.conn.commit()
            delivery_id = int(cur.lastrowid or 0)
        return {
            "id": delivery_id, "run_id": run_id, "bundle": bundle,
            "cursor": cursor, "digest": digest, "chars": chars,
        }, False

    # ── events ───────────────────────────────────────────

    def append_event(self, run_id: str | None, event_type: str,
                     payload: dict[str, Any] | None = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO butler_events (run_id, event_type, payload_json) VALUES (?, ?, ?)",
                (run_id, event_type, json.dumps(payload or {}, ensure_ascii=False)),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def list_events(self, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM butler_events WHERE run_id = ? ORDER BY seq ASC LIMIT ?",
            (run_id, limit),
        )

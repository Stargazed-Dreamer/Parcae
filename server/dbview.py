"""数据库与文件只读浏览接口（面板「数据库」页的数据源）。

设计依据：
- `workspace/local_butler/30_架构与设计/20_现行设计/28_数据与记忆_面板视图与召回整改_20261030.md` §1
- `workspace/local_butler/30_架构与设计/20_现行设计/27_面板现行设计_v3.6_20261030.md` §2.9
- 施工计划 `.../30_施工与证据/42_施工方案_阶段一事务与数据库_20261031.md` §4 S6

四条硬约束：
1. **只读**：所有连接一律 `PRAGMA query_only=ON`，本模块不提供任何写路径。
2. **不猜路径**：库用注册表 id（不接受用户给的文件路径），表名先查 `sqlite_master` 再拼，
   拼之前做标识符双引号转义；文件路径限制在仓库根内 + 扩展名白名单 + 敏感目录黑名单。
3. **不做业务解释**：只给行数、列、填充率、样例值；"这算不算问题"不在这里判断。
4. **大表不拖死**：超过 `_FILL_SCAN_LIMIT` 行的表跳过填充率（返回 null），由前端显示"未统计"。

不属于本模块的库：`data/accounting.db`（记账）等**插件自有库**，随插件页走，不进基座面板。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from server.memory.config import get_memory_config

router = APIRouter(prefix="/db", tags=["db"])

_ROOT = Path(__file__).resolve().parents[1]

# 填充率扫描上限：超过就不扫（避免超大表把请求拖死）
_FILL_SCAN_LIMIT = 200_000

# 单次返回的最大行数 / 单元格预览长度
_MAX_LIMIT = 500
_MAX_CELL = 240

# 文件预览：扩展名白名单 + 体积上限
_TEXT_EXT = {
    ".md", ".txt", ".json", ".jsonl", ".toml", ".yaml", ".yml", ".cfg", ".ini",
    ".py", ".html", ".htm", ".csv", ".log", ".tsv", ".sql",
}
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_PREVIEW_LINES = 2000

# 敏感路径黑名单（只读浏览也不给看）
_DENY_PARTS = {
    ".git", "chrome_debug", "backups", "private_vault", "__pycache__",
    "node_modules", ".venv", "release",
}
_DENY_NAMES = {"config.toml", "keys.json", ".env", "credentials.json"}


# ========== 数据源注册表 ==========

def _memory_db_path() -> Path:
    """memory.db 走记忆配置（测试可用 PARCAE_MEMORY_DB_PATH 重定向）。"""
    p = Path(get_memory_config().db_path)
    return p if p.is_absolute() else _ROOT / p


_SOURCES: dict[str, dict[str, Any]] = {
    # 注意：这里一律用 lambda 间接取，别直接塞函数对象——否则测试 monkeypatch
    # `_memory_db_path` 时注册表里仍是旧引用，隔离失效。
    "memory": {"label": "memory.db", "owner": "记忆/事务", "path": lambda: _memory_db_path()},
    "agent": {"label": "agent.db", "owner": "中枢", "path": lambda: _ROOT / "data" / "client" / "agent.db"},
    "inbox": {"label": "inbox.db", "owner": "事务", "path": lambda: _ROOT / "data" / "inbox.db"},
    "inbound": {"label": "inbound_calls.db", "owner": "接口", "path": lambda: _ROOT / "data" / "inbound_calls.db"},
    "browser_stats": {"label": "browser_stats.db", "owner": "插件", "path": lambda: _ROOT / "data" / "browser_stats.db"},
}


# ========== 连接与工具 ==========

def _connect(db_path: Path) -> sqlite3.Connection:
    """只读连接。先试 URI 只读模式，WAL 打不开再退回普通连接 + query_only。"""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def _quote_ident(name: str) -> str:
    """SQLite 标识符转义：双引号包裹，内部双引号翻倍。"""
    return '"' + name.replace('"', '""') + '"'


def _cell(value: Any) -> Any:
    """单元格值 → JSON 友好。BLOB 只报体积，长文本截断。"""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<blob {len(bytes(value))} bytes>"
    if isinstance(value, str) and len(value) > _MAX_CELL:
        return value[:_MAX_CELL] + f"…(+{len(value) - _MAX_CELL})"
    return value


def _resolve_source(source: str) -> tuple[str, Path]:
    key = (source or "").strip()
    entry = _SOURCES.get(key)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"未知数据源 {source!r}；可用：{sorted(_SOURCES)}",
        )
    path = entry["path"]()
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"数据源文件不存在：{entry['label']}")
    return key, path


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def _require_table(conn: sqlite3.Connection, table: str) -> str:
    """表名必须真实存在；返回库里的原名（用于后续安全拼接）。"""
    hit = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (table,),
    ).fetchone()
    if hit is None:
        raise HTTPException(status_code=404, detail=f"表不存在：{table}")
    return hit["name"]


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}").fetchone()[0])
    except sqlite3.DatabaseError:
        return -1


# ========== 端点 ==========

@router.get("/sources")
def list_sources() -> dict[str, Any]:
    """五个库的总览：是否存在、体积、表数。文件类数据源用 /db/files。"""
    out: list[dict[str, Any]] = []
    for key, entry in _SOURCES.items():
        path: Path = entry["path"]()
        item: dict[str, Any] = {
            "id": key,
            "label": entry["label"],
            "owner": entry["owner"],
            "exists": path.is_file(),
            "size_bytes": path.stat().st_size if path.is_file() else 0,
            "tables": None,
        }
        if item["exists"]:
            try:
                conn = _connect(path)
                try:
                    item["tables"] = len(_table_names(conn))
                finally:
                    conn.close()
            except sqlite3.DatabaseError as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
        out.append(item)
    return {"sources": out, "read_only": True}


@router.get("/tables")
def list_tables(source: str = Query(..., description="数据源 id，见 /db/sources")) -> dict[str, Any]:
    """某库的表清单（含行数、列数）。"""
    key, path = _resolve_source(source)
    conn = _connect(path)
    try:
        tables: list[dict[str, Any]] = []
        for name in _table_names(conn):
            cols = conn.execute(f"PRAGMA table_info({_quote_ident(name)})").fetchall()
            tables.append({
                "name": name,
                "rows": _count(conn, name),
                "columns": len(cols),
            })
    finally:
        conn.close()
    tables.sort(key=lambda t: (-(t["rows"] or 0), t["name"]))
    return {"source": key, "label": _SOURCES[key]["label"], "tables": tables}


@router.get("/schema")
def table_schema(
    source: str = Query(...),
    table: str = Query(...),
) -> dict[str, Any]:
    """字段表：类型 / 非空 / 主键 / 填充率 / 样例值。

    填充率靠一条 `SELECT COUNT(*), SUM(CASE ...)` 单趟搞定；超过 `_FILL_SCAN_LIMIT` 行则跳过。
    """
    key, path = _resolve_source(source)
    conn = _connect(path)
    try:
        name = _require_table(conn, table)
        q = _quote_ident(name)
        total = _count(conn, name)
        cols = conn.execute(f"PRAGMA table_info({q})").fetchall()
        col_names = [c["name"] for c in cols]

        fills: dict[str, int | None] = {c: None for c in col_names}
        scanned = 0 <= total <= _FILL_SCAN_LIMIT
        if scanned and col_names:
            parts = [
                "SUM(CASE WHEN {c} IS NOT NULL AND CAST({c} AS TEXT) <> '' THEN 1 ELSE 0 END)".format(
                    c=_quote_ident(c)
                )
                for c in col_names
            ]
            row = conn.execute(
                f"SELECT {', '.join(parts)} FROM {q}"
            ).fetchone()
            for i, c in enumerate(col_names):
                fills[c] = int(row[i] or 0)

        columns: list[dict[str, Any]] = []
        for c in cols:
            col = c["name"]
            filled = fills.get(col)
            samples: list[Any] = []
            if filled:
                try:
                    rows = conn.execute(
                        f"SELECT {_quote_ident(col)} FROM {q} "
                        f"WHERE {_quote_ident(col)} IS NOT NULL "
                        f"AND CAST({_quote_ident(col)} AS TEXT) <> '' LIMIT 3"
                    ).fetchall()
                    samples = [_cell(r[0]) for r in rows]
                except sqlite3.DatabaseError:
                    samples = []
            columns.append({
                "name": col,
                "type": c["type"] or "",
                "notnull": bool(c["notnull"]),
                "pk": bool(c["pk"]),
                "default": c["dflt_value"],
                "filled": filled,
                "total": total,
                "fill_ratio": (
                    round(filled / total, 4)
                    if filled is not None and total > 0
                    else None
                ),
                "samples": samples,
            })

        indexes = [
            {"name": r["name"], "unique": bool(r["unique"]), "origin": r["origin"]}
            for r in conn.execute(f"PRAGMA index_list({q})").fetchall()
        ]
        ddl_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()

    return {
        "source": key,
        "table": name,
        "rows": total,
        "fill_scanned": scanned,
        "columns": columns,
        "indexes": indexes,
        "ddl": ddl_row["sql"] if ddl_row else None,
    }


@router.get("/rows")
def table_rows(
    source: str = Query(...),
    table: str = Query(...),
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """行列表（只读分页）。无 ORDER BY —— 只保证翻页不重不漏在单次会话内稳定。"""
    key, path = _resolve_source(source)
    conn = _connect(path)
    try:
        name = _require_table(conn, table)
        q = _quote_ident(name)
        total = _count(conn, name)
        cur = conn.execute(f"SELECT * FROM {q} LIMIT ? OFFSET ?", (limit, offset))
        col_names = [d[0] for d in cur.description] if cur.description else []
        rows = [[_cell(v) for v in dict(r).values()] for r in cur.fetchall()]
    finally:
        conn.close()
    return {
        "source": key,
        "table": name,
        "total": total,
        "offset": offset,
        "limit": limit,
        "columns": col_names,
        "rows": rows,
        "next_offset": offset + len(rows) if offset + len(rows) < total else None,
    }


# ========== 文件类数据源 ==========

_GROUPS: dict[str, dict[str, Any]] = {
    "memory": {
        "label": "记忆真源",
        "owner": "记忆",
        "globs": [".agents/memory/*.md", ".agents/memory/machine/**/*.md"],
        # 日日志单独成组，别在"真源"里重复出现（两边都列会让同一文件出现两次）
        "exclude": ["20*.md"],
    },
    "logs": {
        "label": "日日志",
        "owner": "记忆",
        "globs": [".agents/memory/20*.md"],
        "exclude": [],
    },
    "skills": {
        "label": "项目级 Skill",
        "owner": "中枢",
        "globs": [".agents/skills/**/SKILL.md"],
        "exclude": [],
    },
    "docs": {
        "label": "契约与文档",
        "owner": "契约",
        "globs": ["AGENTS.md", "CHANGELOG.md", "docs/*.md", "workspace/*/AGENTS.md"],
        "exclude": [],
    },
    "projection": {
        "label": "harness 注入投影",
        "owner": "投影",
        "globs": [".workbuddy/memory/*.md"],
        "exclude": [],
    },
}


def _is_denied(path: Path) -> bool:
    if path.name in _DENY_NAMES:
        return True
    return any(part in _DENY_PARTS for part in path.parts)


def _file_meta(path: Path) -> dict[str, Any]:
    st = path.stat()
    lines = 0
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(512 * 1024)
            lines = chunk.count(b"\n")
            partial = len(chunk) < st.st_size
    except OSError:
        partial = True
    return {
        "path": path.relative_to(_ROOT).as_posix(),
        "size_bytes": st.st_size,
        "mtime": int(st.st_mtime),
        "lines": lines,
        "lines_partial": partial,
        "ext": path.suffix.lower(),
    }


@router.get("/files")
def list_files(group: str = Query(..., description="文件组 id，见返回的 groups")) -> dict[str, Any]:
    """按组列文件（记忆真源 / 日日志 / skill / 文档 / 注入投影）。"""
    entry = _GROUPS.get(group)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"未知文件组 {group!r}；可用：{sorted(_GROUPS)}")
    seen: set[str] = set()
    files: list[dict[str, Any]] = []
    excludes = entry.get("exclude") or []
    for pattern in entry["globs"]:
        for path in _ROOT.glob(pattern):
            if not path.is_file() or _is_denied(path):
                continue
            if any(path.match(pat) for pat in excludes):
                continue
            rel = path.relative_to(_ROOT).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            try:
                files.append(_file_meta(path))
            except OSError:
                continue
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return {
        "group": group,
        "label": entry["label"],
        "owner": entry["owner"],
        "count": len(files),
        "files": files,
        "groups": {k: v["label"] for k, v in _GROUPS.items()},
    }


@router.get("/file")
def preview_file(
    path: str = Query(..., description="仓库内相对路径"),
    lines: int = Query(200, ge=1, le=_MAX_PREVIEW_LINES),
) -> dict[str, Any]:
    """只读预览文件前 N 行。路径必须落在仓库根内、扩展名在白名单、不在敏感目录。"""
    raw = (path or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ".." in Path(raw).parts or ":" in raw:
        raise HTTPException(status_code=400, detail="路径必须是仓库内相对路径")
    target = (_ROOT / raw).resolve()
    try:
        target.relative_to(_ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="路径越出仓库根")
    relative = target.relative_to(_ROOT).as_posix()
    if _is_denied(target):
        raise HTTPException(status_code=403, detail="该路径不在可浏览范围")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在：{raw}")
    if target.suffix.lower() not in _TEXT_EXT:
        raise HTTPException(status_code=415, detail=f"不支持预览的类型：{target.suffix or '(无扩展名)'}")

    size = target.stat().st_size
    if size > _MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件 {size} 字节，超过预览上限 {_MAX_FILE_BYTES}；请直接打开文件",
        )
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取失败：{exc}")

    all_lines = text.splitlines()
    head = all_lines[:lines]
    return {
        "path": relative,
        "size_bytes": size,
        "mtime": target.stat().st_mtime,
        "total_lines": len(all_lines),
        "requested_lines": lines,
        "returned_lines": len(head),
        "truncated": len(all_lines) > len(head),
        "content": "\n".join(head),
    }

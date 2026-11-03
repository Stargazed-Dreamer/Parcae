"""server.dbview（面板「数据库」页的只读数据源）HTTP 端点测试。

用最小化 FastAPI app（只挂 dbview_router），不拉 server.main、不占端口。
DB 侧一律指向 tmp_path 里现造的 sqlite，**不碰生产 memory.db**。

覆盖：
- GET /db/sources                     五个库的总览
- GET /db/tables?source=             表清单 + 行数
- GET /db/schema?source=&table=      字段表 + 填充率 + 索引
- GET /db/rows?source=&table=        只读分页
- GET /db/files?group=               文件组
- GET /db/file?path=&lines=          文件预览
负例：未知数据源 / 不存在的表 / SQL 注入串 / 路径穿越 / 敏感路径 / 二进制文件

"只读"是本模块的第一硬约束，最后一条用例直接断言写操作被 query_only 挡住。
"""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import dbview
from server.dbview import router as dbview_router


@pytest.fixture
def fake_db(tmp_path, monkeypatch):
    """造一个两表库，并把 memory 数据源指向它。"""
    path = tmp_path / "fake_memory.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY,
            key TEXT NOT NULL,
            value TEXT,
            confidence REAL
        );
        CREATE UNIQUE INDEX idx_fake_facts_key ON facts(key);
        CREATE TABLE empties (
            a TEXT,
            b INTEGER
        );
        """
    )
    conn.executemany(
        "INSERT INTO facts (key, value, confidence) VALUES (?, ?, ?)",
        [
            ("k1", "v1", 0.9),
            ("k2", "v2", 0.5),
            ("k3", None, None),
            ("k4", "x" * 400, 0.1),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(dbview, "_memory_db_path", lambda: path)
    return path


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(dbview_router)
    return TestClient(app)


# ========== sources / tables ==========

def test_sources_lists_all_five(client, fake_db):
    r = client.get("/db/sources")
    assert r.status_code == 200
    body = r.json()
    assert body["read_only"] is True
    ids = {s["id"] for s in body["sources"]}
    assert ids == {"memory", "agent", "inbox", "inbound", "browser_stats"}
    mem = next(s for s in body["sources"] if s["id"] == "memory")
    assert mem["exists"] is True
    assert mem["tables"] == 2
    assert mem["size_bytes"] > 0


def test_tables_reports_rows_and_columns(client, fake_db):
    r = client.get("/db/tables", params={"source": "memory"})
    assert r.status_code == 200
    tables = {t["name"]: t for t in r.json()["tables"]}
    assert set(tables) == {"facts", "empties"}
    assert tables["facts"]["rows"] == 4
    assert tables["facts"]["columns"] == 4
    assert tables["empties"]["rows"] == 0


def test_unknown_source_is_404(client, fake_db):
    # 数据源只认注册表 id，不接受路径
    for bad in ["../../config", "memory.db", "'; DROP TABLE facts; --"]:
        assert client.get("/db/tables", params={"source": bad}).status_code == 404


# ========== schema ==========

def test_schema_fill_ratio_and_indexes(client, fake_db):
    r = client.get("/db/schema", params={"source": "memory", "table": "facts"})
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == 4
    assert body["fill_scanned"] is True

    cols = {c["name"]: c for c in body["columns"]}
    assert cols["id"]["pk"] is True
    assert cols["key"]["notnull"] is True
    assert cols["key"]["filled"] == 4
    # 3/4 条 value 非空（'v1','v2','x'*400），k3 是 NULL
    assert cols["value"]["filled"] == 3
    assert cols["value"]["fill_ratio"] == 0.75
    assert cols["confidence"]["filled"] == 3
    # id 是 INTEGER，'CAST AS TEXT <> ""' 不该把任何一行判成空
    assert cols["id"]["filled"] == 4

    assert any(i["name"] == "idx_fake_facts_key" and i["unique"] for i in body["indexes"])
    assert "CREATE TABLE facts" in body["ddl"]

    # 超长值按 _MAX_CELL 截断，且标注多出来的长度
    sample = cols["value"]["samples"]
    assert sample and sample[0] == "v1"
    long_samples = [s for s in sample if isinstance(s, str) and s.endswith(")")]
    for s in long_samples:
        assert len(s) <= dbview._MAX_CELL + 12


def test_schema_missing_table_is_404(client, fake_db):
    r = client.get("/db/schema", params={"source": "memory", "table": "nope"})
    assert r.status_code == 404


# ========== rows ==========

def test_rows_pagination(client, fake_db):
    r = client.get("/db/rows", params={"source": "memory", "table": "facts", "limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4
    assert len(body["rows"]) == 2
    assert body["next_offset"] == 2
    assert body["columns"] == ["id", "key", "value", "confidence"]

    r2 = client.get(
        "/db/rows", params={"source": "memory", "table": "facts", "limit": 10, "offset": 2}
    )
    body2 = r2.json()
    assert len(body2["rows"]) == 2
    assert body2["next_offset"] is None


def test_rows_limits_are_enforced(client, fake_db):
    assert client.get(
        "/db/rows", params={"source": "memory", "table": "facts", "limit": 0}
    ).status_code == 422
    assert client.get(
        "/db/rows", params={"source": "memory", "table": "facts", "limit": dbview._MAX_LIMIT + 1}
    ).status_code == 422


def test_rows_are_read_only(client, fake_db):
    """只读是硬约束：端点返回的库连接不允许任何写。"""
    conn = dbview._connect(fake_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM facts")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO facts (key) VALUES ('hack')")
    finally:
        conn.close()

    # 原数据没被改动
    check = sqlite3.connect(str(fake_db))
    try:
        assert check.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 4
    finally:
        check.close()


# ========== files ==========

def test_files_group_and_unknown_group(client):
    r = client.get("/db/files", params={"group": "docs"})
    assert r.status_code == 200
    body = r.json()
    assert body["group"] == "docs"
    assert body["count"] == len(body["files"])
    assert set(body["groups"]) == {"memory", "logs", "skills", "docs", "projection"}
    for f in body["files"]:
        assert not f["path"].startswith("/")
        assert f["size_bytes"] >= 0

    assert client.get("/db/files", params={"group": "nope"}).status_code == 404


def test_memory_group_excludes_dated_logs(client):
    """日日志单独成组，别在"记忆真源"里重复出现。"""
    groups = {}
    for g in ("memory", "logs"):
        r = client.get("/db/files", params={"group": g})
        groups[g] = {f["path"] for f in r.json()["files"]}
    assert not (groups["memory"] & groups["logs"])


def test_file_preview(client):
    r = client.get("/db/file", params={"path": "AGENTS.md", "lines": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "AGENTS.md"
    assert body["returned_lines"] == 3
    assert body["total_lines"] > 3
    assert body["truncated"] is True
    assert body["content"].startswith("#")


def test_file_preview_rejects_escapes_and_secrets(client):
    assert client.get("/db/file", params={"path": "../x.md"}).status_code == 400
    assert client.get("/db/file", params={"path": "/etc/passwd"}).status_code == 400
    assert client.get("/db/file", params={"path": "C:/Windows/win.ini"}).status_code == 400
    assert client.get("/db/file", params={"path": "config.toml"}).status_code == 403
    assert client.get("/db/file", params={"path": "chrome_debug/README.md"}).status_code == 403
    assert client.get("/db/file", params={"path": "nope/missing.md"}).status_code == 404
    # LICENSE 无扩展名 → 不在预览白名单
    assert client.get("/db/file", params={"path": "LICENSE"}).status_code == 415

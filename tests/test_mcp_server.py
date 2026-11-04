from __future__ import annotations

import json

import pytest

from garmin_mcp.db.connection import init_db
from garmin_mcp.mcp_server import server


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    conn = init_db(path)
    conn.execute("INSERT INTO gear (gear_id, make_model) VALUES (1, 'shoe')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("GARMIN_DB_PATH", str(path))
    return path


def _gear_count(path) -> int:
    conn = init_db(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM gear").fetchone()[0]
    finally:
        conn.close()


def test_execute_sql_runs_select(db):
    assert json.loads(server.execute_sql("SELECT make_model FROM gear")) == [{"make_model": "shoe"}]


@pytest.mark.parametrize(
    "query",
    [
        "WITH x AS (SELECT 1) DELETE FROM gear",
        "WITH x AS (SELECT 1) UPDATE gear SET make_model = 'x'",
        "WITH x AS (SELECT 1) INSERT INTO gear (gear_id) VALUES (2)",
        "DELETE FROM gear",
    ],
)
def test_execute_sql_cannot_write(db, query):
    result = json.loads(server.execute_sql(query))
    assert "error" in result
    assert _gear_count(db) == 1


def test_execute_sql_missing_db_is_an_error_not_a_new_file(tmp_path, monkeypatch):
    path = tmp_path / "absent.db"
    monkeypatch.setenv("GARMIN_DB_PATH", str(path))
    assert "error" in json.loads(server.execute_sql("SELECT 1"))
    assert not path.exists()

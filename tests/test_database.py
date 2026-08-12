import os

import pytest

from agent_toolkit import database


def test_raises_without_agent_db_path(monkeypatch):
    monkeypatch.delenv("AGENT_DB_PATH", raising=False)
    monkeypatch.setattr(database, "DB_PATH", None)
    with pytest.raises(RuntimeError, match="AGENT_DB_PATH"):
        database.get_db()


def test_get_db_creates_parent_dir_and_connects(_isolated_db):
    conn = database.get_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
        conn.commit()
    finally:
        conn.close()
    assert os.path.exists(_isolated_db)

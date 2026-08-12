from agent_toolkit import database
from agent_toolkit.session import Session

_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    chat_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT (datetime('now')),
    PRIMARY KEY (chat_id, key)
);
"""


def _init_schema():
    conn = database.get_db()
    try:
        conn.executescript(_CREATE_SESSIONS)
        conn.commit()
    finally:
        conn.close()


def test_set_get_roundtrip(_isolated_db):
    _init_schema()
    s = Session("chat-1")
    s.set("mode", "chat")
    assert s.get("mode") == "chat"


def test_get_missing_key_returns_none(_isolated_db):
    _init_schema()
    s = Session("chat-1")
    assert s.get("nope") is None


def test_clear_removes_key(_isolated_db):
    _init_schema()
    s = Session("chat-1")
    s.set("mode", "chat")
    s.clear("mode")
    assert s.get("mode") is None


def test_append_history_and_ttl_expiry(_isolated_db, monkeypatch):
    _init_schema()
    s = Session("chat-1")
    s.append_history("user", "hi")
    assert s.history == [{"role": "user", "content": "hi"}]

    import time
    monkeypatch.setattr(time, "time", lambda: s.get("conversation_history_ts") + 999999)
    assert s.history == []

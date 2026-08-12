from agent_toolkit.database import get_db
from agent_toolkit.query_log import log_query


def _create_table():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS query_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            question TEXT,
            lane TEXT,
            status TEXT,
            duration_ms INTEGER,
            created_at TIMESTAMP DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def test_log_query_inserts_a_row():
    _create_table()
    log_query("chat-1", "when is the next meeting?", lane="frontier", status="ok", duration_ms=1200)

    conn = get_db()
    row = conn.execute("SELECT chat_id, question, lane, status, duration_ms FROM query_log").fetchone()
    conn.close()

    assert tuple(row) == ("chat-1", "when is the next meeting?", "frontier", "ok", 1200)


def test_log_query_swallows_errors_when_table_missing():
    log_query("chat-1", "question with no table created")  # does not raise

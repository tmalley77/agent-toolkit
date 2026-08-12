"""Durable record of every question an agent is asked.

Ported from ClaudeAIScoutMaster's app/query_log.py (see
ClaudeAIScoutMaster#277). Unlike llm_metrics, this one needed no
generalizing — it already wrote to a plain `query_log` table via
`agent_toolkit.database.get_db()` (AGENT_DB_PATH-driven), with no agent
name baked into the table or field names. Once each agent gets its own
physically separate SQLite file (the DB-split decision on #277), a bare
`query_log` table per file has no cross-agent collision risk.

Session history is working memory: it expires and gets cleared. A
tool-call log only sees turns that reached the tool loop. Neither is a
record of what the user actually asked, which matters for building a
retrieval eval set — so this table keeps the question itself, forever,
with no TTL and no pruning. Written before routing, deliberately: a
question that errored out or got answered badly is the most useful row
here, and both of those are turns the other two paths drop.

Consumers must create the table themselves:
  CREATE TABLE IF NOT EXISTS query_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_id TEXT,
      question TEXT,
      lane TEXT,
      status TEXT,
      duration_ms INTEGER,
      created_at TIMESTAMP DEFAULT (datetime('now'))
  );
"""
from agent_toolkit.database import get_db


def log_query(
    chat_id: str,
    question: str,
    lane: str | None = None,
    status: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Append one question. Never raises — analytics must never cost the
    user a reply."""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO query_log (chat_id, question, lane, status, duration_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, question, lane, status, duration_ms),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

"""Session state management — PTB context (ephemeral) + SQLite (durable)."""
import json
import time
from typing import Any
from agent_toolkit.database import get_db

HISTORY_TTL_SECONDS = 2 * 60 * 60  # 2 hours


class Session:
    """Per-chat durable state backed by SQLite sessions table."""

    def __init__(self, chat_id: str):
        self.chat_id = str(chat_id)

    def get(self, key: str) -> Any | None:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT value FROM sessions WHERE chat_id=? AND key=?",
                (self.chat_id, key),
            ).fetchone()
            return json.loads(row["value"]) if row else None
        finally:
            conn.close()

    def set(self, key: str, value: Any) -> None:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO sessions (chat_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id, key) DO UPDATE SET value=excluded.value, "
                "updated_at=datetime('now')",
                (self.chat_id, key, json.dumps(value, default=str)),
            )
            conn.commit()
        finally:
            conn.close()

    def clear(self, key: str) -> None:
        conn = get_db()
        try:
            conn.execute(
                "DELETE FROM sessions WHERE chat_id=? AND key=?",
                (self.chat_id, key),
            )
            conn.commit()
        finally:
            conn.close()

    @property
    def history(self) -> list[dict]:
        data = self.get("conversation_history")
        if not data:
            return []
        ts = self.get("conversation_history_ts") or 0
        if time.time() - ts > HISTORY_TTL_SECONDS:
            self.clear_history()
            return []
        return data

    def append_history(self, role: str, content: str) -> None:
        h = self.history
        h.append({"role": role, "content": content})
        self.set("conversation_history", h)
        self.set("conversation_history_ts", time.time())

    def clear_history(self) -> None:
        self.clear("conversation_history")
        self.clear("conversation_history_ts")

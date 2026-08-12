import sqlite3
import os

# AGENT_DB_PATH lets a caller point at a different database without editing
# code. No default is provided here — each consuming app sets its own path
# (e.g. donna.db, gretchen.db); a shared package has no single sensible
# default file to fall back to.
DB_PATH = os.environ.get("AGENT_DB_PATH")


def get_db():
    if not DB_PATH:
        raise RuntimeError("AGENT_DB_PATH is not set")
    # sqlite3.connect fails immediately if DB_PATH's parent directory doesn't
    # exist (e.g. a fresh container without a pre-created data/ dir) — this
    # used to crash-loop the whole app at import time (ClaudeAIScoutMaster#174).
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

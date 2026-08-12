# agent-toolkit

Shared Python infra for Tom Malley's agent fleet — currently Donna (personal
assistant) and Gretchen (Troop Assistant Scoutmaster), split out of
`ClaudeAIScoutMaster` (see ClaudeAIScoutMaster#276, #277). Meant to be reused
by future agents too, not scout- or Donna-specific.

## What's here (v0.1.0)

- `agent_toolkit.database` — SQLite connection helper. Reads its DB path from
  `AGENT_DB_PATH` (no default — each consumer points this at its own file,
  e.g. `donna.db` / `gretchen.db`). Raises `RuntimeError` if unset.
- `agent_toolkit.session` — per-chat durable session state (`Session` class),
  backed by a `sessions` table. **Consumers must create this table
  themselves** (schema below) — this package only reads/writes it, it does
  not own migrations:
  ```sql
  CREATE TABLE IF NOT EXISTS sessions (
      chat_id TEXT NOT NULL,
      key TEXT NOT NULL,
      value TEXT NOT NULL,
      updated_at TIMESTAMP DEFAULT (datetime('now')),
      PRIMARY KEY (chat_id, key)
  );
  ```
- `agent_toolkit.limiter` — shared `slowapi` rate limiter instance.
- `agent_toolkit.http_retry` — `@retry_http` decorator: retries only on
  transport-level failures (connection never completed), never on 5xx/generic
  timeouts for non-idempotent calls.
- `agent_toolkit.encryption` — Fernet-based field-level PII encryption
  (`enc`/`dec`), keyed by `DB_ENCRYPTION_KEY`. Falls back to plaintext with a
  one-time warning if the key isn't set; `dec()` tolerates pre-migration
  plaintext rows.

## Test-isolation pattern (recommended for consumers)

`ClaudeAIScoutMaster#265` — a test suite that doesn't redirect its DB path
away from production can write test fixtures into live data. Each consumer
repo should replicate this guard in its own `conftest.py`: redirect
`AGENT_DB_PATH` to a temp file for the whole test run, and additionally
monkeypatch `sqlite3.connect` to hard-fail if anything ever tries to open the
real production DB file by path. This package's own `tests/conftest.py` shows
the minimal version (path redirection only, since there is no "production
file" at the package level).

## Not here yet

Deliberately left out of the first extraction pass — either not yet fully
bot-agnostic in the source repo, or not yet audited:
- `llm_metrics.py` — has a hardcoded `AGENT = "donna"` constant tied to
  aiserver-stack#70's `/metrics` endpoint; needs generalizing to accept an
  agent name before it can be shared.
- `query_log.py` — writes to a `donna_tool_calls` table name; needs a
  parameterized table name (or per-consumer table) before extraction.
- Mailbox/API clients (`gmail_client`, `outlook_client`, `mailbox/`),
  MCP bridge clients (`icloud_mcp`, `calendar_mcp`, `imessage_mcp`,
  `reminders_mcp`), `memory.py` (aiserver Qdrant API client), and the
  Telegram single-user auth gate (`auth/telegram_gate.py`) are all
  candidates for a second extraction pass — tracked on
  ClaudeAIScoutMaster#277.

## Install

Not yet published. Point a consumer's `requirements.txt` / `pyproject.toml`
at the git URL until this is worth publishing to a private index:

```
agent-toolkit @ git+https://github.com/tmalley77/agent-toolkit.git@main
```

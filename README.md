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
- `agent_toolkit.auth` — single-user Telegram identity gate
  (`is_allowed_sender`, checks `TELEGRAM_ALLOWED_USER_ID` by default, or any
  env var name you pass) + `make_audit_log(logger_name, high_consequence_tools)`,
  which returns an `audit_log(update, tool_name)` bound to *your* logger name
  and *your* tool set — Donna's and Gretchen's high-consequence tools (and
  audit log streams) are expected to differ.
- `agent_toolkit.outlook_client` — Microsoft Graph API client for a single
  Outlook mailbox (fetch/search/send/reply/draft/forward, OAuth token
  refresh). One env var beyond the source it was ported from:
  `OUTLOOK_ENV_PATH` (default `.env` in the cwd) — the original located its
  env file via a path relative to its own source file, which breaks once
  this lives in an installed package.
- `agent_toolkit.gmail_client` — Gmail REST API client (fetch/search/send/
  reply/draft/quoted-reply, OAuth token refresh with cross-process file
  locking). `GMAIL_TOKEN_DIR` (default cwd) replaces the same kind of
  `__file__`-relative default-path bug `outlook_client` had. Quoted-reply
  rendering uses `agent_toolkit.email_html` (see below) instead of the
  source's `app.sdk_workers.email_draft`.
- `agent_toolkit.email_html` — pure HTML/markdown/plain-text conversion
  helpers with zero app dependencies (safe-subset HTML sanitizing, markdown
  fallback rendering, quoted-reply assembly, HTML-to-preview-text). Split
  out of ClaudeAIScoutMaster's `app/sdk_workers/email_draft.py`, which mixed
  these with heavily troop/Donna-coupled content generation — that split
  (ClaudeAIScoutMaster commit 634be3b) is what unblocked `gmail_client`.
- `agent_toolkit.mailbox` — `MailboxClient` ABC + `Email` dataclass +
  registry-based `get_mailbox()`/`register_mailbox()` factory, plus
  `agent_toolkit.mailbox.outlook.OutlookMailbox` and
  `agent_toolkit.mailbox.gmail.GmailMailbox`. One behavioral change from the
  ClaudeAIScoutMaster source: the original factory hardcoded the three
  mailbox names from Donna's pre-split setup (`outlook`/`troop_gmail`/
  `personal_gmail`); this one is registry-based instead, so each consumer
  registers only the mailbox names it actually owns
  (`register_mailbox("outlook", OutlookMailbox)`, etc.) — see the module
  docstring for the exact API. Also fixes a real bug found while porting the
  Outlook path (ClaudeAIScoutMaster#278): the source's 404-as-success
  handling checked `requests.exceptions.HTTPError`, but the client it wraps
  raises `httpx`'s exception type, so the check never matched and a message
  that was already gone was treated as a hard failure instead — covered by
  a regression test. (The Gmail path's equivalent check was already correct
  in the source — it uses `googleapiclient`'s own `HttpError` throughout,
  no mismatch there.) `GmailMailbox.forward()` carries over a real
  behavioral coupling from the source, not fixed: it forwards by sending
  through `OutlookMailbox` (Gmail has no API-level forward that preserves a
  prepended comment cleanly) — fine for Donna, who owns both, but a future
  Gmail-only consumer would need its own implementation. Flagged in the
  module docstring rather than silently redesigned.

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

Deliberately left out — either not yet fully bot-agnostic in the source
repo, or blocked on splitting a source file that mixes generic and
agent-specific code:
- `llm_metrics.py` — has a hardcoded `AGENT = "donna"` constant tied to
  aiserver-stack#70's `/metrics` endpoint; needs generalizing to accept an
  agent name before it can be shared.
- `query_log.py` — writes to a `donna_tool_calls` table name; needs a
  parameterized table name (or per-consumer table) before extraction.
- **`icloud_mcp.py`** (iMessage/Reminders/Calendar MCP bridge client) — same
  shape of blocker Gmail had: it bundles the generic MCP bridge core with a
  ScoutMasterHub-specific tool (the troop task board), so `calendar_mcp.py`/
  `imessage_mcp.py`/`reminders_mcp.py` (which all import from it) can't be
  cleanly extracted until that split happens too.
- `memory.py` (aiserver Qdrant client) — hardcodes `AGENT = "donna"` and
  `DEFAULT_PROJECT = "scoutmaster"` at module level. Generalizing this one
  isn't pure mechanical extraction — it's the actual implementation of the
  `agent="gretchen"` / `project="personal"` memory-split decision from
  ClaudeAIScoutMaster#277, so it's deferred to that work rather than done
  here as infra housekeeping.

All tracked on ClaudeAIScoutMaster#277.

## Install

Not yet published. Point a consumer's `requirements.txt` / `pyproject.toml`
at the git URL until this is worth publishing to a private index:

```
agent-toolkit @ git+https://github.com/tmalley77/agent-toolkit.git@main
```

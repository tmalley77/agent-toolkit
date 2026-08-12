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
- `agent_toolkit.query_log` — durable log of every question asked
  (`log_query(chat_id, question, lane, status, duration_ms)`, never raises).
  No generalizing needed beyond the `agent_toolkit.database` swap — it
  already wrote to a plain `query_log` table with no agent name baked in;
  the source's neighboring `donna_tool_calls` table (a different, still
  Donna-hardcoded log — see "Not here yet") is written by `app/ai/ai_tools.py`,
  not this module, despite a docstring mention that made it look coupled.
- `agent_toolkit.llm_metrics` — cross-process Prometheus-format `/metrics`
  counters (tokens/calls/duration histograms) via Redis, for exposing an
  agent's own LLM call volume. Real fix here, not just a rename: the source
  hardcoded `AGENT = "donna"` as a *render-time label only* — the
  underlying Redis keys were fixed strings (`llm_metrics:tokens`, etc.), so
  a second agent sharing the same Redis (the default `REDIS_URL` has no
  per-agent override) would have its counters silently merged into the
  first agent's, then reported entirely under the first agent's label.
  `METRICS_AGENT` (required, no default — same posture as `AGENT_DB_PATH`)
  is now baked into the storage keys themselves
  (`llm_metrics:{agent}:tokens`), not just the output. Regression-tested:
  two agents recording through the same fake Redis produce non-overlapping
  `render()` output.
- `agent_toolkit.memory_client` — HTTP client for aiserver's shared semantic
  memory API (search/store/delete, HOA search, camp recipes, event
  debriefs, Ollama-based reranking). **This one is the actual
  implementation of the `agent="gretchen"` / `project="personal"`
  memory-split decision from ClaudeAIScoutMaster#277**, not routine infra
  housekeeping — `MEMORY_AGENT`/`MEMORY_DEFAULT_PROJECT` (both required, no
  default) replace the source's hardcoded `AGENT = "donna"` /
  `DEFAULT_PROJECT = "scoutmaster"`. No storage-key collision risk to fix
  here unlike `llm_metrics.py` — `agent`/`project` were already sent as
  explicit fields on every request to a server that already partitions by
  them; only the constants feeding those fields needed to become
  configurable. Two things deliberately kept as hardcoded literals, not
  parameterized: `agent="harvey"` in the `*_shared_memory` functions
  (Harvey's own synced notes — a fixed resource, not per-consumer) and
  `project="hoa_westmoreland"` in the HOA functions (Donna-only domain per
  #277, no reason to make it configurable for a domain only one consumer
  has). One deliberate value change: the source's reranker's `OLLAMA_URL`
  default was a specific Tailscale IP baked into ClaudeAIScoutMaster's own
  infra; replaced with a generic `http://localhost:11434` default here,
  since a shared package hardcoding one consumer's specific host makes no
  sense — every real consumer should set `OLLAMA_URL` explicitly anyway.
  **Not wired into any live consumer** — porting this does not touch
  donna-workspace's live `app/memory.py`, which still hardcodes AGENT/
  DEFAULT_PROJECT today; that swap is a real production behavior change
  (every memory-reliant feature routes through it) and belongs with the
  actual Donna/Gretchen cutover, not bundled into an extraction pass.

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
- **`icloud_mcp.py`** (iMessage/Reminders/Calendar MCP bridge client) —
  **correction (2026-08-12): not actually a Phase 1 blocker.** An earlier
  pass here flagged this as bundling the generic MCP bridge core with a
  ScoutMasterHub-specific tool; reading the full file shows that's wrong —
  the only ScoutMasterHub mention is one line of *prompt text* telling the
  LLM not to confuse Apple Reminders with the troop task board, not
  embedded tool logic. The real reason this isn't ported: per
  ClaudeAIScoutMaster#277's decisions, Donna keeps *sole* ownership of
  iMessage/Calendar/Reminders — Gretchen has zero duties here — so there is
  no second consumer to extract this for. Revisit only if a future agent
  actually needs one of these MCP bridges.
- `app/ai/ai_tools.py`'s `donna_tool_calls` logging — tied up in the
  monolithic tool-catalog file itself (ClaudeAIScoutMaster#277 blocker #2),
  not a standalone extraction candidate.

All tracked on ClaudeAIScoutMaster#277.

## Install

Not yet published. Point a consumer's `requirements.txt` / `pyproject.toml`
at the git URL until this is worth publishing to a private index:

```
agent-toolkit @ git+https://github.com/tmalley77/agent-toolkit.git@main
```

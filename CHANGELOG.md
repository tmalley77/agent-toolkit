# Changelog

Five repos install this package — donna-workspace, aiserver-stack's `donna` and
`gretchen` service builds, WestmorelandFamsHOA and WateronDemand. Until
2026-09-20 every one of them pinned `@main`, so "which version is Donna running"
had no answer. This file exists so it does.

Versions are tags (`vX.Y.Z`) matching `version` in `pyproject.toml`; CI refuses a
tag that does not. Consumers pin the tag, never `main`.

While this package is pre-1.0, a breaking change bumps the **minor** version.

## Unreleased

## v0.2.0 — 2026-09-20

The first tagged release. `main` had moved eight commits past the `v0.1.0` the
README described, with no way for a consumer to say which of them it had.

Since v0.1.0:

- `memory_client`: shared HTTP client timeout 30s → 90s. Embedding runs on the
  CPU since bge-m3 was pinned off the GPU, and 30s was cutting off legitimate
  requests (PR #1).
- `memory_client`: added `search_handbook()` — the ingested Scout Handbook
  carries no `memory_type`, so the generic search could not reach it.
- `memory_client`: rerank runs on the resident model and never thinks. A
  thinking model behind a short timeout is a silent no-op.
- `memory_client`: removed `search_donna_scoutmaster_memory()`
  (gretchen-workspace#10).
- `gmail_imap`: IMAP/SMTP app-password backend, dispatched per account. Google
  expires OAuth refresh tokens after 7 days for unverified apps requesting
  restricted Gmail scopes; an app password has no such fuse
  (donna-workspace#311).
- `outlook_client`: the requested scope set is per-consumer
  (aiserver-stack#142).
- `memory_client`: `MEMORY_DEFAULT_PROJECT` is optional, not required.

Infrastructure:

- CI runs the 114-test suite on push and pull request. Nothing ran it on a push
  before (donna-workspace#361 phase 0).

## v0.1.0

Initial extraction from ClaudeAIScoutMaster (#276, #277): `database`, `session`,
`http_retry`, `encryption`, `auth`, `limiter`, `query_log`, `llm_metrics`,
`mailbox`, `gmail_client`, `outlook_client`, `email_html`, `memory_client`.

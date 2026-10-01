# Changelog

Five repos install this package — donna-workspace, aiserver-stack's `donna` and
`gretchen` service builds, WestmorelandFamsHOA and WateronDemand. Until
2026-09-20 every one of them pinned `@main`, so "which version is Donna running"
had no answer. This file exists so it does.

Versions are tags (`vX.Y.Z`) matching `version` in `pyproject.toml`; CI refuses a
tag that does not. Consumers pin the tag, never `main`.

While this package is pre-1.0, a breaking change bumps the **minor** version.

## Unreleased

## v0.3.0 — 2026-10-01

Minor bump because the refresh-token precedence changes for all five consumers:
the rotation store now wins over `OUTLOOK_REFRESH_TOKEN` in the environment.

- `outlook_client._update_env()`: **creates the rotation store instead of
  skipping it.** It used to return early when the path did not exist, reading
  that as "env vars came purely from the process environment" — which is
  exactly how Donna is configured (compose `env_file:`, nothing bind-mounted,
  `.env` excluded by `.dockerignore`). Every refresh token Microsoft rotated
  was therefore dropped on the floor, the original grant stood untouched for
  the life of the deployment, and the credential only ever changed when it was
  revoked (donna-workspace#373). Same bug class as donna-workspace#109, which
  fixed one code path and left this one.
- A store that cannot be written now logs a warning naming `OUTLOOK_ENV_PATH`
  instead of failing silently. It still does not raise: the access token in
  hand is good, and taking mail down over a failed write is worse than the
  stale store it leaves behind.
- `outlook_client._get_access_token()`: reads the refresh token from the store
  first, falling back to `OUTLOOK_REFRESH_TOKEN` once on `invalid_grant`. The
  fallback is the re-mint path — `get_outlook_token.py` writes the env file,
  not the store, so after a revocation the store holds the revoked value and
  only the environment has a good one. Without it the store would have to be
  deleted by hand after every re-consent.
- Non-auth token-endpoint errors (500s) no longer trigger that fallback, and a
  store equal to the env seed is not redeemed twice.
- The single-key rewrite now substitutes via a callable, so a literal
  backslash in a credential can no longer be read as a group reference.

**Consumers must point `OUTLOOK_ENV_PATH` at a writable, persistent
directory.** A single-file bind mount cannot host the store: the atomic
replace needs its temp file in the target's own directory, and under a
file mount that directory is inside the container, so the write never
reaches the host. Mount a directory.

## v0.2.5 — 2026-09-30

- `gmail_client`/`gmail_imap`: `fetch_rfc822()` (full raw message bytes, every
  MIME part intact) and `send_mime_message()` (send a caller-built MIME
  message verbatim) — the Smoke Signals relay re-sends the council newsletter
  with its inline images preserved (gretchen-workspace#31).

## v0.2.4 — 2026-09-30

- `outlook_client.move_to_folder()`: folder names are now ALWAYS literal
  displayNames, never path-split (Tom, donna-workspace#371: "just use
  Scouting/General only to prevent overlap"). v0.2.3's path-split fallback
  could recreate a nested Scouting→General tree beside the real slashed-name
  folder on a lookup miss; a missing slashed name is now created literally.

## v0.2.3 — 2026-09-30

- `outlook_client.move_to_folder()`: try a slashed name as a LITERAL top-level
  displayName before path-splitting — Outlook allows "/" in folder names and
  Tom's mailbox has a real top-level "Scouting/General"; v0.2.2's path
  resolution alone would have recreated a nested tree beside it.

## v0.2.2 — 2026-09-30

- `outlook_client`: `add_category()` / `remove_category()` — tag a message by
  category name without touching folder or read state (donna-workspace#371's
  Outlook "Action Required"; works with the mail scope alone — no
  MailboxSettings needed).
- `outlook_client`: `move_to_folder()` now resolves "Parent/Child" paths the
  way `list_folders()` renders them. The old top-level-only lookup missed
  nested folders and silently created a bogus top-level folder with the
  slashed name.

## v0.2.1 — 2026-09-29

- `gmail_client`/`gmail_imap`: `add_label()` / `remove_label()` — tag a message
  without archiving or marking it read (donna-workspace#370's "Action Required"
  needs the message to stay in the inbox; `apply_label` files it away).
- `gmail_client`/`gmail_imap`: `purge_label_older_than()` — trash everything
  under a label older than N days (the Promotions 30-day purge).

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

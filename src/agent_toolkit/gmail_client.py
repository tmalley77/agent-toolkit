"""Gmail REST API client — OAuth-based.

Ported from ClaudeAIScoutMaster's app/gmail_client.py (see
ClaudeAIScoutMaster#277). Each consumer sets its own token env var(s), e.g.:
  GMAIL_TOKEN_TROOP    -> path to a troop-mailbox OAuth token file
  GMAIL_TOKEN_PERSONAL -> path to a personal-mailbox OAuth token file
`fetch_unread`/`search_messages`/etc. all take a `token_env` argument naming
which env var holds (or defaults to) the token file path — there is no
built-in "troop"/"personal" concept here, that's a consumer-level choice.

Two changes from the source: (1) `GMAIL_TOKEN_DIR` (default: cwd) replaces
a `__file__`-relative path for resolving a relative default token path —
the original located it via a path into its own repo, which breaks once
this lives in an installed package. (2) the HTML-quoting helpers it needs
for `send_message`/`create_quoted_reply_draft` come from
`agent_toolkit.email_html` instead of the source's `app.sdk_workers.email_draft`
(itself split for this exact reason, ClaudeAIScoutMaster#277).
"""
import base64
import fcntl
import html
import logging
import os
import re
from email import message_from_bytes
from email.header import decode_header as _decode_header
from email.policy import compat32
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

_DEFAULT_TOKEN_PATHS = {
    "GMAIL_TOKEN_TROOP": "data/gmail_token_troop.json",
    "GMAIL_TOKEN_PERSONAL": "data/gmail_token_personal.json",
}


def _token_dir() -> Path:
    return Path(os.getenv("GMAIL_TOKEN_DIR", "."))


def _get_service(token_env: str = "GMAIL_TOKEN_TROOP"):
    token_path = os.getenv(token_env)
    if not token_path:
        token_path = _DEFAULT_TOKEN_PATHS.get(token_env)
    if not token_path:
        raise RuntimeError(f"{token_env} not set and no default path")

    if not os.path.isabs(token_path):
        token_path = str(_token_dir() / token_path)

    if not os.path.exists(token_path):
        raise RuntimeError(
            f"Gmail token not found at {token_path}. Run your OAuth setup script for this account."
        )

    # Multiple processes (e.g. a bot process, a scheduler, a worker) can share
    # this token file (bind mount, no other coordination) and each
    # independently reads-refreshes-writes it on every call. Without a lock,
    # two processes can both see an expired token and both call Google's
    # refresh endpoint at once; Google honors both but the loser's write
    # clobbers the winner's, and the token left on disk can end up one that's
    # already been superseded -- invalid_grant on the next use, hours later,
    # with no obvious trigger (ClaudeAIScoutMaster#259). An flock on a
    # sidecar lock file serializes the whole read-check-refresh-write
    # section: whoever loses the race blocks, then re-reads the file the
    # winner already refreshed and finds it no longer expired, so it never
    # calls refresh() itself.
    lock_path = f"{token_path}.lock"
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            creds = Credentials.from_authorized_user_file(token_path)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                # Atomic write on top of the lock, not instead of it: a
                # concurrent *reader* outside this function (nothing today,
                # but don't assume forever) should never see a half-written
                # file either.
                tmp_path = f"{token_path}.tmp"
                Path(tmp_path).write_text(creds.to_json())
                os.replace(tmp_path, token_path)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    parts = _decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def _strip_html(raw: str) -> str:
    """Convert HTML to readable plain text, stripping style/script blocks."""
    text = re.sub(r"<(style|script|head)[^>]*>.*?</\1>", "", raw, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<(br|p|tr|h[1-6]|li|div)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


def _extract_body_from_payload(payload: dict) -> str:
    """Extract plain text body from Gmail API message payload."""
    mime_type = payload.get("mimeType", "")
    parts = payload.get("parts", [])

    if mime_type == "text/plain" and "body" in payload:
        data = payload["body"].get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    if parts:
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for part in parts:
            if part.get("mimeType") == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    raw = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    return _strip_html(raw)
            if part.get("mimeType", "").startswith("multipart/"):
                nested = _extract_body_from_payload(part)
                if nested:
                    return nested

    if mime_type == "text/html" and "body" in payload:
        data = payload["body"].get("data", "")
        if data:
            raw = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return _strip_html(raw)

    return ""


def _get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def list_labels(token_env: str = "GMAIL_TOKEN_TROOP") -> list[str]:
    """Return all user-created label names (excludes system labels)."""
    service = _get_service(token_env)
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    return sorted(
        lbl["name"] for lbl in labels
        if lbl.get("type") == "user"
    )


def _parse_message(service, msg_id: str) -> dict:
    """Fetch and parse a single Gmail message into the common email dict shape."""
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()

    headers = msg.get("payload", {}).get("headers", [])
    sender_raw = _get_header(headers, "From")
    m = re.search(r"<([^>]+)>", sender_raw)
    sender_email = m.group(1) if m else sender_raw.strip()
    sender_name = re.sub(r"\s*<[^>]+>", "", sender_raw).strip().strip('"')

    body = _extract_body_from_payload(msg.get("payload", {}))

    return {
        "uid": msg_id,
        "thread_id": msg.get("threadId", ""),
        "message_id": _get_header(headers, "Message-ID"),
        "subject": _get_header(headers, "Subject") or "(no subject)",
        "sender": sender_name or sender_email,
        "sender_email": sender_email,
        "date": _get_header(headers, "Date"),
        "body": body[:4000],
    }


def fetch_unread(
    limit: int = 10,
    token_env: str = "GMAIL_TOKEN_TROOP",
) -> list[dict]:
    """Fetch up to `limit` unread emails. Returns same dict format as IMAP client."""
    service = _get_service(token_env)

    resp = service.users().messages().list(
        userId="me",
        q="is:unread in:inbox",
        maxResults=limit,
    ).execute()

    message_ids = [m["id"] for m in resp.get("messages", [])]
    if not message_ids:
        return []

    return [_parse_message(service, msg_id) for msg_id in message_ids]


def search_messages(
    keyword: str,
    sender: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 15,
    token_env: str = "GMAIL_TOKEN_TROOP",
) -> list[dict]:
    """Search mail history (Inbox + Archive/All Mail + Sent, not Trash/Spam) by
    keyword, newest-first. `since`/`until` are "YYYY-MM-DD" strings. Gmail's
    default `q=` search scope already excludes Trash/Spam, so no extra folder
    qualifiers are needed."""
    service = _get_service(token_env)

    q_parts = [keyword]
    if sender:
        q_parts.append(f"from:{sender}")
    if since:
        q_parts.append(f"after:{since.replace('-', '/')}")
    if until:
        q_parts.append(f"before:{until.replace('-', '/')}")
    q = " ".join(q_parts)

    resp = service.users().messages().list(
        userId="me",
        q=q,
        maxResults=limit,
    ).execute()

    message_ids = [m["id"] for m in resp.get("messages", [])]
    if not message_ids:
        return []

    return [_parse_message(service, msg_id) for msg_id in message_ids]


def mark_read(msg_id: str, token_env: str = "GMAIL_TOKEN_TROOP") -> None:
    """Remove UNREAD label from a message."""
    service = _get_service(token_env)
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()


def archive_message(msg_id: str, token_env: str = "GMAIL_TOKEN_TROOP") -> None:
    """Archive a message (remove from inbox)."""
    service = _get_service(token_env)
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"removeLabelIds": ["INBOX"]},
    ).execute()


def trash_message(msg_id: str, token_env: str = "GMAIL_TOKEN_TROOP") -> None:
    """Move a message to trash."""
    service = _get_service(token_env)
    service.users().messages().trash(userId="me", id=msg_id).execute()


def apply_label(msg_id: str, label_name: str, token_env: str = "GMAIL_TOKEN_TROOP") -> None:
    """Apply a label to a message and remove from inbox. Creates label if it doesn't exist."""
    service = _get_service(token_env)

    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    label_id = None
    for lbl in labels:
        if lbl["name"].lower() == label_name.lower():
            label_id = lbl["id"]
            break

    if not label_id:
        created = service.users().labels().create(
            userId="me", body={"name": label_name, "labelListVisibility": "labelShow"},
        ).execute()
        label_id = created["id"]

    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"addLabelIds": [label_id], "removeLabelIds": ["INBOX", "UNREAD"]},
    ).execute()


def send_message(
    to_address: str,
    subject: str,
    body: str,
    token_env: str = "GMAIL_TOKEN_TROOP",
    in_reply_to: str | None = None,
    content_type: str = "html",
    thread_id: str | None = None,
    references: str | None = None,
) -> None:
    """Send an email via Gmail API.

    HTML sends go as multipart/alternative so plain-text clients still get
    readable copy.
    """
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    service = _get_service(token_env)

    profile = service.users().getProfile(userId="me").execute()
    from_addr = profile.get("emailAddress", "")

    if content_type == "html":
        from agent_toolkit.email_html import html_to_preview_text

        msg = MIMEMultipart("alternative")
        # Least-preferred part first, per RFC 2046 — clients pick the last one
        # they can render.
        msg.attach(MIMEText(html_to_preview_text(body), "plain"))
        msg.attach(MIMEText(body, "html"))
    else:
        msg = MIMEText(body, content_type)
    msg["To"] = to_address
    msg["From"] = from_addr
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        # References should be the accumulated chain, not just the parent —
        # overwriting it with the parent alone degrades threading in strict
        # clients on deep threads. Callers that have the original References
        # header pass it through; we append the parent to it.
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    # Headers alone don't place a sent message in an existing Gmail
    # conversation — the API needs threadId in the request body.
    send_body = {"raw": raw}
    if thread_id:
        send_body["threadId"] = thread_id
    service.users().messages().send(
        userId="me", body=send_body
    ).execute()


def create_draft(
    to_address: str,
    subject: str,
    body: str,
    token_env: str = "GMAIL_TOKEN_TROOP",
    content_type: str = "html",
    thread_id: str | None = None,
    in_reply_to: str | None = None,
) -> str:
    """Create a reply draft in Gmail (saved to Drafts folder, not sent).

    Without threadId and In-Reply-To the saved draft is a standalone
    conversation rather than a reply on the original thread.
    """
    from email.mime.text import MIMEText

    service = _get_service(token_env)
    profile = service.users().getProfile(userId="me").execute()
    from_addr = profile.get("emailAddress", "")

    msg = MIMEText(body, content_type)
    msg["To"] = to_address
    msg["From"] = from_addr
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    message: dict = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    result = service.users().drafts().create(
        userId="me", body={"message": message}
    ).execute()
    return result.get("id", "")


def _extract_html_from_payload(payload: dict) -> str:
    """Return the message's `text/html` part *unstripped*, or "" if it is
    plain-text only.

    Deliberately not `_extract_body_from_payload()`, which prefers text/plain
    and runs `_strip_html()` over anything else — right for feeding a model,
    wrong for quoting, where the original's own markup is the whole point.
    """
    if payload.get("mimeType") == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        if part.get("mimeType", "").startswith("multipart/"):
            nested = _extract_html_from_payload(part)
            if nested:
                return nested
    return ""


def create_quoted_reply_draft(
    source_msg_id: str,
    body_html: str,
    token_env: str = "GMAIL_TOKEN_TROOP",
) -> str:
    """Create a reply draft quoting `source_msg_id` with its markup intact.

    Gmail has no server-side `createReply` (Graph's does this for us), so the
    quote is assembled here from the original's own `text/html` part, and the
    draft is threaded via threadId/In-Reply-To/References.
    """
    from email.mime.text import MIMEText

    service = _get_service(token_env)
    src = service.users().messages().get(
        userId="me", id=source_msg_id, format="full"
    ).execute()
    payload = src.get("payload", {})
    headers = payload.get("headers", [])

    sender = _get_header(headers, "From")
    date = _get_header(headers, "Date")
    subject = _get_header(headers, "Subject") or "(no subject)"
    message_id = _get_header(headers, "Message-ID")
    references = _get_header(headers, "References")

    from agent_toolkit.email_html import build_quoted_reply_html, plain_text_to_quoted_html

    original_html = _extract_html_from_payload(payload)
    if not original_html:
        # Plain-text-only original: escape it so it survives an HTML body, and
        # keep its line breaks rather than letting them collapse.
        original_html = plain_text_to_quoted_html(_extract_body_from_payload(payload))

    quoted = build_quoted_reply_html(
        body_html, original_html, f"On {date}, {sender} wrote:"
    )

    profile = service.users().getProfile(userId="me").execute()
    msg = MIMEText(quoted, "html")
    msg["To"] = sender
    msg["From"] = profile.get("emailAddress", "")
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if message_id:
        msg["In-Reply-To"] = message_id
        msg["References"] = f"{references} {message_id}".strip() if references else message_id

    message: dict = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if src.get("threadId"):
        message["threadId"] = src["threadId"]
    result = service.users().drafts().create(
        userId="me", body={"message": message}
    ).execute()
    return result.get("id", "")


def create_draft_new(
    to_address: str,
    subject: str,
    body: str,
    token_env: str = "GMAIL_TOKEN_TROOP",
    content_type: str = "html",
) -> str:
    """Create a fresh (non-reply) draft in Gmail — no 'Re:' prefix forced, unlike
    create_draft(). Returns draft_id."""
    from email.mime.text import MIMEText

    service = _get_service(token_env)
    profile = service.users().getProfile(userId="me").execute()
    from_addr = profile.get("emailAddress", "")

    msg = MIMEText(body, content_type)
    msg["To"] = to_address
    msg["From"] = from_addr
    msg["Subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()
    return result.get("id", "")


def list_drafts(limit: int = 20, token_env: str = "GMAIL_TOKEN_TROOP") -> list[dict]:
    """List up to `limit` drafts sitting unsent in this mailbox's Drafts
    folder, newest-modified first. Returns [{"to","subject","last_modified"}]."""
    from datetime import datetime, timezone

    service = _get_service(token_env)
    resp = service.users().drafts().list(userId="me", maxResults=limit).execute()
    stubs = resp.get("drafts", [])
    results = []
    for stub in stubs:
        draft = service.users().drafts().get(
            userId="me", id=stub["id"], format="metadata",
            metadataHeaders=["Subject", "To"],
        ).execute()
        headers = draft.get("message", {}).get("payload", {}).get("headers", [])
        internal_date_ms = draft.get("message", {}).get("internalDate", "")
        last_modified = ""
        if internal_date_ms:
            last_modified = datetime.fromtimestamp(
                int(internal_date_ms) / 1000, tz=timezone.utc
            ).isoformat()
        results.append({
            "to": _get_header(headers, "To") or "(no recipient)",
            "subject": _get_header(headers, "Subject") or "(no subject)",
            "last_modified": last_modified,
        })
    results.sort(key=lambda d: d["last_modified"], reverse=True)
    return results


def send_draft(draft_id: str, token_env: str = "GMAIL_TOKEN_TROOP") -> None:
    """Send an existing Gmail draft by ID — sends whatever is currently saved."""
    service = _get_service(token_env)
    service.users().drafts().send(
        userId="me", body={"id": draft_id}
    ).execute()


def update_draft(
    draft_id: str,
    to_address: str,
    subject: str,
    body: str,
    token_env: str = "GMAIL_TOKEN_TROOP",
    content_type: str = "html",
) -> None:
    """Overwrite an existing Gmail draft's content in place."""
    from email.mime.text import MIMEText

    service = _get_service(token_env)
    profile = service.users().getProfile(userId="me").execute()
    from_addr = profile.get("emailAddress", "")

    msg = MIMEText(body, content_type)
    msg["To"] = to_address
    msg["From"] = from_addr
    msg["Subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().drafts().update(
        userId="me", id=draft_id, body={"message": {"raw": raw}}
    ).execute()

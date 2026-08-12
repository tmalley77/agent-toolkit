"""
Outlook client for a single mailbox via Microsoft Graph API + OAuth2.

Authentication: exchange a one-time-obtained refresh token for short-lived
access tokens on each use; the on-disk env file is updated if Microsoft
rotates the refresh token.

Required env vars (per consumer):
  OUTLOOK_ADDRESS       — the mailbox address
  OUTLOOK_CLIENT_ID     — from Azure app registration
  OUTLOOK_REFRESH_TOKEN — from a one-time OAuth setup script

Optional:
  OUTLOOK_ENV_PATH — path to the .env file to rewrite when the refresh
    token rotates. Defaults to ".env" in the current working directory.
    (Ported from ClaudeAIScoutMaster's app/outlook_client.py, which located
    this via a `__file__`-relative path into its own repo root — that
    breaks once this module lives in an installed package, hence the env
    var. See ClaudeAIScoutMaster#277.)
"""
import os
import re
import html
import httpx
from datetime import datetime
from typing import Optional

from agent_toolkit.http_retry import retry_http

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
SCOPES = "https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/Mail.Send https://graph.microsoft.com/Notes.Read offline_access"

# Defense-in-depth: re-validate at the send boundary so a malformed address
# (or a name accidentally passed as the address) can't ever reach Graph.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _require_valid_email(addr: str, context: str) -> None:
    if not addr or not isinstance(addr, str) or not _EMAIL_RE.match(addr):
        raise ValueError(f"{context}: refused to send — invalid email address {addr!r}")


def _cfg() -> tuple[str, str, str]:
    addr = os.getenv("OUTLOOK_ADDRESS", "")
    client_id = os.getenv("OUTLOOK_CLIENT_ID", "")
    refresh_token = os.getenv("OUTLOOK_REFRESH_TOKEN", "")
    if not addr or not client_id or not refresh_token:
        raise RuntimeError(
            "OUTLOOK_ADDRESS, OUTLOOK_CLIENT_ID, and OUTLOOK_REFRESH_TOKEN must be set."
        )
    return addr, client_id, refresh_token


@retry_http
def _get_access_token() -> str:
    """Exchange the refresh token for a short-lived access token.
    If Microsoft rotates the refresh token, updates the env file automatically."""
    _, client_id, refresh_token = _cfg()
    r = httpx.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": SCOPES,
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    access_token = data["access_token"]

    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        _update_env("OUTLOOK_REFRESH_TOKEN", new_refresh)
        os.environ["OUTLOOK_REFRESH_TOKEN"] = new_refresh

    return access_token


def _update_env(key: str, value: str) -> None:
    """Update a single key in the env file without disturbing other lines.

    Uses atomic write (temp file + os.replace) to prevent corruption if the
    process is killed mid-write. No-ops if the file doesn't exist (e.g. env
    vars supplied purely via the process environment).
    """
    env_path = os.getenv("OUTLOOK_ENV_PATH", ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r") as f:
        content = f.read()
    pattern = rf"^{re.escape(key)}=.*$"
    replacement = f"{key}={value}"
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n{replacement}\n"
    tmp_path = env_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, env_path)


def _html_to_text(raw: str) -> str:
    text = re.sub(r"<(style|script|head)[^>]*>.*?</\1>", "", raw, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<(br|p|tr|h[1-6]|li|div)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


@retry_http
def list_folders() -> list[str]:
    """Return all user-created mail folder display names (excludes system folders)."""
    token = _get_access_token()
    system_names = {"Inbox", "Drafts", "Sent Items", "Deleted Items", "Junk Email",
                    "Archive", "Conversation History", "Outbox", "RSS Subscriptions",
                    "Clutter", "Sync Issues"}
    result = []
    url = f"{GRAPH}/me/mailFolders?$top=100"
    while url:
        r = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        for f in data.get("value", []):
            name = f.get("displayName", "")
            if not name or name in system_names:
                continue
            result.append(name)
            child_url = f"{GRAPH}/me/mailFolders/{f['id']}/childFolders?$top=100"
            cr = httpx.get(child_url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if cr.status_code == 200:
                for cf in cr.json().get("value", []):
                    cname = cf.get("displayName", "")
                    if cname:
                        result.append(f"{name}/{cname}")
        url = data.get("@odata.nextLink")
    return sorted(result)


def _parse_message(m: dict) -> dict:
    """Parse a raw Graph message object into the common email dict shape."""
    sender_info = m.get("from", {}).get("emailAddress", {})
    body_obj = m.get("body", {})
    raw_body = body_obj.get("content", "")
    if body_obj.get("contentType", "").lower() == "html":
        body_text = _html_to_text(raw_body)
    else:
        body_text = raw_body
    return {
        "uid": m["id"],
        "thread_id": m.get("conversationId", ""),
        "message_id": m.get("internetMessageId", ""),
        "subject": m.get("subject", "(no subject)"),
        "sender": sender_info.get("name", sender_info.get("address", "")),
        "sender_email": sender_info.get("address", ""),
        "date": m.get("receivedDateTime", "")[:16].replace("T", " "),
        "body": body_text[:4000],
    }


@retry_http
def fetch_unread(limit: int = 10) -> list[dict]:
    """
    Fetch up to `limit` unread emails from the Outlook inbox via Graph API.
    Returns list of dicts: uid, subject, sender, sender_email, date, body, message_id.
    Does NOT mark messages as read.
    """
    token = _get_access_token()
    params = {
        "$filter": "isRead eq false",
        "$top": limit,
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,receivedDateTime,body,internetMessageId,conversationId",
    }
    r = httpx.get(
        f"{GRAPH}/me/mailFolders/Inbox/messages",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=20,
    )
    r.raise_for_status()
    messages = r.json().get("value", [])
    return [_parse_message(m) for m in messages]


_SEARCH_FOLDERS = ("Inbox", "Archive", "SentItems")


@retry_http
def search_messages(
    keyword: str,
    sender: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 15,
) -> list[dict]:
    """Search mail history (Inbox + Archive + Sent, not Deleted/Junk) by
    keyword, newest-first. `since`/`until` are "YYYY-MM-DD" strings. Uses
    Graph's $search (KQL) per folder — $search cannot be combined with
    $orderby, so results from all three folders are merged and sorted in
    Python instead."""
    token = _get_access_token()

    kql_parts = [f'"{keyword}"']
    if sender:
        kql_parts.append(f"from:{sender}")
    if since and until:
        kql_parts.append(f"received:{since}..{until}")
    elif since:
        kql_parts.append(f"received>={since}")
    elif until:
        kql_parts.append(f"received<={until}")
    kql = " AND ".join(kql_parts)

    results = []
    for folder in _SEARCH_FOLDERS:
        # Fetch more than `limit` per folder before merging: Graph's $search ranks by
        # relevance (not date) and can't combine with $orderby, so a folder with more
        # than `limit` matches could otherwise have its true-newest message(s) excluded
        # by the relevance cutoff before the cross-folder date-sort below ever sees them.
        params = {
            "$search": kql,
            "$top": min(limit * 4, 50),
            "$select": "id,subject,from,receivedDateTime,body,internetMessageId,conversationId",
        }
        r = httpx.get(
            f"{GRAPH}/me/mailFolders/{folder}/messages",
            headers={"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"},
            params=params,
            timeout=20,
        )
        r.raise_for_status()
        results.extend(_parse_message(m) for m in r.json().get("value", []))

    results.sort(key=lambda d: d["date"], reverse=True)
    return results[:limit]


@retry_http
def mark_read(uid: str) -> None:
    """Mark a message as read by Graph message ID."""
    token = _get_access_token()
    httpx.patch(
        f"{GRAPH}/me/messages/{uid}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"isRead": True},
        timeout=10,
    ).raise_for_status()


@retry_http
def send_reply(
    to_address: str,
    subject: str,
    body: str,
    in_reply_to: Optional[str] = None,
    content_type: str = "HTML",
) -> None:
    """Send a reply from the configured mailbox via Graph API sendMail."""
    _require_valid_email(to_address, "send_reply")
    token = _get_access_token()
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    payload = {
        "message": {
            "subject": reply_subject,
            "body": {"contentType": content_type, "content": body},
            "toRecipients": [{"emailAddress": {"address": to_address}}],
        },
        "saveToSentItems": True,
    }
    httpx.post(
        f"{GRAPH}/me/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    ).raise_for_status()


@retry_http
def create_draft_reply(
    to_address: str,
    subject: str,
    body: str,
    content_type: str = "HTML",
) -> str:
    """Create a draft reply in Outlook (saved to Drafts folder, not sent)."""
    _require_valid_email(to_address, "create_draft_reply")
    token = _get_access_token()
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    payload = {
        "subject": reply_subject,
        "body": {"contentType": content_type, "content": body},
        "toRecipients": [{"emailAddress": {"address": to_address}}],
    }
    r = httpx.post(
        f"{GRAPH}/me/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("id", "")


@retry_http
def create_draft_new(to_address: str, subject: str, body: str, content_type: str = "HTML") -> str:
    """Create a fresh (non-reply) draft in Outlook — no 'Re:' prefix forced, unlike
    create_draft_reply(). Returns the message id."""
    _require_valid_email(to_address, "create_draft_new")
    token = _get_access_token()
    payload = {
        "subject": subject,
        "body": {"contentType": content_type, "content": body},
        "toRecipients": [{"emailAddress": {"address": to_address}}],
    }
    r = httpx.post(
        f"{GRAPH}/me/messages",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("id", "")


# Never a real actionable draft, even though Graph's isDraft flag stays set:
# deleting a draft moves it to Deleted Items but does NOT clear isDraft, and
# Sync Issues/Conflicts is an auto-generated system folder for sync
# conflict remnants (confirmed live against a real mailbox, ClaudeAIScoutMaster#261).
_EXCLUDED_DRAFT_FOLDER_NAMES = {"deleted items", "junk email", "sync issues"}


def _excluded_draft_folder_ids(token: str) -> set[str]:
    """Folder ids for _EXCLUDED_DRAFT_FOLDER_NAMES and their children (e.g.
    Sync Issues/Conflicts) — never raises; an empty set just means no
    filtering happens (list_drafts falls back to unfiltered rather than
    failing the whole call over this)."""
    try:
        r = httpx.get(
            f"{GRAPH}/me/mailFolders",
            headers={"Authorization": f"Bearer {token}"},
            params={"$top": 50, "$select": "id,displayName"},
            timeout=20,
        )
        r.raise_for_status()
        excluded_ids = set()
        for f in r.json().get("value", []):
            if f.get("displayName", "").lower() not in _EXCLUDED_DRAFT_FOLDER_NAMES:
                continue
            excluded_ids.add(f["id"])
            cr = httpx.get(
                f"{GRAPH}/me/mailFolders/{f['id']}/childFolders",
                headers={"Authorization": f"Bearer {token}"},
                params={"$top": 50, "$select": "id"},
                timeout=20,
            )
            cr.raise_for_status()
            excluded_ids.update(cf["id"] for cf in cr.json().get("value", []))
        return excluded_ids
    except Exception:
        return set()


@retry_http
def list_drafts(limit: int = 20) -> list[dict]:
    """List up to `limit` drafts sitting unsent anywhere in the mailbox
    (excluding Deleted Items/Junk Email/Sync Issues, see
    _EXCLUDED_DRAFT_FOLDER_NAMES), newest-modified first. Returns
    [{"to","subject","last_modified"}].

    Deliberately NOT scoped to /me/mailFolders/Drafts/messages
    (ClaudeAIScoutMaster#261): a reply/forward draft created via Graph's
    createReply/createReplyAll/createForward is left as a child of the
    *original message's* folder, never moved into the top-level Drafts
    folder. Querying isDraft eq true across the whole mailbox catches
    drafts regardless of which folder they landed in — but that widened
    net also picks up stale drafts sitting in Deleted Items, filtered back
    out here.
    """
    token = _get_access_token()
    excluded_ids = _excluded_draft_folder_ids(token)
    params = {
        "$filter": "isDraft eq true",
        "$top": limit * 3 if excluded_ids else limit,
        "$orderby": "lastModifiedDateTime desc",
        "$select": "toRecipients,subject,lastModifiedDateTime,parentFolderId",
    }
    r = httpx.get(
        f"{GRAPH}/me/messages",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=20,
    )
    r.raise_for_status()
    messages = r.json().get("value", [])
    results = []
    for m in messages:
        if m.get("parentFolderId") in excluded_ids:
            continue
        if not m.get("subject") and not m.get("toRecipients"):
            continue
        to_addrs = [rec.get("emailAddress", {}).get("address", "") for rec in m.get("toRecipients", [])]
        results.append({
            "to": ", ".join(a for a in to_addrs if a) or "(no recipient)",
            "subject": m.get("subject") or "(no subject)",
            "last_modified": m.get("lastModifiedDateTime", ""),
        })
        if len(results) >= limit:
            break
    return results


@retry_http
def send_draft(message_id: str) -> None:
    """Send an existing Outlook draft by message id — sends whatever is currently saved."""
    token = _get_access_token()
    httpx.post(
        f"{GRAPH}/me/messages/{message_id}/send",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    ).raise_for_status()


@retry_http
def update_draft(message_id: str, to_address: str, subject: str, body: str, content_type: str = "HTML") -> None:
    """Overwrite an existing Outlook draft's subject/body in place. `to_address` is
    accepted for interface symmetry with a Gmail client's update_draft but unused —
    this PATCH only touches subject/body, recipients are untouched by a revision."""
    token = _get_access_token()
    payload = {
        "subject": subject,
        "body": {"contentType": content_type, "content": body},
    }
    httpx.patch(
        f"{GRAPH}/me/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    ).raise_for_status()


@retry_http
def create_reply_draft(message_id: str) -> tuple[str, str]:
    """Ask Graph to build a reply draft for `message_id` and return
    (draft_id, body_html).

    This is the *provider's* reply, so the returned body already contains
    the original quoted with its own markup intact — logo, tables, fonts,
    the "From:/Sent:/To:/Subject:" header block — exactly what Outlook
    itself produces. Recipients, subject and conversation threading are set
    by Graph too. Callers insert their own prose at the top via
    `prepend_to_draft_body()` rather than assembling a quote by hand.
    """
    token = _get_access_token()
    r = httpx.post(
        f"{GRAPH}/me/messages/{message_id}/createReply",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    msg = r.json()
    return msg.get("id", ""), (msg.get("body") or {}).get("content", "")


def _insert_at_body_start(document_html: str, fragment_html: str) -> str:
    """Put `fragment_html` at the top of `document_html`'s visible content.

    Graph hands back a full `<html><head>…</head><body>…</body></html>`
    document. Naively concatenating puts our prose *outside* <body> (or before
    <html>), which some clients drop entirely — so splice it in right after the
    opening <body> tag when there is one, and fall back to a plain prepend for
    a bare fragment.
    """
    m = re.search(r"<body\b[^>]*>", document_html, re.IGNORECASE)
    if not m:
        return f"{fragment_html}{document_html}"
    at = m.end()
    return f"{document_html[:at]}{fragment_html}{document_html[at:]}"


@retry_http
def prepend_to_draft_body(message_id: str, fragment_html: str) -> None:
    """Insert `fragment_html` at the top of an existing draft's body, keeping
    everything already there (the quoted original) untouched."""
    token = _get_access_token()
    r = httpx.get(
        f"{GRAPH}/me/messages/{message_id}?$select=body",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    r.raise_for_status()
    current = (r.json().get("body") or {}).get("content", "")
    httpx.patch(
        f"{GRAPH}/me/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"body": {"contentType": "HTML", "content": _insert_at_body_start(current, fragment_html)}},
        timeout=15,
    ).raise_for_status()


@retry_http
def send_new_email(
    to_address: str,
    subject: str,
    body: str,
    attachment_path: Optional[str] = None,
    attachment_name: Optional[str] = None,
    content_type: str = "HTML",
) -> None:
    """Compose and send a brand-new email from the configured mailbox.

    Optionally attach a file by passing its local path. attachment_name overrides
    the filename shown to the recipient (defaults to the basename of attachment_path).
    """
    import base64
    _require_valid_email(to_address, "send_new_email")
    token = _get_access_token()
    message: dict = {
        "subject": subject,
        "body": {"contentType": content_type, "content": body},
        "toRecipients": [{"emailAddress": {"address": to_address}}],
    }
    if attachment_path:
        with open(attachment_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode()
        fname = attachment_name or os.path.basename(attachment_path)
        message["attachments"] = [{
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": fname,
            "contentBytes": encoded,
        }]
    httpx.post(
        f"{GRAPH}/me/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"message": message, "saveToSentItems": True},
        timeout=30,
    ).raise_for_status()


@retry_http
def list_sent_messages(since: datetime, limit: int = 200) -> list[dict]:
    """List messages from the Outlook SentItems folder since the given UTC datetime.

    Returns list of dicts: {id, subject, body, sent_at (datetime), recipients (list[str])}.
    Recipients is flattened from toRecipients[*].emailAddress.address.
    """
    from datetime import datetime as _dt
    token = _get_access_token()
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "$filter": f"sentDateTime ge {since_iso}",
        "$top": limit,
        "$orderby": "sentDateTime desc",
        "$select": "id,subject,bodyPreview,body,sentDateTime,toRecipients",
    }
    r = httpx.get(
        f"{GRAPH}/me/mailFolders/SentItems/messages",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    messages = r.json().get("value", [])

    results = []
    for m in messages:
        body_obj = m.get("body", {}) or {}
        raw_body = body_obj.get("content", "") or m.get("bodyPreview", "")
        if body_obj.get("contentType", "").lower() == "html":
            body_text = _html_to_text(raw_body)
        else:
            body_text = raw_body
        recipients = [
            r.get("emailAddress", {}).get("address", "")
            for r in (m.get("toRecipients") or [])
        ]
        recipients = [r for r in recipients if r]
        sent_raw = m.get("sentDateTime", "")
        try:
            sent_at = _dt.strptime(sent_raw[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            sent_at = _dt.utcnow()
        results.append({
            "id": m["id"],
            "subject": m.get("subject", "(no subject)"),
            "body": body_text,
            "sent_at": sent_at,
            "recipients": recipients,
        })
    return results


@retry_http
def archive_message(uid: str) -> None:
    """Move a message to the Archive folder (or Deleted Items if Archive doesn't exist)."""
    token = _get_access_token()
    for folder_name in ("Archive", "deleteditems"):
        try:
            if folder_name == "deleteditems":
                folder_id = folder_name
            else:
                r = httpx.get(
                    f"{GRAPH}/me/mailFolders",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"$filter": f"displayName eq '{folder_name}'"},
                    timeout=10,
                )
                r.raise_for_status()
                folders = r.json().get("value", [])
                if not folders:
                    continue
                folder_id = folders[0]["id"]

            httpx.post(
                f"{GRAPH}/me/messages/{uid}/move",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"destinationId": folder_id},
                timeout=10,
            ).raise_for_status()
            return
        except Exception:
            continue
    mark_read(uid)


def move_to_folder(uid: str, folder_name: str) -> None:
    """Move a message to a named folder, creating it if it doesn't exist."""
    token = _get_access_token()

    r = httpx.get(
        f"{GRAPH}/me/mailFolders",
        headers={"Authorization": f"Bearer {token}"},
        params={"$filter": f"displayName eq '{folder_name}'"},
        timeout=10,
    )
    r.raise_for_status()
    folders = r.json().get("value", [])

    if folders:
        folder_id = folders[0]["id"]
    else:
        r = httpx.post(
            f"{GRAPH}/me/mailFolders",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"displayName": folder_name},
            timeout=10,
        )
        r.raise_for_status()
        folder_id = r.json()["id"]

    httpx.post(
        f"{GRAPH}/me/messages/{uid}/move",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"destinationId": folder_id},
        timeout=10,
    ).raise_for_status()
    mark_read(uid)


@retry_http
def forward_message(uid: str, to_address: str, comment: str = "") -> None:
    """Forward a message to another address via Graph API."""
    _require_valid_email(to_address, "forward_message")
    token = _get_access_token()
    payload = {
        "toRecipients": [{"emailAddress": {"address": to_address}}],
    }
    if comment:
        payload["comment"] = comment
    httpx.post(
        f"{GRAPH}/me/messages/{uid}/forward",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    ).raise_for_status()

"""Gmail IMAP/SMTP client — app-password-based twin of gmail_client.

Why this exists (donna-workspace#311): Google hard-expires OAuth refresh
tokens after 7 days for unverified apps requesting restricted Gmail scopes,
and every Gmail read/modify scope is restricted. Publishing the consent
screen to Production did not clear the fuse; only a CASA verification would.
App passwords (2FA-gated, per-account) have no expiry, so IMAP+SMTP is the
durable transport for a personal deployment.

Public functions mirror agent_toolkit.gmail_client exactly — same names,
same signatures, same return shapes — and gmail_client dispatches here
per-account when the app-password env vars are set:

  GMAIL_ADDRESS_<ACCT>       full address, e.g. GMAIL_ADDRESS_PERSONAL
  GMAIL_APP_PASSWORD_<ACCT>  16-char app password (spaces tolerated)

where <ACCT> is the token_env with its GMAIL_TOKEN_ prefix stripped, so the
token_env strings threaded through every consumer keep working as the
account discriminator without touching call sites.

ID compatibility: Gmail's REST message id is the lowercase-hex form of
IMAP's X-GM-MSGID (and threadId of X-GM-THRID), so uids handed out here are
byte-identical to the REST client's — persisted seen-state and pending
message references survive the transport switch. Draft ids are the drafts'
own Message-ID headers (stable across update_draft), which means draft ids
minted by the REST client cannot be resolved here — send_draft raises for
those rather than guessing.

Mutations treat "message not found" as already-handled (success), matching
the REST client's 404 semantics that MailActionError callers rely on.
"""
from __future__ import annotations

import imaplib
import logging
import os
import re
import smtplib
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid

from agent_toolkit.gmail_client import _decode_str, _strip_html

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# Special-use fallbacks for English-locale Gmail, used only if the LIST
# scan somehow yields no folder carrying the attribute.
_SPECIAL_FALLBACK = {
    "\\All": "[Gmail]/All Mail",
    "\\Trash": "[Gmail]/Trash",
    "\\Drafts": "[Gmail]/Drafts",
    "\\Sent": "[Gmail]/Sent Mail",
}


def _account(token_env: str) -> tuple[str, str]:
    suffix = token_env.removeprefix("GMAIL_TOKEN_")
    addr = os.getenv(f"GMAIL_ADDRESS_{suffix}", "")
    pwd = os.getenv(f"GMAIL_APP_PASSWORD_{suffix}", "").replace(" ", "")
    return addr, pwd


def is_configured(token_env: str) -> bool:
    """True when this account has both app-password env vars set — the
    signal gmail_client's dispatcher uses to route here."""
    addr, pwd = _account(token_env)
    return bool(addr and pwd)


def _q(s: str) -> str:
    """IMAP quoted-string."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


@contextmanager
def _imap(token_env: str):
    addr, pwd = _account(token_env)
    if not (addr and pwd):
        raise RuntimeError(
            f"Gmail IMAP not configured for {token_env} "
            f"(need GMAIL_ADDRESS_* and GMAIL_APP_PASSWORD_*)"
        )
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        conn.login(addr, pwd)
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _special_folder(conn, attr: str) -> str:
    """Resolve a special-use folder (\\All, \\Trash, \\Drafts, \\Sent) by its
    LIST attribute rather than its display name, which is locale-dependent."""
    typ, listing = conn.list()
    if typ == "OK":
        for entry in listing or []:
            if not entry:
                continue
            line = entry.decode("utf-8", errors="replace") if isinstance(entry, bytes) else str(entry)
            m = re.match(r'\(([^)]*)\)\s+"[^"]*"\s+(.+)$', line)
            if not m:
                continue
            attrs, name = m.group(1), m.group(2).strip()
            if attr.lower() in attrs.lower().split():
                return name.strip('"')
    return _SPECIAL_FALLBACK[attr]


def _gm_uid_search(conn, msg_id_hex: str) -> list[bytes]:
    """UID-search the selected folder for a REST-style (hex X-GM-MSGID) id."""
    try:
        decimal = str(int(msg_id_hex, 16))
    except ValueError:
        return []
    typ, data = conn.uid("SEARCH", "X-GM-MSGID", decimal)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _fetch_full(conn, uid: bytes) -> tuple[dict, bytes] | None:
    """Fetch one message: returns ({'msgid': int, 'thrid': int}, raw_mime)."""
    typ, data = conn.uid("FETCH", uid.decode(), "(X-GM-MSGID X-GM-THRID BODY.PEEK[])")
    if typ != "OK":
        return None
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            meta = item[0] if isinstance(item[0], bytes) else b""
            gm = {}
            m = re.search(rb"X-GM-MSGID (\d+)", meta)
            gm["msgid"] = int(m.group(1)) if m else 0
            m = re.search(rb"X-GM-THRID (\d+)", meta)
            gm["thrid"] = int(m.group(1)) if m else 0
            return gm, item[1]
    return None


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def _extract_body_from_mime(msg: Message) -> str:
    """Plain-text body: prefer text/plain, else stripped text/html — same
    preference order as the REST client's _extract_body_from_payload."""
    html_fallback = ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if "attachment" in str(part.get("Content-Disposition", "")).lower():
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            text = _part_text(part)
            if text:
                return text
        elif ctype == "text/html" and not html_fallback:
            html_fallback = _part_text(part)
    return _strip_html(html_fallback) if html_fallback else ""


def _extract_html_from_mime(msg: Message) -> str:
    """The text/html part *unstripped* (for quoting), or "" if plain-only."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            if "attachment" in str(part.get("Content-Disposition", "")).lower():
                continue
            text = _part_text(part)
            if text:
                return text
    return ""


def _parse_mime(gm: dict, raw: bytes) -> dict:
    msg = message_from_bytes(raw)
    sender_raw = _decode_str(msg.get("From"))
    m = re.search(r"<([^>]+)>", sender_raw)
    sender_email = m.group(1) if m else sender_raw.strip()
    sender_name = re.sub(r"\s*<[^>]+>", "", sender_raw).strip().strip('"')
    return {
        "uid": format(gm["msgid"], "x") if gm.get("msgid") else "",
        "thread_id": format(gm["thrid"], "x") if gm.get("thrid") else "",
        "message_id": _decode_str(msg.get("Message-ID")).strip(),
        "subject": _decode_str(msg.get("Subject")) or "(no subject)",
        "sender": sender_name or sender_email,
        "sender_email": sender_email,
        "date": _decode_str(msg.get("Date")),
        "body": _extract_body_from_mime(msg)[:4000],
    }


def _fetch_parsed(conn, uids: list[bytes], limit: int) -> list[dict]:
    """Fetch+parse the newest `limit` of `uids`, newest-first (matching the
    REST client's ordering)."""
    results = []
    for uid in sorted(uids, key=int, reverse=True)[:limit]:
        got = _fetch_full(conn, uid)
        if got:
            results.append(_parse_mime(got[0], got[1]))
    return results


# --------------------------------------------------------------------- #
# Read paths
# --------------------------------------------------------------------- #

def fetch_unread(limit: int = 10, token_env: str = "GMAIL_TOKEN_TROOP") -> list[dict]:
    """Fetch up to `limit` unread inbox emails. Same dict shape as the REST client."""
    with _imap(token_env) as conn:
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("SEARCH", "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return []
        return _fetch_parsed(conn, data[0].split(), limit)


def search_messages(
    keyword: str,
    sender: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 15,
    token_env: str = "GMAIL_TOKEN_TROOP",
) -> list[dict]:
    """Search via X-GM-RAW in All Mail — Gmail's own query syntax, and All
    Mail (+Sent, which it contains) matches the REST default scope of
    everything-but-Trash/Spam."""
    q_parts = [keyword]
    if sender:
        q_parts.append(f"from:{sender}")
    if since:
        q_parts.append(f"after:{since.replace('-', '/')}")
    if until:
        q_parts.append(f"before:{until.replace('-', '/')}")
    q = " ".join(q_parts)

    with _imap(token_env) as conn:
        conn.select(_q(_special_folder(conn, "\\All")), readonly=True)
        typ, data = conn.uid("SEARCH", "X-GM-RAW", _q(q))
        if typ != "OK" or not data or not data[0]:
            return []
        return _fetch_parsed(conn, data[0].split(), limit)


def list_labels(token_env: str = "GMAIL_TOKEN_TROOP") -> list[str]:
    """User-created label names — Gmail exposes each label as an IMAP folder;
    system ones carry special-use attributes or live under [Gmail]."""
    with _imap(token_env) as conn:
        typ, listing = conn.list()
        names = []
        for entry in listing or []:
            if not entry:
                continue
            line = entry.decode("utf-8", errors="replace") if isinstance(entry, bytes) else str(entry)
            m = re.match(r'\(([^)]*)\)\s+"[^"]*"\s+(.+)$', line)
            if not m:
                continue
            attrs = m.group(1).lower()
            name = m.group(2).strip().strip('"')
            if name.upper() == "INBOX" or name.startswith("[Gmail]"):
                continue
            if any(a in attrs for a in ("\\all", "\\trash", "\\drafts", "\\sent", "\\junk", "\\flagged", "\\important")):
                continue
            names.append(name)
        return sorted(names)


def get_message_raw(msg_id: str, token_env: str = "GMAIL_TOKEN_TROOP") -> dict:
    """Headers + plain body of one message, for forward composition."""
    with _imap(token_env) as conn:
        conn.select(_q(_special_folder(conn, "\\All")), readonly=True)
        uids = _gm_uid_search(conn, msg_id)
        if not uids:
            raise RuntimeError(f"Gmail message {msg_id} not found in All Mail")
        got = _fetch_full(conn, uids[0])
        if not got:
            raise RuntimeError(f"Gmail message {msg_id} fetch failed")
        msg = message_from_bytes(got[1])
        return {
            "from": _decode_str(msg.get("From")),
            "subject": _decode_str(msg.get("Subject")) or "(no subject)",
            "date": _decode_str(msg.get("Date")),
            "body": _extract_body_from_mime(msg),
        }


# --------------------------------------------------------------------- #
# Mutations — not-found is success, mirroring REST 404 semantics
# --------------------------------------------------------------------- #

def _mutate(token_env: str, msg_id: str, op_name: str, fn) -> None:
    with _imap(token_env) as conn:
        conn.select(_q(_special_folder(conn, "\\All")))
        uids = _gm_uid_search(conn, msg_id)
        if not uids:
            log.info("gmail_imap.%s(%s): not found in All Mail, treating as handled", op_name, msg_id)
            return
        fn(conn, uids[0].decode())


def mark_read(msg_id: str, token_env: str = "GMAIL_TOKEN_TROOP") -> None:
    _mutate(token_env, msg_id, "mark_read",
            lambda conn, uid: conn.uid("STORE", uid, "+FLAGS", r"(\Seen)"))


def archive_message(msg_id: str, token_env: str = "GMAIL_TOKEN_TROOP") -> None:
    # Removing the \Inbox label *is* archiving in Gmail.
    _mutate(token_env, msg_id, "archive",
            lambda conn, uid: conn.uid("STORE", uid, "-X-GM-LABELS", r"(\Inbox)"))


def trash_message(msg_id: str, token_env: str = "GMAIL_TOKEN_TROOP") -> None:
    def _trash(conn, uid):
        # COPY to Trash is Gmail's documented IMAP "move to trash" — it
        # detaches the message from every other folder server-side.
        trash = _special_folder(conn, "\\Trash")
        typ, _ = conn.uid("COPY", uid, _q(trash))
        if typ != "OK":
            raise RuntimeError(f"COPY to {trash} failed for {uid}")
    _mutate(token_env, msg_id, "trash", _trash)


def apply_label(msg_id: str, label_name: str, token_env: str = "GMAIL_TOKEN_TROOP") -> None:
    """Apply a label and remove from inbox (creates the label if missing)."""
    def _label(conn, uid):
        conn.create(_q(label_name))  # NO if it already exists — fine
        typ, _ = conn.uid("STORE", uid, "+X-GM-LABELS", f"({_q(label_name)})")
        if typ != "OK":
            raise RuntimeError(f"label {label_name!r} failed for {uid}")
        conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")
        conn.uid("STORE", uid, "-X-GM-LABELS", r"(\Inbox)")
    _mutate(token_env, msg_id, "apply_label", _label)


# --------------------------------------------------------------------- #
# Send + drafts
# --------------------------------------------------------------------- #

def _smtp_send(token_env: str, msg: Message) -> None:
    addr, pwd = _account(token_env)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60) as smtp:
        smtp.login(addr, pwd)
        smtp.send_message(msg)


def _build_mime(token_env: str, to_address: str, subject: str, body: str,
                content_type: str) -> Message:
    from_addr, _ = _account(token_env)
    if content_type == "html":
        from agent_toolkit.email_html import html_to_preview_text
        msg = MIMEMultipart("alternative")
        # Least-preferred part first, per RFC 2046.
        msg.attach(MIMEText(html_to_preview_text(body), "plain"))
        msg.attach(MIMEText(body, "html"))
    else:
        msg = MIMEText(body, content_type)
    msg["To"] = to_address
    msg["From"] = from_addr
    msg["Subject"] = subject
    return msg


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
    """Send via SMTP. `thread_id` is accepted for signature parity but unused —
    In-Reply-To/References are what threads a message over SMTP, and Gmail
    files the Sent copy into the conversation from those headers."""
    msg = _build_mime(token_env, to_address, subject, body, content_type)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    _smtp_send(token_env, msg)


def _append_draft(token_env: str, msg: Message) -> str:
    """APPEND to Drafts; the draft's own Message-ID header is the draft id."""
    if not msg.get("Message-ID"):
        msg["Message-ID"] = make_msgid()
    draft_id = msg["Message-ID"]
    with _imap(token_env) as conn:
        drafts = _special_folder(conn, "\\Drafts")
        typ, data = conn.append(_q(drafts), r"(\Draft \Seen)",
                                imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if typ != "OK":
            raise RuntimeError(f"draft APPEND failed: {data}")
    return draft_id


def _find_draft(conn, draft_id: str) -> bytes | None:
    typ, data = conn.uid("SEARCH", "HEADER", "Message-ID", _q(draft_id))
    if typ != "OK" or not data or not data[0]:
        return None
    uids = data[0].split()
    return uids[-1] if uids else None


def create_draft(
    to_address: str,
    subject: str,
    body: str,
    token_env: str = "GMAIL_TOKEN_TROOP",
    content_type: str = "html",
    thread_id: str | None = None,
    in_reply_to: str | None = None,
) -> str:
    msg = MIMEText(body, content_type)
    msg["To"] = to_address
    msg["From"] = _account(token_env)[0]
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    return _append_draft(token_env, msg)


def create_draft_new(
    to_address: str,
    subject: str,
    body: str,
    token_env: str = "GMAIL_TOKEN_TROOP",
    content_type: str = "html",
) -> str:
    msg = MIMEText(body, content_type)
    msg["To"] = to_address
    msg["From"] = _account(token_env)[0]
    msg["Subject"] = subject
    return _append_draft(token_env, msg)


def create_quoted_reply_draft(
    source_msg_id: str,
    body_html: str,
    token_env: str = "GMAIL_TOKEN_TROOP",
) -> str:
    """Reply draft quoting the original with its markup intact, threaded via
    In-Reply-To/References (no threadId concept over IMAP — Gmail threads
    the draft from the headers)."""
    from agent_toolkit.email_html import build_quoted_reply_html, plain_text_to_quoted_html

    with _imap(token_env) as conn:
        conn.select(_q(_special_folder(conn, "\\All")), readonly=True)
        uids = _gm_uid_search(conn, source_msg_id)
        if not uids:
            raise RuntimeError(f"Gmail message {source_msg_id} not found in All Mail")
        got = _fetch_full(conn, uids[0])
        if not got:
            raise RuntimeError(f"Gmail message {source_msg_id} fetch failed")
    src = message_from_bytes(got[1])

    sender = _decode_str(src.get("From"))
    date = _decode_str(src.get("Date"))
    subject = _decode_str(src.get("Subject")) or "(no subject)"
    message_id = _decode_str(src.get("Message-ID")).strip()
    references = _decode_str(src.get("References")).strip()

    original_html = _extract_html_from_mime(src)
    if not original_html:
        original_html = plain_text_to_quoted_html(_extract_body_from_mime(src))

    quoted = build_quoted_reply_html(body_html, original_html, f"On {date}, {sender} wrote:")

    msg = MIMEText(quoted, "html")
    msg["To"] = sender
    msg["From"] = _account(token_env)[0]
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if message_id:
        msg["In-Reply-To"] = message_id
        msg["References"] = f"{references} {message_id}".strip() if references else message_id
    return _append_draft(token_env, msg)


def list_drafts(limit: int = 20, token_env: str = "GMAIL_TOKEN_TROOP") -> list[dict]:
    """Same shape as the REST client: [{"to","subject","last_modified"}],
    newest-modified first."""
    with _imap(token_env) as conn:
        drafts = _special_folder(conn, "\\Drafts")
        conn.select(_q(drafts), readonly=True)
        typ, data = conn.uid("SEARCH", "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        results = []
        for uid in sorted(data[0].split(), key=int, reverse=True)[:limit]:
            typ, fetched = conn.uid(
                "FETCH", uid.decode(),
                "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (TO SUBJECT)])",
            )
            if typ != "OK":
                continue
            for item in fetched or []:
                if not (isinstance(item, tuple) and len(item) >= 2):
                    continue
                meta = item[0] if isinstance(item[0], bytes) else b""
                hdrs = message_from_bytes(item[1])
                last_modified = ""
                t = imaplib.Internaldate2tuple(meta)
                if t:
                    last_modified = datetime.fromtimestamp(
                        time.mktime(t), tz=timezone.utc
                    ).isoformat()
                results.append({
                    "to": _decode_str(hdrs.get("To")) or "(no recipient)",
                    "subject": _decode_str(hdrs.get("Subject")) or "(no subject)",
                    "last_modified": last_modified,
                })
        results.sort(key=lambda d: d["last_modified"], reverse=True)
        return results


def send_draft(draft_id: str, token_env: str = "GMAIL_TOKEN_TROOP") -> None:
    """Send an existing draft (by its Message-ID draft id) via SMTP, then
    remove it from Drafts. Gmail saves the Sent copy itself."""
    with _imap(token_env) as conn:
        drafts = _special_folder(conn, "\\Drafts")
        conn.select(_q(drafts))
        uid = _find_draft(conn, draft_id)
        if uid is None:
            raise RuntimeError(
                f"Draft {draft_id!r} not found in Drafts (REST-era draft ids "
                f"cannot be resolved by the IMAP backend)"
            )
        typ, data = conn.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
        raw = None
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2:
                raw = item[1]
        if typ != "OK" or raw is None:
            raise RuntimeError(f"Draft {draft_id!r} fetch failed")
        _smtp_send(token_env, message_from_bytes(raw))
        conn.uid("STORE", uid.decode(), "+FLAGS", r"(\Deleted)")
        conn.expunge()


def update_draft(
    draft_id: str,
    to_address: str,
    subject: str,
    body: str,
    token_env: str = "GMAIL_TOKEN_TROOP",
    content_type: str = "html",
) -> None:
    """Replace a draft's content, keeping the same draft id — the new copy is
    appended with the old Message-ID, then the old copy is expunged."""
    msg = MIMEText(body, content_type)
    msg["To"] = to_address
    msg["From"] = _account(token_env)[0]
    msg["Subject"] = subject
    msg["Message-ID"] = draft_id
    with _imap(token_env) as conn:
        drafts = _special_folder(conn, "\\Drafts")
        conn.select(_q(drafts))
        old_uid = _find_draft(conn, draft_id)
        if old_uid is None:
            raise RuntimeError(f"Draft {draft_id!r} not found in Drafts")
        typ, data = conn.append(_q(drafts), r"(\Draft \Seen)",
                                imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if typ != "OK":
            raise RuntimeError(f"draft APPEND failed: {data}")
        conn.uid("STORE", old_uid.decode(), "+FLAGS", r"(\Deleted)")
        conn.expunge()

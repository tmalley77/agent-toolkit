"""gmail_imap — IMAP/SMTP app-password backend (donna-workspace#311).

Covers the dispatch contract (gmail_client routes per-account on the
app-password env vars), REST-id compatibility (hex X-GM-MSGID uids), the
not-found-is-success mutation semantics MailActionError callers rely on,
and the Message-ID draft-id scheme.
"""
from unittest.mock import MagicMock, patch

import pytest

from agent_toolkit import gmail_client as gc
from agent_toolkit import gmail_imap as gi


ACCT = "GMAIL_TOKEN_TESTACCT"


@pytest.fixture
def imap_env(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS_TESTACCT", "test@example.com")
    # Spaces as Google displays them — must be stripped before login.
    monkeypatch.setenv("GMAIL_APP_PASSWORD_TESTACCT", "abcd efgh ijkl mnop")


def _mock_conn():
    conn = MagicMock()
    conn.list.return_value = ("OK", [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
        b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
        b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"',
        b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
        b'(\\HasNoChildren) "/" "Troop208"',
        b'(\\HasNoChildren) "/" "Receipts/2026"',
    ])
    conn.select.return_value = ("OK", [b"1"])
    return conn


# --------------------------------------------------------------------- #
# Config + dispatch
# --------------------------------------------------------------------- #

def test_is_configured_requires_both_env_vars(monkeypatch, imap_env):
    assert gi.is_configured(ACCT)
    monkeypatch.delenv("GMAIL_APP_PASSWORD_TESTACCT")
    assert not gi.is_configured(ACCT)


def test_account_strips_spaces_from_app_password(imap_env):
    addr, pwd = gi._account(ACCT)
    assert addr == "test@example.com"
    assert pwd == "abcdefghijklmnop"


def test_gmail_client_dispatches_to_imap_when_configured(imap_env):
    with patch.object(gi, "mark_read") as imap_mark:
        gc.mark_read("abc123", token_env=ACCT)
    imap_mark.assert_called_once_with("abc123", token_env=ACCT)


def test_gmail_client_falls_back_to_rest_when_not_configured(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS_TESTACCT", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD_TESTACCT", raising=False)
    # REST path: _get_service raises on the missing token file — proof the
    # call did NOT route to the IMAP twin.
    monkeypatch.delenv(ACCT, raising=False)
    with pytest.raises(RuntimeError, match="not set and no default"):
        gc.mark_read("abc123", token_env=ACCT)


def test_dispatch_binds_positional_token_env(imap_env):
    # fetch_unread(limit, token_env) called fully positionally must still
    # resolve the account for routing.
    with patch.object(gi, "fetch_unread", return_value=[]) as imap_fetch:
        gc.fetch_unread(5, ACCT)
    imap_fetch.assert_called_once_with(5, ACCT)


# --------------------------------------------------------------------- #
# Read paths
# --------------------------------------------------------------------- #

RAW_MSG = (
    b"From: Jane Leader <jane@example.org>\r\n"
    b"To: test@example.com\r\n"
    b"Subject: Campout headcount\r\n"
    b"Date: Mon, 24 Aug 2026 09:00:00 -0400\r\n"
    b"Message-ID: <orig-1@example.org>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Who is coming to the campout?\r\n"
)


def test_fetch_unread_parses_hex_gm_ids_newest_first(imap_env):
    conn = _mock_conn()
    conn.uid.side_effect = [
        ("OK", [b"3 7"]),  # SEARCH UNSEEN
        ("OK", [(b"7 (X-GM-MSGID 255 X-GM-THRID 16 BODY[] {123}", RAW_MSG), b")"]),
        ("OK", [(b"3 (X-GM-MSGID 254 X-GM-THRID 16 BODY[] {123}", RAW_MSG), b")"]),
    ]
    with patch.object(gi.imaplib, "IMAP4_SSL", return_value=conn):
        out = gi.fetch_unread(limit=10, token_env=ACCT)

    assert [m["uid"] for m in out] == ["ff", "fe"]  # newest (highest UID) first, hex
    assert out[0]["thread_id"] == "10"
    assert out[0]["sender_email"] == "jane@example.org"
    assert out[0]["sender"] == "Jane Leader"
    assert out[0]["subject"] == "Campout headcount"
    assert "campout" in out[0]["body"].lower()
    # Unread fetch must never set \Seen: peek-only, readonly select.
    conn.select.assert_called_with("INBOX", readonly=True)
    assert all("PEEK" in str(c) for c in conn.uid.call_args_list[1:])


def test_search_messages_builds_x_gm_raw_query(imap_env):
    conn = _mock_conn()
    conn.uid.side_effect = [("OK", [b""])]
    with patch.object(gi.imaplib, "IMAP4_SSL", return_value=conn):
        out = gi.search_messages(
            "dues", sender="treasurer@x.org", since="2026-08-01", until="2026-08-20",
            token_env=ACCT,
        )
    assert out == []
    args = conn.uid.call_args_list[0][0]
    assert args[0] == "SEARCH" and args[1] == "X-GM-RAW"
    assert args[2] == '"dues from:treasurer@x.org after:2026/08/01 before:2026/08/20"'


def test_list_labels_excludes_system_folders(imap_env):
    conn = _mock_conn()
    with patch.object(gi.imaplib, "IMAP4_SSL", return_value=conn):
        assert gi.list_labels(token_env=ACCT) == ["Receipts/2026", "Troop208"]


# --------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------- #

def test_mark_read_stores_seen_on_gm_msgid_match(imap_env):
    conn = _mock_conn()
    conn.uid.side_effect = [
        ("OK", [b"42"]),  # SEARCH X-GM-MSGID
        ("OK", [b"42"]),  # STORE
    ]
    with patch.object(gi.imaplib, "IMAP4_SSL", return_value=conn):
        gi.mark_read("ff", token_env=ACCT)
    search_args = conn.uid.call_args_list[0][0]
    assert search_args == ("SEARCH", "X-GM-MSGID", "255")  # hex ff -> decimal
    store_args = conn.uid.call_args_list[1][0]
    assert store_args == ("STORE", "42", "+FLAGS", r"(\Seen)")


def test_mutation_not_found_is_success(imap_env):
    """REST 404 semantics: an already-gone message must not raise, so the
    caller records it handled instead of retrying forever."""
    conn = _mock_conn()
    conn.uid.side_effect = [("OK", [b""])]
    with patch.object(gi.imaplib, "IMAP4_SSL", return_value=conn):
        gi.trash_message("ff", token_env=ACCT)  # no raise
    assert len(conn.uid.call_args_list) == 1  # searched, then stopped


def test_non_hex_uid_treated_as_not_found(imap_env):
    # A REST-era draft id or garbage uid can reach a mutation; it can't be a
    # X-GM-MSGID, so it is "already gone", not a crash.
    conn = _mock_conn()
    with patch.object(gi.imaplib, "IMAP4_SSL", return_value=conn):
        gi.mark_read("r-9999999", token_env=ACCT)


def test_archive_removes_inbox_label(imap_env):
    conn = _mock_conn()
    conn.uid.side_effect = [("OK", [b"42"]), ("OK", [b"42"])]
    with patch.object(gi.imaplib, "IMAP4_SSL", return_value=conn):
        gi.archive_message("ff", token_env=ACCT)
    assert conn.uid.call_args_list[1][0] == ("STORE", "42", "-X-GM-LABELS", r"(\Inbox)")


def test_apply_label_creates_labels_and_clears_inbox_unread(imap_env):
    conn = _mock_conn()
    conn.uid.side_effect = [
        ("OK", [b"42"]),  # SEARCH
        ("OK", [b"42"]),  # +X-GM-LABELS
        ("OK", [b"42"]),  # +FLAGS \Seen
        ("OK", [b"42"]),  # -X-GM-LABELS \Inbox
    ]
    with patch.object(gi.imaplib, "IMAP4_SSL", return_value=conn):
        gi.apply_label("ff", "Troop 208", token_env=ACCT)
    conn.create.assert_called_once_with('"Troop 208"')
    assert conn.uid.call_args_list[1][0] == ("STORE", "42", "+X-GM-LABELS", '("Troop 208")')


# --------------------------------------------------------------------- #
# Send + drafts
# --------------------------------------------------------------------- #

def test_send_message_html_is_multipart_with_threading_headers(imap_env):
    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, addr, pwd):
            sent["login"] = (addr, pwd)
        def send_message(self, msg):
            sent["msg"] = msg

    with patch.object(gi.smtplib, "SMTP_SSL", FakeSMTP):
        gi.send_message(
            "scout@x.org", "Re: Campout", "<p>Yes!</p>", token_env=ACCT,
            in_reply_to="<orig-1@example.org>", references="<root@example.org>",
            thread_id="deadbeef",  # signature parity — must be tolerated
        )

    assert sent["login"] == ("test@example.com", "abcdefghijklmnop")
    msg = sent["msg"]
    assert msg["From"] == "test@example.com"
    assert msg["To"] == "scout@x.org"
    assert msg.is_multipart() and msg.get_content_subtype() == "alternative"
    assert msg["In-Reply-To"] == "<orig-1@example.org>"
    assert msg["References"] == "<root@example.org> <orig-1@example.org>"


def test_create_draft_returns_message_id_and_appends_to_drafts(imap_env):
    conn = _mock_conn()
    conn.append.return_value = ("OK", [b"[APPENDUID 1 99] Success"])
    with patch.object(gi.imaplib, "IMAP4_SSL", return_value=conn):
        draft_id = gi.create_draft(
            "scout@x.org", "Campout", "body", token_env=ACCT,
            in_reply_to="<orig-1@example.org>",
        )
    assert draft_id.startswith("<") and draft_id.endswith(">")
    folder, flags = conn.append.call_args[0][0], conn.append.call_args[0][1]
    assert folder == '"[Gmail]/Drafts"'
    assert r"\Draft" in flags
    appended = conn.append.call_args[0][3]
    assert b"Subject: Re: Campout" in appended
    assert draft_id.encode() in appended


def test_update_draft_keeps_draft_id_and_expunges_old(imap_env):
    conn = _mock_conn()
    conn.uid.side_effect = [
        ("OK", [b"7"]),   # SEARCH HEADER Message-ID (old copy)
        ("OK", [b"7"]),   # STORE \Deleted
    ]
    conn.append.return_value = ("OK", [b"ok"])
    with patch.object(gi.imaplib, "IMAP4_SSL", return_value=conn):
        gi.update_draft("<d-1@x>", "scout@x.org", "New subject", "new body", token_env=ACCT)
    assert b"Message-ID: <d-1@x>" in conn.append.call_args[0][3]
    assert conn.uid.call_args_list[1][0] == ("STORE", "7", "+FLAGS", r"(\Deleted)")
    conn.expunge.assert_called_once()


def test_send_draft_unresolvable_rest_era_id_raises(imap_env):
    conn = _mock_conn()
    conn.uid.side_effect = [("OK", [b""])]  # HEADER Message-ID search: nothing
    with patch.object(gi.imaplib, "IMAP4_SSL", return_value=conn):
        with pytest.raises(RuntimeError, match="not found in Drafts"):
            gi.send_draft("r-rest-era-id", token_env=ACCT)


def test_get_message_raw_returns_forward_fields(imap_env):
    conn = _mock_conn()
    conn.uid.side_effect = [
        ("OK", [b"42"]),
        ("OK", [(b"42 (X-GM-MSGID 255 X-GM-THRID 16 BODY[] {123}", RAW_MSG), b")"]),
    ]
    with patch.object(gi.imaplib, "IMAP4_SSL", return_value=conn):
        raw = gi.get_message_raw("ff", token_env=ACCT)
    assert raw["from"] == "Jane Leader <jane@example.org>"
    assert raw["subject"] == "Campout headcount"
    assert "campout" in raw["body"].lower()

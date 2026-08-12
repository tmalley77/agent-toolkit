import httplib2
import pytest
from googleapiclient.errors import HttpError

from agent_toolkit.mailbox.client import Email, MailActionError
from agent_toolkit.mailbox.gmail import GmailMailbox, _run_mutation


def _http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": str(status)}), b"error body")


def test_run_mutation_treats_404_as_success():
    def raises_404():
        raise _http_error(404)

    _run_mutation("mark_read", "msg-1", raises_404)  # does not raise


def test_run_mutation_reraises_non_404_http_error_as_mail_action_error():
    def raises_500():
        raise _http_error(500)

    with pytest.raises(MailActionError):
        _run_mutation("mark_read", "msg-1", raises_500)


def test_run_mutation_wraps_other_exceptions_as_mail_action_error():
    def raises_generic():
        raise RuntimeError("network exploded")

    with pytest.raises(MailActionError):
        _run_mutation("archive", "msg-1", raises_generic)


def test_gmail_mailbox_fetch_unread_maps_to_email_dataclass(monkeypatch):
    from agent_toolkit import gmail_client

    monkeypatch.setattr(
        gmail_client,
        "fetch_unread",
        lambda limit=10, token_env="GMAIL_TOKEN_TROOP": [{
            "uid": "1", "subject": "Hi", "sender": "A", "sender_email": "a@b.com",
            "date": "Mon, 1 Jan 2026 10:00:00 -0500", "body": "body",
            "message_id": "mid-1", "thread_id": "th-1",
        }],
    )

    mb = GmailMailbox(token_env="GMAIL_TOKEN_TROOP", mailbox_name="troop_gmail")
    result = mb.fetch_unread(limit=5)

    assert result == [Email(
        uid="1", subject="Hi", sender="A", sender_email="a@b.com",
        date="Mon, 1 Jan 2026 10:00:00 -0500", body="body",
        mailbox_name="troop_gmail", source_type="gmail",
        message_id="mid-1", thread_id="th-1",
    )]


def test_gmail_mailbox_send_reply_forces_re_prefix(monkeypatch):
    from agent_toolkit import gmail_client

    calls = {}

    def fake_send_message(**kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(gmail_client, "send_message", fake_send_message)

    mb = GmailMailbox(token_env="GMAIL_TOKEN_PERSONAL", mailbox_name="personal_gmail")
    mb.send_reply(to_address="a@b.com", subject="Trip details", body="<p>hi</p>")

    assert calls["subject"] == "Re: Trip details"
    assert calls["token_env"] == "GMAIL_TOKEN_PERSONAL"

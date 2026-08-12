import httpx
import pytest

from agent_toolkit.mailbox.client import Email, MailActionError, MailboxClient, get_mailbox, register_mailbox
from agent_toolkit.mailbox.outlook import OutlookMailbox, _run_mutation


def test_get_mailbox_unknown_name_raises():
    with pytest.raises(ValueError):
        get_mailbox("does-not-exist")


def test_register_and_get_mailbox_roundtrip(monkeypatch):
    import agent_toolkit.mailbox.client as client_module

    monkeypatch.setattr(client_module, "_REGISTRY", dict(client_module._REGISTRY))
    sentinel = object()
    register_mailbox("fake", lambda: sentinel)
    assert get_mailbox("fake") is sentinel


def test_mailbox_client_is_abstract():
    with pytest.raises(TypeError):
        MailboxClient()


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("PATCH", "https://graph.microsoft.com/v1.0/me/messages/abc")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_run_mutation_treats_404_as_success_not_failure():
    """Regression test for ClaudeAIScoutMaster#278: the ported source checked
    requests.exceptions.HTTPError, but outlook_client raises httpx's
    exception type — so a real 404 never hit the "already gone" branch."""
    def raises_404():
        raise _http_status_error(404)

    _run_mutation("mark_read", "msg-1", raises_404)  # does not raise


def test_run_mutation_reraises_non_404_http_status_error_as_mail_action_error():
    def raises_500():
        raise _http_status_error(500)

    with pytest.raises(MailActionError):
        _run_mutation("mark_read", "msg-1", raises_500)


def test_run_mutation_wraps_other_exceptions_as_mail_action_error():
    def raises_generic():
        raise RuntimeError("network exploded")

    with pytest.raises(MailActionError):
        _run_mutation("archive", "msg-1", raises_generic)


def test_outlook_mailbox_fetch_unread_maps_to_email_dataclass(monkeypatch):
    from agent_toolkit import outlook_client

    monkeypatch.setattr(
        outlook_client,
        "fetch_unread",
        lambda limit=10: [{
            "uid": "1", "subject": "Hi", "sender": "A", "sender_email": "a@b.com",
            "date": "2026-08-12 10:00", "body": "body", "message_id": "mid-1", "thread_id": "th-1",
        }],
    )

    result = OutlookMailbox().fetch_unread(limit=5)

    assert result == [Email(
        uid="1", subject="Hi", sender="A", sender_email="a@b.com",
        date="2026-08-12 10:00", body="body", mailbox_name="outlook",
        source_type="outlook", message_id="mid-1", thread_id="th-1",
    )]

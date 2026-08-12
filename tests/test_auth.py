import logging

import pytest

from agent_toolkit.auth import is_allowed_sender, make_audit_log


class _FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class _FakeUpdate:
    def __init__(self, user_id=None):
        self.effective_user = _FakeUser(user_id) if user_id is not None else None


def test_is_allowed_sender_denies_when_env_unset(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)
    assert is_allowed_sender(_FakeUpdate(123)) is False


def test_is_allowed_sender_denies_missing_user(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "123")
    assert is_allowed_sender(_FakeUpdate(None)) is False


def test_is_allowed_sender_denies_wrong_sender(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "123")
    assert is_allowed_sender(_FakeUpdate(456)) is False


def test_is_allowed_sender_allows_configured_sender(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "123")
    assert is_allowed_sender(_FakeUpdate(123)) is True


def test_is_allowed_sender_respects_custom_env_var(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)
    monkeypatch.setenv("GRETCHEN_ALLOWED_USER_ID", "789")
    assert is_allowed_sender(_FakeUpdate(789), env_var="GRETCHEN_ALLOWED_USER_ID") is True


def test_audit_log_only_fires_for_high_consequence_tools(caplog):
    audit_log = make_audit_log("test.audit", {"email:send_draft"})
    with caplog.at_level(logging.WARNING, logger="test.audit"):
        audit_log(_FakeUpdate(123), "notes:list_notes")
    assert not [r for r in caplog.records if r.name == "test.audit"]


def test_audit_log_fires_and_uses_given_logger_name(caplog):
    audit_log = make_audit_log("test.audit", {"email:send_draft"})
    with caplog.at_level(logging.WARNING, logger="test.audit"):
        audit_log(_FakeUpdate(123), "email:send_draft")
    records = [r for r in caplog.records if r.name == "test.audit"]
    assert len(records) == 1
    assert "sender_id=123" in records[0].message
    assert "tool=email:send_draft" in records[0].message


def test_audit_log_handles_missing_user():
    audit_log = make_audit_log("test.audit2", {"email:send_draft"})
    audit_log(_FakeUpdate(None), "email:send_draft")  # does not raise


def test_two_agents_get_independent_tool_sets():
    donna_log = make_audit_log("donna.audit", {"email:send_draft"})
    gretchen_log = make_audit_log("gretchen.audit", {"roster:remove_scout"})

    import logging as _logging
    donna_logger = _logging.getLogger("donna.audit")
    fired = []
    donna_logger.warning = lambda *a, **k: fired.append(a)

    donna_log(_FakeUpdate(1), "roster:remove_scout")  # not in Donna's set
    assert fired == []

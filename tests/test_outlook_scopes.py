"""OUTLOOK_SCOPES override (aiserver-stack#142).

Gretchen needs the troop OneNote notebook and nothing else. The default set
carries Mail.ReadWrite and Mail.Send on Tom's personal mailbox, which is
Donna's domain — so the scope list has to be per-consumer. The default must
not move, because hoa-ingest, wod-ingest and Donna all rely on it.
"""
import importlib
import os


def _reload(monkeypatch, value=None):
    if value is None:
        monkeypatch.delenv("OUTLOOK_SCOPES", raising=False)
    else:
        monkeypatch.setenv("OUTLOOK_SCOPES", value)
    import agent_toolkit.outlook_client as oc
    return importlib.reload(oc)


def test_default_is_unchanged_for_existing_consumers(monkeypatch):
    oc = _reload(monkeypatch)
    assert "Mail.ReadWrite" in oc.SCOPES
    assert "Mail.Send" in oc.SCOPES
    assert "Notes.Read" in oc.SCOPES
    assert "offline_access" in oc.SCOPES


def test_override_replaces_the_whole_set(monkeypatch):
    oc = _reload(monkeypatch, "https://graph.microsoft.com/Notes.Read offline_access")
    assert oc.SCOPES == "https://graph.microsoft.com/Notes.Read offline_access"
    assert "Mail." not in oc.SCOPES, "a notes-only consumer must not request mail scopes"


def test_blank_override_falls_back_to_the_default(monkeypatch):
    """An empty env var is a misconfiguration, not a request for no scopes —
    falling through to nothing would fail the token exchange confusingly."""
    oc = _reload(monkeypatch, "   ")
    assert oc.SCOPES == oc.DEFAULT_SCOPES

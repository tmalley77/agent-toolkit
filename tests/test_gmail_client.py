import os

import pytest

from agent_toolkit import gmail_client as gc


def test_get_service_raises_when_token_env_unset_and_no_default(monkeypatch):
    monkeypatch.delenv("GMAIL_TOKEN_CUSTOM", raising=False)
    with pytest.raises(RuntimeError):
        gc._get_service("GMAIL_TOKEN_CUSTOM")


def test_get_service_raises_when_token_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_TOKEN_CUSTOM", str(tmp_path / "does-not-exist.json"))
    with pytest.raises(RuntimeError, match="token not found"):
        gc._get_service("GMAIL_TOKEN_CUSTOM")


def test_token_dir_uses_gmail_token_dir_env_var_not_module_relative_path(monkeypatch):
    """Regression test: the source this was ported from (ClaudeAIScoutMaster's
    app/gmail_client.py) resolved a relative default token path via
    Path(__file__).resolve().parent.parent — a path into its own repo, which
    breaks once this lives in an installed package's site-packages."""
    monkeypatch.setenv("GMAIL_TOKEN_DIR", "/some/consumer/repo")
    assert gc._token_dir() == gc.Path("/some/consumer/repo")


def test_token_dir_defaults_to_cwd(monkeypatch):
    monkeypatch.delenv("GMAIL_TOKEN_DIR", raising=False)
    assert gc._token_dir() == gc.Path(".")


def test_get_header_case_insensitive_lookup():
    headers = [{"name": "Subject", "value": "Hi"}, {"name": "From", "value": "a@b.com"}]
    assert gc._get_header(headers, "subject") == "Hi"
    assert gc._get_header(headers, "FROM") == "a@b.com"


def test_get_header_missing_returns_empty_string():
    assert gc._get_header([], "Subject") == ""


def test_strip_html_removes_script_and_collapses_whitespace():
    raw = "<style>.x{}</style><p>Hello   <b>world</b></p><script>evil()</script>"
    text = gc._strip_html(raw)
    assert "evil()" not in text
    assert "Hello world" in text


def test_extract_body_from_payload_prefers_plain_text():
    import base64
    plain = base64.urlsafe_b64encode(b"plain body").decode()
    html_ = base64.urlsafe_b64encode(b"<p>html body</p>").decode()
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": plain}},
            {"mimeType": "text/html", "body": {"data": html_}},
        ],
    }
    assert gc._extract_body_from_payload(payload) == "plain body"


def test_extract_body_from_payload_falls_back_to_html_when_no_plain_part():
    import base64
    html_ = base64.urlsafe_b64encode(b"<p>only html</p>").decode()
    payload = {"mimeType": "text/html", "body": {"data": html_}}
    assert gc._extract_body_from_payload(payload) == "only html"

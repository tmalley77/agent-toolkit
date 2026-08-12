import os

import httpx
import pytest

from agent_toolkit import outlook_client as oc


@pytest.fixture(autouse=True)
def _outlook_env(monkeypatch):
    monkeypatch.setenv("OUTLOOK_ADDRESS", "tom@example.com")
    monkeypatch.setenv("OUTLOOK_CLIENT_ID", "client-id")
    monkeypatch.setenv("OUTLOOK_REFRESH_TOKEN", "refresh-token")


def test_cfg_raises_when_unset(monkeypatch):
    monkeypatch.delenv("OUTLOOK_REFRESH_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        oc._cfg()


@pytest.mark.parametrize("bad_addr", ["", "not-an-email", "missing-domain@", "@nodomain.com", None])
def test_require_valid_email_rejects_bad_addresses(bad_addr):
    with pytest.raises(ValueError):
        oc._require_valid_email(bad_addr, "test")


def test_require_valid_email_accepts_good_address():
    oc._require_valid_email("scoutmaster@troop208.org", "test")  # does not raise


def test_html_to_text_strips_tags_and_collapses_whitespace():
    raw = "<p>Hello   <b>world</b></p><script>evil()</script><br>Bye"
    text = oc._html_to_text(raw)
    assert "evil()" not in text
    assert "Hello world" in text
    assert "Bye" in text


def test_insert_at_body_start_splices_after_body_tag():
    doc = "<html><head></head><body><p>original</p></body></html>"
    out = oc._insert_at_body_start(doc, "<p>new</p>")
    assert out == "<html><head></head><body><p>new</p><p>original</p></body></html>"


def test_insert_at_body_start_falls_back_to_prepend_without_body_tag():
    doc = "<p>fragment only</p>"
    out = oc._insert_at_body_start(doc, "<p>new</p>")
    assert out == "<p>new</p><p>fragment only</p>"


def test_update_env_uses_outlook_env_path_not_module_relative_path(tmp_path, monkeypatch):
    """Regression test: the source this was ported from (ClaudeAIScoutMaster's
    app/outlook_client.py) located its .env via a __file__-relative path into
    its own repo — which breaks once this module lives in an installed
    package's site-packages directory. OUTLOOK_ENV_PATH replaces that."""
    env_file = tmp_path / ".env"
    env_file.write_text("OUTLOOK_REFRESH_TOKEN=old-token\nOTHER_VAR=untouched\n")
    monkeypatch.setenv("OUTLOOK_ENV_PATH", str(env_file))

    oc._update_env("OUTLOOK_REFRESH_TOKEN", "new-token")

    content = env_file.read_text()
    assert "OUTLOOK_REFRESH_TOKEN=new-token" in content
    assert "OTHER_VAR=untouched" in content


def test_update_env_noop_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTLOOK_ENV_PATH", str(tmp_path / "does-not-exist.env"))
    oc._update_env("OUTLOOK_REFRESH_TOKEN", "new-token")  # does not raise


def test_get_access_token_persists_rotated_refresh_token(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("OUTLOOK_REFRESH_TOKEN=refresh-token\n")
    monkeypatch.setenv("OUTLOOK_ENV_PATH", str(env_file))

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "access-123", "refresh_token": "rotated-token"}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse())

    token = oc._get_access_token()

    assert token == "access-123"
    assert os.environ["OUTLOOK_REFRESH_TOKEN"] == "rotated-token"
    assert "OUTLOOK_REFRESH_TOKEN=rotated-token" in env_file.read_text()

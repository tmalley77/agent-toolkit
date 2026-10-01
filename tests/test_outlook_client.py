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


def test_update_env_creates_store_when_file_missing(tmp_path, monkeypatch):
    """The rotation store is created rather than skipped.

    This used to no-op, on the reasoning that a missing file meant the env
    vars came purely from the process environment. That is exactly Donna's
    case -- compose supplies them via `env_file:` and nothing is bind-mounted
    -- so every refresh token Microsoft rotated was silently dropped, leaving
    the original grant in place until it was revoked (donna-workspace#373).
    """
    store = tmp_path / "outlook_token.env"
    monkeypatch.setenv("OUTLOOK_ENV_PATH", str(store))

    oc._update_env("OUTLOOK_REFRESH_TOKEN", "new-token")

    assert store.exists()
    assert "OUTLOOK_REFRESH_TOKEN=new-token" in store.read_text()


def test_update_env_warns_and_does_not_raise_when_store_unwritable(tmp_path, monkeypatch, caplog):
    """A credential that cannot be persisted must say so, not fail silently.

    It must not raise either: the access token in hand is still good, and
    taking down mail delivery over a failed write would be worse than the
    stale store it leaves behind.
    """
    monkeypatch.setenv("OUTLOOK_ENV_PATH", str(tmp_path / "no-such-dir" / "token.env"))

    with caplog.at_level("WARNING"):
        oc._update_env("OUTLOOK_REFRESH_TOKEN", "new-token")  # does not raise

    assert any("rotated" in r.message.lower() for r in caplog.records), caplog.text
    # never log the credential itself
    assert "new-token" not in caplog.text


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


# --------------------------------------------------------------------- #
# Rotation store precedence and re-mint recovery (donna-workspace#373)
# --------------------------------------------------------------------- #

def _token_response(status, body):
    return httpx.Response(status, json=body, request=httpx.Request("POST", oc.TOKEN_URL))


def _invalid_grant():
    return _token_response(400, {
        "error": "invalid_grant",
        "error_description": "AADSTS70000: The user could not be authenticated as the grant is expired.",
    })


def test_stored_token_is_preferred_over_the_env_seed(tmp_path, monkeypatch):
    """The store is the live value; the env var is only the seed.

    Job containers are ephemeral and get OUTLOOK_REFRESH_TOKEN fresh from
    `env_file:` on every start, so the env var goes stale the moment Microsoft
    rotates. Whatever the last run persisted has to win.
    """
    store = tmp_path / "outlook_token.env"
    store.write_text("OUTLOOK_REFRESH_TOKEN=rotated-by-last-run\n")
    monkeypatch.setenv("OUTLOOK_ENV_PATH", str(store))
    monkeypatch.setenv("OUTLOOK_REFRESH_TOKEN", "stale-seed-from-env-file")

    sent = []

    def fake_post(url, data=None, **kw):
        sent.append(data["refresh_token"])
        return _token_response(200, {"access_token": "access-123"})

    monkeypatch.setattr(httpx, "post", fake_post)

    assert oc._get_access_token() == "access-123"
    assert sent == ["rotated-by-last-run"]


def test_falls_back_to_env_seed_when_stored_token_is_dead(tmp_path, monkeypatch):
    """A re-mint writes the env file, not the store — so the store goes dead.

    After Tom re-mints interactively, the store still holds the revoked token.
    Without this fallback the fix would be inert until someone deleted the
    store by hand, which is precisely the kind of undocumented step that
    turns a five-minute recovery into an outage.
    """
    store = tmp_path / "outlook_token.env"
    store.write_text("OUTLOOK_REFRESH_TOKEN=revoked-token\n")
    monkeypatch.setenv("OUTLOOK_ENV_PATH", str(store))
    monkeypatch.setenv("OUTLOOK_REFRESH_TOKEN", "freshly-minted")

    sent = []

    def fake_post(url, data=None, **kw):
        rt = data["refresh_token"]
        sent.append(rt)
        if rt == "revoked-token":
            return _invalid_grant()
        return _token_response(200, {"access_token": "access-123", "refresh_token": "rotated-again"})

    monkeypatch.setattr(httpx, "post", fake_post)

    assert oc._get_access_token() == "access-123"
    assert sent == ["revoked-token", "freshly-minted"]
    # recovery is persisted, so the next run starts from the good token
    assert "OUTLOOK_REFRESH_TOKEN=rotated-again" in store.read_text()


def test_invalid_grant_on_the_env_seed_propagates(tmp_path, monkeypatch):
    """Both tokens dead means genuine re-consent — it must fail loudly."""
    store = tmp_path / "outlook_token.env"
    store.write_text("OUTLOOK_REFRESH_TOKEN=revoked-token\n")
    monkeypatch.setenv("OUTLOOK_ENV_PATH", str(store))
    monkeypatch.setenv("OUTLOOK_REFRESH_TOKEN", "also-revoked")

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _invalid_grant())

    with pytest.raises(httpx.HTTPStatusError):
        oc._get_access_token()


def test_only_one_exchange_when_store_matches_the_env_seed(tmp_path, monkeypatch):
    """No pointless second attempt with an identical token."""
    store = tmp_path / "outlook_token.env"
    store.write_text("OUTLOOK_REFRESH_TOKEN=same-token\n")
    monkeypatch.setenv("OUTLOOK_ENV_PATH", str(store))
    monkeypatch.setenv("OUTLOOK_REFRESH_TOKEN", "same-token")

    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (calls.append(1), _invalid_grant())[1])

    with pytest.raises(httpx.HTTPStatusError):
        oc._get_access_token()
    assert len(calls) == 1


def test_non_auth_http_error_is_not_retried_with_the_seed(tmp_path, monkeypatch):
    """A 500 from the token endpoint is not a dead credential."""
    store = tmp_path / "outlook_token.env"
    store.write_text("OUTLOOK_REFRESH_TOKEN=stored\n")
    monkeypatch.setenv("OUTLOOK_ENV_PATH", str(store))
    monkeypatch.setenv("OUTLOOK_REFRESH_TOKEN", "seed")

    calls = []

    def fake_post(url, data=None, **kw):
        calls.append(data["refresh_token"])
        return _token_response(500, {"error": "server_error"})

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(httpx.HTTPStatusError):
        oc._get_access_token()
    assert calls == ["stored"]


# --------------------------------------------------------------------- #
# Category tagging + path-aware folder moves (donna-workspace#371)
# --------------------------------------------------------------------- #

from unittest.mock import MagicMock, patch


def _resp(json_body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body
    r.raise_for_status.return_value = None
    return r


@patch.object(oc, "_get_access_token", return_value="tok")
def test_add_category_appends_without_touching_read_state(_tok):
    with patch.object(oc.httpx, "get", return_value=_resp({"categories": ["Blue"]})), \
         patch.object(oc.httpx, "patch", return_value=_resp({})) as p:
        oc.add_category("m1", "Action Required")
    # only the categories property changes -- no isRead, no folder move
    assert p.call_args.kwargs["json"] == {"categories": ["Blue", "Action Required"]}


@patch.object(oc, "_get_access_token", return_value="tok")
def test_add_category_idempotent(_tok):
    with patch.object(oc.httpx, "get", return_value=_resp({"categories": ["Action Required"]})), \
         patch.object(oc.httpx, "patch") as p:
        oc.add_category("m1", "Action Required")
    p.assert_not_called()


@patch.object(oc, "_get_access_token", return_value="tok")
def test_remove_category_strips_only_that_category(_tok):
    with patch.object(oc.httpx, "get", return_value=_resp({"categories": ["Action Required", "Blue"]})), \
         patch.object(oc.httpx, "patch", return_value=_resp({})) as p:
        oc.remove_category("m1", "Action Required")
    assert p.call_args.kwargs["json"] == {"categories": ["Blue"]}


@patch.object(oc, "_get_access_token", return_value="tok")
def test_remove_category_absent_is_noop(_tok):
    with patch.object(oc.httpx, "get", return_value=_resp({"categories": []})), \
         patch.object(oc.httpx, "patch") as p:
        oc.remove_category("m1", "Action Required")
    p.assert_not_called()


@patch.object(oc, "mark_read")
@patch.object(oc, "_get_access_token", return_value="tok")
def test_move_to_folder_slashed_name_is_literal(_tok, _mr):
    # Tom's "Scouting/General" is one top-level folder with "/" in its
    # displayName (#371, Tom: literal only, "to prevent overlap") -- the
    # name must never be path-split into a nested Scouting -> General tree.
    with patch.object(oc.httpx, "get", return_value=_resp({"value": [{"id": "F-literal", "displayName": "Scouting/General"}]})) as g, \
         patch.object(oc.httpx, "post", return_value=_resp({})) as p:
        oc.move_to_folder("m1", "Scouting/General")
    assert len(g.call_args_list) == 1  # one top-level lookup, no child walk
    assert p.call_args.kwargs["json"] == {"destinationId": "F-literal"}


@patch.object(oc, "mark_read")
@patch.object(oc, "_get_access_token", return_value="tok")
def test_move_to_folder_missing_slashed_name_created_literally(_tok, _mr):
    posts = [
        _resp({"id": "F-created"}),  # created top-level, slash included
        _resp({}),                   # the move
    ]
    with patch.object(oc.httpx, "get", return_value=_resp({"value": []})), \
         patch.object(oc.httpx, "post", side_effect=posts) as p:
        oc.move_to_folder("m1", "Scouting/General")
    assert p.call_args_list[0][0][0].endswith("/me/mailFolders")  # top level
    assert p.call_args_list[0].kwargs["json"]["displayName"] == "Scouting/General"
    assert p.call_args_list[1].kwargs["json"] == {"destinationId": "F-created"}


@patch.object(oc, "mark_read")
@patch.object(oc, "_get_access_token", return_value="tok")
def test_move_to_folder_flat_name_unchanged(_tok, _mr):
    with patch.object(oc.httpx, "get", return_value=_resp({"value": [{"id": "F-fin", "displayName": "Finance"}]})) as g, \
         patch.object(oc.httpx, "post", return_value=_resp({})) as p:
        oc.move_to_folder("m1", "Finance")
    assert g.call_args_list[0][0][0].endswith("/me/mailFolders")
    assert p.call_args.kwargs["json"] == {"destinationId": "F-fin"}

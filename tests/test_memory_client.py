import pytest

from agent_toolkit import memory_client as mc


@pytest.fixture(autouse=True)
def _memory_env(monkeypatch):
    monkeypatch.setenv("MEMORY_AGENT", "gretchen")
    monkeypatch.setenv("MEMORY_DEFAULT_PROJECT", "scoutmaster")


def test_agent_required(monkeypatch):
    monkeypatch.delenv("MEMORY_AGENT", raising=False)
    with pytest.raises(RuntimeError):
        mc._agent()


def test_default_project_is_none_when_unset(monkeypatch):
    # Not required, unlike _agent() — a single-domain agent (e.g. gretchen)
    # legitimately has no MEMORY_DEFAULT_PROJECT, and the API's /search and
    # /recent both treat an absent project as "search everything", not an
    # error. Requiring one broke every search/recent call for such an agent
    # (found live wiring Gretchen, ClaudeAIScoutMaster#277).
    monkeypatch.delenv("MEMORY_DEFAULT_PROJECT", raising=False)
    assert mc._default_project() is None


def test_store_memory_degrades_to_zero_when_agent_unset(monkeypatch):
    monkeypatch.delenv("MEMORY_AGENT", raising=False)
    assert mc.store_memory("id-1", "some text", "note") == 0


def _capture_post(monkeypatch):
    calls = []

    def fake_api_post(path, json):
        calls.append((path, json))

        class _Resp:
            def json(self):
                return {"hits": [], "documents": [], "chunks": 3}
        return _Resp()

    monkeypatch.setattr(mc, "_api_post", fake_api_post)
    return calls


def test_store_memory_sends_configured_agent_and_project(monkeypatch):
    calls = _capture_post(monkeypatch)
    mc.store_memory("id-1", "some text", "note")

    path, body = calls[0]
    assert path == "/remember"
    assert body["agent"] == "gretchen"
    assert body["project"] == "scoutmaster"


def test_search_handbook_requests_the_handbook_kind_server_side(monkeypatch):
    calls = _capture_post(monkeypatch)
    mc.search_handbook("knife safety", top_k=7)

    path, body = calls[0]
    assert path == "/search"
    assert body["agent"] == "gretchen"
    assert body["project"] == "scoutmaster"
    assert body["kind"] == "handbook"
    assert body["limit"] == 7


def test_search_handbook_returns_hits_flattened(monkeypatch):
    def fake_api_post(path, json):
        class _Resp:
            def json(self):
                return {"hits": [
                    {"score": 0.9, "text": "Safe Knife Use",
                     "metadata": {"chapter": 12, "title": "Tools"}, "project": "troop208"},
                ]}
        return _Resp()

    monkeypatch.setattr(mc, "_api_post", fake_api_post)
    results = mc.search_handbook("knife safety")
    assert results == [{"score": 0.9, "text": "Safe Knife Use",
                        "chapter": 12, "title": "Tools", "project": "troop208"}]


def test_search_handbook_degrades_to_empty_list_on_error(monkeypatch):
    def raise_error(path, json):
        raise RuntimeError("API unavailable")

    monkeypatch.setattr(mc, "_api_post", raise_error)
    assert mc.search_handbook("knife safety") == []


def test_search_hoa_uses_hardcoded_project_but_configured_agent(monkeypatch):
    calls = _capture_post(monkeypatch)
    mc.search_hoa("water heater")

    path, body = calls[0]
    assert path == "/search"
    assert body["agent"] == "gretchen"  # not hardcoded — configured per consumer
    assert body["project"] == "hoa_westmoreland"  # always this, regardless of consumer


def test_search_shared_memory_always_uses_harvey_regardless_of_consumer(monkeypatch):
    calls = _capture_post(monkeypatch)
    mc.search_shared_memory("deploy process")

    path, body = calls[0]
    assert body["agent"] == "harvey"
    assert "project" not in body


def test_search_memory_filters_out_sent_comm_and_recipe_types(monkeypatch):
    def fake_api_post(path, json):
        class _Resp:
            def json(self):
                return {"hits": [
                    {"score": 0.9, "text": "a", "metadata": {"memory_type": "chat"}},
                    {"score": 0.8, "text": "b", "metadata": {"memory_type": "sent_comm"}},
                    {"score": 0.7, "text": "c", "metadata": {"memory_type": "recipe"}},
                ]}
        return _Resp()

    monkeypatch.setattr(mc, "_api_post", fake_api_post)
    results = mc.search_memory("query")

    assert len(results) == 1
    assert results[0]["memory_type"] == "chat"


def test_delete_memory_never_raises_on_api_error(monkeypatch):
    def raises(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(mc, "_api_post", raises)
    mc.delete_memory("id-1")  # does not raise


def test_store_document_returns_fail_loud_error_dict(monkeypatch):
    def raises(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(mc, "_api_post", raises)
    result = mc.store_document("path/to/doc", "text", kind="document")

    assert result == {"ok": False, "error": "api down"}


def test_store_document_relays_a_real_timeout_exceptions_message(monkeypatch):
    """Regression test for the exact live failure (2026-09-07): a large PDF
    ingested right after bge-m3 moved to CPU inference (~3-4x slower per
    embedded chunk) blew past the client's timeout and store_document
    surfaced str(exc) == "timed out" verbatim, matching httpx's own
    TimeoutException message -- this is what the fail-loud contract above
    actually produces for a real network timeout, not just a generic
    RuntimeError."""
    import httpx

    def raises(*a, **k):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(mc, "_api_post", raises)
    result = mc.store_document("path/to/doc", "text", kind="document")

    assert result == {"ok": False, "error": "timed out"}


def test_http_client_timeout_is_90s_not_30s(monkeypatch):
    """Regression test (2026-09-07): store_document failed live on a real
    multi-page PDF ("timed out") once bge-m3 moved from GPU to CPU
    inference -- CPU embedding is ~3-4x slower per chunk, and a big
    document's aggregate embed time crossed the old 30s ceiling. 90s is
    the fix; this pins the actual configured value so a future edit can't
    silently drop it back down."""
    monkeypatch.setattr(mc, "_client", None)
    client = mc._get_http_client()
    try:
        assert client.timeout.connect == 90
        assert client.timeout.read == 90
    finally:
        monkeypatch.setattr(mc, "_client", None)


def test_store_document_project_override_beats_default(monkeypatch):
    calls = _capture_post(monkeypatch)
    mc.store_document("path", "text", kind="document", project="custom_project")

    _, body = calls[0]
    assert body["project"] == "custom_project"


def _capture_rerank_post(monkeypatch):
    """Run _rerank against a stubbed Ollama, returning the JSON body sent."""
    sent = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "[2,1]"}

    def _fake_post(url, json=None, timeout=None):
        sent["url"] = url
        sent["body"] = json
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "post", _fake_post)
    results = [{"text": "first"}, {"text": "second"}]
    mc._rerank("q", results, top_k=2)
    return sent


def test_rerank_disables_thinking(monkeypatch):
    # aiserver-stack#158. Without think=False a thinking-capable model spends
    # the entire timeout on hidden chain-of-thought and never returns the
    # array, and _rerank's except-branch swallows that into "original order".
    # The rerank then silently does nothing forever, which is indistinguishable
    # from a working one at the call site. Measured on aiserver: gemma4:12b
    # exceeded 60s without the flag, 1.2s with it.
    sent = _capture_rerank_post(monkeypatch)
    assert sent["body"]["think"] is False


def test_rerank_defaults_to_the_resident_model(monkeypatch):
    # A second, smaller rerank model has to fit beside the host's resident set;
    # on a 12 GB card loading phi4-mini (3.1 GB) beside gemma4:12b + bge-m3
    # evicted both. Reusing the resident model costs no VRAM.
    monkeypatch.delenv("RERANK_MODEL", raising=False)
    sent = _capture_rerank_post(monkeypatch)
    assert sent["body"]["model"] == "gemma4:12b-it-q4_K_M"


def test_rerank_model_is_overridable(monkeypatch):
    monkeypatch.setenv("RERANK_MODEL", "phi4-mini")
    sent = _capture_rerank_post(monkeypatch)
    assert sent["body"]["model"] == "phi4-mini"

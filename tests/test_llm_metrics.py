import pytest

from agent_toolkit import llm_metrics


class _FakePipeline:
    def __init__(self, redis):
        self._redis = redis

    def hincrby(self, key, field, amount):
        self._redis.hincrby(key, field, amount)

    def execute(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeRedis:
    def __init__(self):
        self._hashes: dict = {}

    def hincrby(self, key, field, amount):
        h = self._hashes.setdefault(key, {})
        h[field] = str(int(h.get(field, 0)) + amount)
        return int(h[field])

    def hgetall(self, key):
        return dict(self._hashes.get(key, {}))

    def pipeline(self):
        return _FakePipeline(self)


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(llm_metrics, "_get_client", lambda: fake)
    return fake


def test_record_and_render_requires_metrics_agent(monkeypatch):
    monkeypatch.delenv("METRICS_AGENT", raising=False)
    with pytest.raises(RuntimeError):
        llm_metrics._agent()
    # render() degrades to "no data" rather than raising through to /metrics.
    assert llm_metrics.render() == ""


def test_two_agents_sharing_one_redis_do_not_collide(monkeypatch):
    """Regression test: the source this was ported from (ClaudeAIScoutMaster's
    app/llm_metrics.py) hardcoded AGENT="donna" as a render-time label only —
    the underlying Redis keys were fixed strings, so a second agent sharing
    the same Redis (the default REDIS_URL has no per-agent override) would
    have its counters merged into the first agent's."""
    monkeypatch.setenv("METRICS_AGENT", "donna")
    llm_metrics.record_tokens("qwen3.5:9b", prompt_tokens=100, completion_tokens=50)

    monkeypatch.setenv("METRICS_AGENT", "gretchen")
    llm_metrics.record_tokens("qwen3.5:9b", prompt_tokens=10, completion_tokens=5)

    monkeypatch.setenv("METRICS_AGENT", "donna")
    donna_output = llm_metrics.render()
    assert 'agent="donna"' in donna_output
    assert 'agent="gretchen"' not in donna_output
    assert 'llm_tokens_total{agent="donna",model="qwen3.5:9b",direction="prompt"} 100' in donna_output

    monkeypatch.setenv("METRICS_AGENT", "gretchen")
    gretchen_output = llm_metrics.render()
    assert 'agent="gretchen"' in gretchen_output
    assert 'agent="donna"' not in gretchen_output
    assert 'llm_tokens_total{agent="gretchen",model="qwen3.5:9b",direction="prompt"} 10' in gretchen_output


def test_record_call_and_render_counters(monkeypatch):
    monkeypatch.setenv("METRICS_AGENT", "donna")
    llm_metrics.record_call("qwen3.5:9b", "assistant", "ok")
    llm_metrics.record_call("qwen3.5:9b", "assistant", "ok")
    llm_metrics.record_call("qwen3.5:9b", "assistant", "error")

    out = llm_metrics.render()
    assert 'llm_calls_total{agent="donna",model="qwen3.5:9b",worker="assistant",status="ok"} 2' in out
    assert 'llm_calls_total{agent="donna",model="qwen3.5:9b",worker="assistant",status="error"} 1' in out


def test_record_duration_renders_histogram_buckets(monkeypatch):
    monkeypatch.setenv("METRICS_AGENT", "donna")
    llm_metrics.record_duration("qwen3.5:9b", "assistant", seconds=0.05)

    out = llm_metrics.render()
    assert "llm_call_duration_seconds_bucket" in out
    assert 'llm_call_duration_seconds_count{agent="donna",model="qwen3.5:9b",worker="assistant"} 1' in out
    assert 'llm_call_duration_seconds_sum{agent="donna",model="qwen3.5:9b",worker="assistant"} 0.05' in out


def test_recording_never_raises_on_redis_failure(monkeypatch):
    monkeypatch.setenv("METRICS_AGENT", "donna")

    class _BrokenRedis:
        def hincrby(self, *a, **k):
            raise ConnectionError("redis down")

        def pipeline(self):
            raise ConnectionError("redis down")

    monkeypatch.setattr(llm_metrics, "_get_client", lambda: _BrokenRedis())

    llm_metrics.record_tokens("m", 1, 1)  # does not raise
    llm_metrics.record_call("m", "w", "ok")  # does not raise
    llm_metrics.record_duration("m", "w", 1.0)  # does not raise

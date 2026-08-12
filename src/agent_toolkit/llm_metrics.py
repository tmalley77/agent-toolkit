"""Cross-process counters for an agent's own LLM calls, exposed on /metrics.

Ported from ClaudeAIScoutMaster's app/llm_metrics.py (see
ClaudeAIScoutMaster#277) with one real fix, not just a naming tweak: the
source hardcoded `AGENT = "donna"` and used *fixed* Redis key names
(`llm_metrics:tokens`, `llm_metrics:calls`, `llm_metrics:durations`) —
`AGENT` was applied only at render() time as a Prometheus label, not baked
into the storage keys themselves. If a second agent (Gretchen) shared the
same Redis instance — the default `REDIS_URL` here has no per-agent
override, so sharing is the default unless a consumer explicitly
configures otherwise — both agents' counters would land in the *same*
Redis hashes, and each agent's `render()` would report the combined total
under its own hardcoded label. `METRICS_AGENT` (required, no default) is
now baked into every storage key (`llm_metrics:{agent}:tokens`, etc.), not
just the render-time label, so two agents sharing one Redis genuinely
don't collide.

Every call funnels through a small number of choke points regardless of
which of many callers triggered it, but those callers can run in separate
processes (e.g. a bot process, an API process, a scheduler: separate
containers, no IPC between them) — an in-process counter would only ever
see whatever the current process itself handled directly. Redis is
typically the one thing all those processes already share, so counters
live there instead.

Recording must never be why an LLM call fails or /metrics 500s: every
Redis operation here is wrapped, and a Redis outage (or an unset
METRICS_AGENT) degrades to "no data" (a scrape sees zero series) rather
than raising through to the caller.
"""
import logging
import os

import redis

logger = logging.getLogger(__name__)

# Milliseconds. A generic spread from sub-second local/rerank calls up to a
# ~60s ceiling for a badly regressed frontier call; consumers with a
# different latency profile can still read this module's output correctly
# since bucket bounds are just labels, not enforced anywhere.
_DURATION_BUCKETS_MS = [100, 250, 500, 1000, 2000, 5000, 10000, 20000, 30000, 60000]

_client: "redis.Redis | None" = None


def _agent() -> str:
    agent = os.getenv("METRICS_AGENT", "")
    if not agent:
        raise RuntimeError("METRICS_AGENT must be set (e.g. 'donna', 'gretchen')")
    return agent


def _tokens_key() -> str:
    return f"llm_metrics:{_agent()}:tokens"


def _calls_key() -> str:
    return f"llm_metrics:{_agent()}:calls"


def _durations_key() -> str:
    return f"llm_metrics:{_agent()}:durations"


def _get_client() -> "redis.Redis":
    global _client
    if _client is None:
        url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        _client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2)
    return _client


def record_tokens(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Call once per successful LLM response, with that response's own
    token counts — never estimated client-side."""
    try:
        c = _get_client()
        with c.pipeline() as pipe:
            pipe.hincrby(_tokens_key(), f"{model}|prompt", prompt_tokens or 0)
            pipe.hincrby(_tokens_key(), f"{model}|completion", completion_tokens or 0)
            pipe.execute()
    except Exception:
        logger.warning("llm_metrics: failed to record tokens (model=%s)", model, exc_info=True)


def record_call(model: str, worker: str, status: str) -> None:
    """status is "ok" or "error", scoped to the LLM HTTP call itself — a
    downstream parsing/business-logic failure after a successful response is
    not a call error, since the model did its job."""
    try:
        _get_client().hincrby(_calls_key(), f"{model}|{worker}|{status}", 1)
    except Exception:
        logger.warning(
            "llm_metrics: failed to record call (model=%s worker=%s status=%s)",
            model, worker, status, exc_info=True,
        )


def record_duration(model: str, worker: str, seconds: float) -> None:
    """Call once per LLM HTTP call, timed around the exact same span as the
    record_call() next to it (including the error path -- how long a call
    took before failing is still latency), so a duration sample and its
    ok/error status always correspond 1:1.

    Stored as integer milliseconds via HINCRBY, not HINCRBYFLOAT, so a
    simple hincrby-only fake Redis suffices in tests. Converted back to
    seconds (Prometheus's convention) at render time.
    """
    try:
        ms = max(0, int(round((seconds or 0) * 1000)))
        c = _get_client()
        with c.pipeline() as pipe:
            pipe.hincrby(_durations_key(), f"{model}|{worker}|sum_ms", ms)
            pipe.hincrby(_durations_key(), f"{model}|{worker}|count", 1)
            # Cumulative buckets: every bound >= this observation gets +1,
            # matching Prometheus's le="X" = "count of observations <= X".
            for bound in _DURATION_BUCKETS_MS:
                if ms <= bound:
                    pipe.hincrby(_durations_key(), f"{model}|{worker}|le_{bound}", 1)
            pipe.execute()
    except Exception:
        logger.warning(
            "llm_metrics: failed to record duration (model=%s worker=%s)",
            model, worker, exc_info=True,
        )


def _format_bucket_bound(bound_ms: int) -> str:
    # Prometheus convention: le="1", not le="1.0" -- avoids a whole-number
    # bucket becoming a different label value than its float form if this
    # list's precision ever changes.
    return f"{bound_ms / 1000:g}"


def _render_duration_histogram(agent: str, durations: dict) -> list[str]:
    series: dict[tuple[str, str], dict] = {}
    for field, v in durations.items():
        model, worker, kind = field.split("|", 2)
        s = series.setdefault((model, worker), {"sum_ms": 0, "count": 0, "buckets": {}})
        if kind == "sum_ms":
            s["sum_ms"] = int(v)
        elif kind == "count":
            s["count"] = int(v)
        elif kind.startswith("le_"):
            s["buckets"][int(kind[len("le_"):])] = int(v)

    out = [
        "# HELP llm_call_duration_seconds LLM call latency by agent, model and worker, since Redis was last reset.",
        "# TYPE llm_call_duration_seconds histogram",
    ]
    for (model, worker), s in sorted(series.items()):
        labels = f'agent="{agent}",model="{model}",worker="{worker}"'
        for bound_ms in _DURATION_BUCKETS_MS:
            le = _format_bucket_bound(bound_ms)
            out.append(f'llm_call_duration_seconds_bucket{{{labels},le="{le}"}} {s["buckets"].get(bound_ms, 0)}')
        out.append(f'llm_call_duration_seconds_bucket{{{labels},le="+Inf"}} {s["count"]}')
        out.append(f'llm_call_duration_seconds_sum{{{labels}}} {s["sum_ms"] / 1000}')
        out.append(f'llm_call_duration_seconds_count{{{labels}}} {s["count"]}')
    return out


def render() -> str:
    try:
        agent = _agent()
        c = _get_client()
        tokens = c.hgetall(_tokens_key())
        calls = c.hgetall(_calls_key())
        durations = c.hgetall(_durations_key())
    except Exception:
        logger.warning("llm_metrics: failed to read counters for /metrics", exc_info=True)
        return ""

    out = []
    if tokens:
        out.append("# HELP llm_tokens_total LLM tokens by agent, model and direction, since Redis was last reset.")
        out.append("# TYPE llm_tokens_total counter")
        for field, v in sorted(tokens.items()):
            model, direction = field.rsplit("|", 1)
            out.append(f'llm_tokens_total{{agent="{agent}",model="{model}",direction="{direction}"}} {v}')
    if calls:
        out.append("# HELP llm_calls_total LLM calls by agent, model, worker and status, since Redis was last reset.")
        out.append("# TYPE llm_calls_total counter")
        for field, v in sorted(calls.items()):
            model, worker, status = field.rsplit("|", 2)
            out.append(f'llm_calls_total{{agent="{agent}",model="{model}",worker="{worker}",status="{status}"}} {v}')
    if durations:
        out.extend(_render_duration_histogram(agent, durations))
    return ("\n".join(out) + "\n") if out else ""

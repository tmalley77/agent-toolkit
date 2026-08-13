"""Semantic memory client — HTTP against aiserver's shared memory API.

Ported from ClaudeAIScoutMaster's app/memory.py (see ClaudeAIScoutMaster#277).
Generalizing this one isn't pure mechanical extraction the way outlook_client/
gmail_client were — it's the actual implementation of the `agent="gretchen"` /
`project="personal"` memory-split decision from that issue, so treat this as
that decision landing, not infra housekeeping done on the side.

`MEMORY_AGENT` replaces the source's hardcoded `AGENT = "donna"` — required,
no default, same posture as `AGENT_DB_PATH`/`METRICS_AGENT`. Both getters
are looked up lazily inside each function's existing
try/except-and-degrade-gracefully block (an unset env var surfaces as "API
unavailable" — a warning log and an empty/None/0 return — exactly like a
real aiserver outage, never a hard crash at import time).

`MEMORY_DEFAULT_PROJECT` replaces `DEFAULT_PROJECT = "scoutmaster"` but is
genuinely OPTIONAL, unlike the above — found live 2026-08-12 wiring
Gretchen (ClaudeAIScoutMaster#277): the API's project-partitioning
(`services/api/projects.py`) only exists for Donna's multi-domain memory;
"most agents get one collection and no further division" per that module's
own docstring. `services/api/main.py`'s `/search` and `/recent` handlers
both 400 on a project value for an agent that isn't registered in
`PROJECTS` — treating an unrecognized project as a likely typo, not a
no-op (asymmetric with `/remember`'s `resolve_project`, which silently
drops it either way). Requiring every consumer to invent a project value
therefore actively broke every search/recent call for a single-domain
agent. `_default_project()` returns `None` when unset — a legitimate value
call sites pass straight through, matching "omitting this searches across
everything" for `/search`/`/recent`. Donna's own behavior is unchanged
(she always sets `MEMORY_DEFAULT_PROJECT=scoutmaster`); only consumers
that leave it unset are affected, and for those this fixes a live bug
rather than changing working behavior.

Two sets of hardcoded values are deliberately NOT parameterized, kept as
literals: `agent="harvey"` in `search_shared_memory`/`list_shared_memory`/
`read_shared_memory` (Harvey's own synced architecture/ops notes — a fixed
third resource any consumer can read, unrelated to which agent this module
instance belongs to) and `project="hoa_westmoreland"` in `search_hoa`/
`get_latest_hoa_documents` (HOA is Donna-only per ClaudeAIScoutMaster#277's
decisions — no reason to make it configurable for a domain only one
consumer has).

A third fixed cross-agent read, added 2026-08-12 (Tom: "she needs access to
scoutmaster memory like donna"): `search_donna_scoutmaster_memory` hardcodes
`agent="donna", project="scoutmaster"` — read-only access into Donna's
existing troop-knowledge partition (1000+ documents accumulated before the
Donna/Gretchen split) for Gretchen, same shape as `search_shared_memory`.
Deliberately read-through, not a data migration — Donna's collection stays
the source of truth, nothing copied into `gretchen_memories`. A real
migration (Gretchen owns it outright, Donna stops seeing it) was the
original split plan's language ("agent=gretchen migrated from
agent=donna/project=scoutmaster") but is a much bigger, less reversible
data operation on 1000+ live documents — deferred as a separate decision,
not assumed here. Known related gap, not fixed as part of this: Donna's
own `MEMORY_DEFAULT_PROJECT` is still `scoutmaster` (line ~30 above) even
though she no longer handles scout mail post-split — every new memory she
writes without an explicit project override still lands in the same
partition this function reads, so Gretchen would see Donna's ongoing
personal content bleed into "scoutmaster" unless that's fixed separately.

Not yet wired into any live consumer — porting this does not change
donna-workspace's live `app/memory.py`, which still hardcodes AGENT/
DEFAULT_PROJECT today. That swap is real production behavior change (every
memory-reliant feature — grounding, HOA answers, recipes, event debriefs —
routes through this) and belongs with the actual Donna/Gretchen cutover,
not bundled into an extraction pass.
"""
import logging
import os
from typing import Optional

import httpx

from agent_toolkit import llm_metrics

logger = logging.getLogger(__name__)

# donna-bot/donna/donna-scheduler-style siblings and Caddy are typically
# colocated on the same Docker network as the memory API, so "caddy:80"
# reaches Caddy's internal listen port directly, while still routing through
# Caddy's basic_auth, which is the ONLY auth the API container has. Do NOT
# point this at the API container directly -- that bypasses auth entirely.
AISERVER_API_URL = os.getenv("AISERVER_API_URL", "http://caddy:80/api")
CADDY_USER = os.getenv("CADDY_USER")
CADDY_PASSWORD = os.getenv("CADDY_PASSWORD")

# memory_type -> kind, for the handful of memory_types that need server-side
# exact filtering via /recent (recall_event_debrief, search_documents_by_filename).
# Everything else stores as kind="note" and stays distinguished only by
# metadata.memory_type.
_FILTERABLE_KIND = {"event_debrief": "event_debrief", "document": "document"}

_client: Optional[httpx.Client] = None


def _agent() -> str:
    agent = os.getenv("MEMORY_AGENT", "")
    if not agent:
        raise RuntimeError("MEMORY_AGENT must be set (e.g. 'donna', 'gretchen')")
    return agent


def _default_project() -> str | None:
    """None for single-domain agents (no MEMORY_DEFAULT_PROJECT set) — a
    legitimate value meaning "no project filter", not a misconfiguration.
    See the module docstring for why this must NOT raise like _agent()."""
    return os.getenv("MEMORY_DEFAULT_PROJECT") or None


def _get_http_client() -> httpx.Client:
    global _client
    if _client is None:
        auth = (CADDY_USER, CADDY_PASSWORD) if CADDY_USER and CADDY_PASSWORD else None
        _client = httpx.Client(base_url=AISERVER_API_URL, auth=auth, timeout=30)
    return _client


def _api_post(path: str, json: dict) -> httpx.Response:
    r = _get_http_client().post(path, json=json)
    r.raise_for_status()
    return r


def _hit_to_dict(hit: dict) -> dict:
    """Reconstruct the flat {"score": ..., **original_payload_fields} shape
    from a /search hit's {score, text, metadata, project, ...} shape."""
    out = {"score": hit.get("score"), "text": hit.get("text")}
    out.update(hit.get("metadata") or {})
    if hit.get("project"):
        out["project"] = hit["project"]
    return out


def _doc_to_dict(doc: dict) -> dict:
    """Same flattening as _hit_to_dict, for /recent's {text, kind, metadata,
    tags, source_path, created_at} shape — no `score` key, since /recent is
    not a similarity search."""
    out = {"text": doc.get("text")}
    out.update(doc.get("metadata") or {})
    return out


def init_memory():
    """Startup check only — aiserver's memory collections are provisioned
    platform-side, not created on demand. Logs a warning if the API is
    unreachable so memory features are known to degrade gracefully."""
    try:
        r = _get_http_client().get("/health")
        r.raise_for_status()
    except Exception as e:
        logger.warning("aiserver memory API unavailable at startup (%s). Memory features disabled until reachable.", e)


def store_memory(memory_id: str, text: str, memory_type: str, extra_metadata: dict = None, chunk_prefix: str = None) -> int:
    """Chunk (server-side), embed, and upsert a memory. Returns the number of
    chunks stored, 0 if the API is unavailable or MEMORY_AGENT/
    MEMORY_DEFAULT_PROJECT are unset.

    chunk_prefix is prepended to the whole text once before server-side
    chunking — the chunk carrying the prefix gets it in its embedding
    context; chunks further into a long document don't.
    """
    try:
        body = f"{chunk_prefix} {text}" if chunk_prefix else text
        metadata = {"memory_type": memory_type, **(extra_metadata or {})}
        kind = _FILTERABLE_KIND.get(memory_type, "note")
        r = _api_post("/remember", {
            "text": body,
            "session_id": "unused",
            "agent": _agent(),
            "project": _default_project(),
            "source_path": memory_id,
            "kind": kind,
            "supersede": True,
            "metadata": metadata,
        })
        return r.json().get("chunks", 0)
    except Exception as e:
        logger.warning("store_memory failed for %s: %s", memory_id, e)
        return 0


def search_documents_by_filename(keywords: list[str], top_k: int = 3) -> list[dict]:
    """Return the first chunk of documents whose filename contains any of the
    keywords (case-insensitive). Returns [] on error."""
    try:
        keywords_lower = [k.lower() for k in keywords if len(k) > 3]
        if not keywords_lower:
            return []
        r = _api_post("/recent", {"agent": _agent(), "project": _default_project(), "kind": "document", "limit": 1000})
        results, seen_files = [], set()
        for doc in r.json()["documents"]:
            meta = doc.get("metadata") or {}
            fn = (meta.get("filename") or "").lower()
            if any(kw in fn for kw in keywords_lower):
                if fn not in seen_files and meta.get("chunk_index", 0) == 0:
                    seen_files.add(fn)
                    results.append({"score": 0.95, **_doc_to_dict(doc)})
                    if len(results) >= top_k:
                        break
        return results
    except Exception as e:
        logger.warning("search_documents_by_filename failed: %s", e)
        return []


def search_sent_comms(query: str, top_k: int = 5, source: str = None, rerank: bool = False) -> list[dict]:
    """Semantic search over sent communications. Returns [] on error."""
    try:
        fetch_k = (top_k * 3 if rerank else top_k) * 10
        r = _api_post("/search", {
            "query": query, "agent": _agent(), "project": _default_project(),
            "kind": "sent_comm", "limit": fetch_k,
        })
        seen: dict = {}
        for h in r.json()["hits"]:
            meta = h.get("metadata") or {}
            if source and meta.get("source") != source:
                continue
            comm_id = meta.get("comm_id")
            if comm_id in seen:
                continue
            subject = meta.get("subject", "")
            seen[comm_id] = {
                "score": h["score"],
                "memory_type": "sent_comm",
                "text": f"Subject: {subject}\n{h.get('text', '')}",
                "subject": subject,
                "source": meta.get("source"),
                "sent_at": meta.get("sent_at"),
                "date": (meta.get("sent_at") or "")[:10],
                "event_id": meta.get("event_id"),
                "comm_id": comm_id,
                "snippet": meta.get("snippet", ""),
            }
            if len(seen) >= (top_k * 3 if rerank else top_k):
                break
        results = list(seen.values())
        if rerank and len(results) > 1:
            return _rerank(query, results, top_k)
        return results[:top_k]
    except Exception as e:
        logger.warning("search_sent_comms failed for query %r: %s", query[:60], e)
        return []


def search_memory(query: str, top_k: int = 5, memory_type: str = None, rerank: bool = False, min_score: float = None) -> list[dict]:
    """Top-k semantically similar memory chunks. Returns [] if the API is
    unavailable."""
    try:
        fetch_k = (top_k * 3 if rerank else top_k) * 4
        r = _api_post("/search", {
            "query": query, "agent": _agent(), "project": _default_project(), "limit": fetch_k,
        })
        results = []
        for h in r.json()["hits"]:
            meta = h.get("metadata") or {}
            if meta.get("memory_type") in ("sent_comm", "recipe"):
                continue
            if memory_type and meta.get("memory_type") != memory_type:
                continue
            results.append(_hit_to_dict(h))
        if min_score is not None:
            results = [r for r in results if r["score"] >= min_score]
        if rerank and len(results) > 1:
            return _rerank(query, results, top_k)
        return results[:top_k]
    except Exception as e:
        logger.warning("search_memory failed for query %r: %s", query[:60], e)
        return []


def search_shared_memory(query: str, top_k: int = 5) -> list[dict]:
    """Semantic search over Harvey's local architecture/ops notes, synced to
    aiserver's agent="harvey" collection. Returns [] on error or if the API
    is unavailable — same degrade-gracefully contract as search_memory.
    Deliberately hardcoded to agent="harvey" for every consumer — see the
    module docstring."""
    try:
        r = _api_post("/search", {"query": query, "agent": "harvey", "limit": top_k})
        return [_hit_to_dict(h) for h in r.json()["hits"]]
    except Exception as e:
        logger.warning("search_shared_memory failed for query %r: %s", query[:60], e)
        return []


def search_donna_scoutmaster_memory(query: str, top_k: int = 5) -> list[dict]:
    """Semantic search over Donna's existing "scoutmaster" project partition
    (troop knowledge accumulated before the Donna/Gretchen split). Returns
    [] on error or if the API is unavailable — same degrade-gracefully
    contract as search_memory. Deliberately hardcoded to
    agent="donna", project="scoutmaster" for every consumer — see the
    module docstring. Read-only: this does not write into the calling
    agent's own collection, so results are not remembered as this agent's
    own memory unless the caller explicitly stores them."""
    try:
        r = _api_post("/search", {
            "query": query, "agent": "donna", "project": "scoutmaster", "limit": top_k,
        })
        return [_hit_to_dict(h) for h in r.json()["hits"]]
    except Exception as e:
        logger.warning("search_donna_scoutmaster_memory failed for query %r: %s", query[:60], e)
        return []


def list_shared_memory() -> list[dict]:
    """All of Harvey's synced notes (name/description/source_file), for the
    no-query 'give me the index' case. Returns [] on error."""
    try:
        r = _api_post("/recent", {"agent": "harvey", "kind": "note", "limit": 200})
        return [_doc_to_dict(d) for d in r.json()["documents"]]
    except Exception as e:
        logger.warning("list_shared_memory failed: %s", e)
        return []


def read_shared_memory(filename: str) -> str | None:
    """Full text of one of Harvey's notes by filename (matches
    memory_sync.py's metadata.source_file). Returns None if not found or the
    API is unavailable."""
    try:
        r = _api_post("/recent", {"agent": "harvey", "kind": "note", "limit": 200})
        for doc in r.json()["documents"]:
            if (doc.get("metadata") or {}).get("source_file") == filename:
                return doc.get("text")
        return None
    except Exception as e:
        logger.warning("read_shared_memory failed for %r: %s", filename, e)
        return None


def search_camp_recipes(query: str, top_k: int = 10, meal_type: str = None, cooking_method: str = None) -> list[dict]:
    """Semantic search over camp recipes. Unlike search_memory, this DOES
    return recipe points. Returns [] on error."""
    try:
        r = _api_post("/search", {
            "query": query, "agent": _agent(), "project": _default_project(),
            "kind": "recipe", "limit": top_k * 3,
        })
        results = []
        for h in r.json()["hits"]:
            meta = h.get("metadata") or {}
            if meal_type and meta.get("meal_type") != meal_type:
                continue
            if cooking_method and meta.get("cooking_method") != cooking_method:
                continue
            results.append(_hit_to_dict(h))
        return results[:top_k]
    except Exception as e:
        logger.warning("search_camp_recipes failed for %r: %s", query[:60], e)
        return []


def search_hoa(query: str, top_k: int = 5) -> list[dict]:
    """Semantic search over HOA content. Read-only — writes happen through a
    separate ingest path. Returns [] on any error. Deliberately hardcoded to
    project="hoa_westmoreland" — see the module docstring."""
    try:
        r = _api_post("/search", {
            "query": query, "agent": _agent(), "project": "hoa_westmoreland", "limit": top_k,
        })
        return [_hit_to_dict(h) for h in r.json()["hits"]]
    except Exception as e:
        logger.warning("search_hoa failed for %r: %s", query[:60], e)
        return []


def get_latest_hoa_documents(doc_type: str, limit: int = 3) -> list[dict]:
    """Most recent HOA documents of a given doc_type, by payload `date` — NOT
    vector similarity. Payloads with no `date` are skipped. Returns [] on
    error."""
    try:
        r = _api_post("/recent", {"agent": _agent(), "project": "hoa_westmoreland", "limit": 500})
        dated = []
        for doc in r.json()["documents"]:
            meta = doc.get("metadata") or {}
            if meta.get("doc_type") == doc_type and meta.get("date"):
                dated.append(_doc_to_dict(doc))
        dated.sort(key=lambda payload: payload["date"], reverse=True)
        seen_docs = set()
        deduped = []
        for payload in dated:
            doc_key = payload.get("filename") or payload.get("original_id") or id(payload)
            if doc_key in seen_docs:
                continue
            seen_docs.add(doc_key)
            deduped.append(payload)
        return deduped[:limit]
    except Exception as e:
        logger.warning("get_latest_hoa_documents failed for doc_type=%r: %s", doc_type, e)
        return []


def get_camp_recipe(name: str) -> dict | None:
    """Full payload of the single best-matching camp recipe by name, or None."""
    try:
        results = search_camp_recipes(name, top_k=1)
        return results[0] if results else None
    except Exception as e:
        logger.warning("get_camp_recipe failed for %r: %s", name[:60], e)
        return None


def recall_event_debrief(event_id: int) -> str | None:
    """Debrief notes for a specific event ID. Returns text or None."""
    try:
        r = _api_post("/recent", {"agent": _agent(), "project": _default_project(), "kind": "event_debrief", "limit": 200})
        texts = [
            doc["text"] for doc in r.json()["documents"]
            if (doc.get("metadata") or {}).get("event_id") == event_id and doc.get("text")
        ]
        return "\n".join(texts) if texts else None
    except Exception as e:
        logger.warning("recall_event_debrief failed for event %d: %s", event_id, e)
        return None


def delete_memory(memory_id: str):
    """Tombstone every chunk stored under memory_id — /forget tombstones by
    source_path directly, no chunk-index guessing needed."""
    try:
        _api_post("/forget", {"source_path": memory_id, "agent": _agent()})
    except Exception as e:
        logger.warning("delete_memory failed for %s: %s", memory_id, e)


def store_document(source_path: str, text: str, kind: str, metadata: dict = None, project: str = None, tags: list[str] = None) -> dict:
    """Lower-level document store for callers doing their own structured
    ingest — same /remember + supersede mechanics as store_memory, but with
    caller-controlled kind/project/tags instead of store_memory's fixed
    note/default-project. Returns {"ok": True, ...} or
    {"ok": False, "error": str} — a fail-loud contract, unlike
    store_memory's swallow-and-return-0.
    """
    try:
        r = _api_post("/remember", {
            "text": text,
            "session_id": "unused",
            "agent": _agent(),
            "project": project or _default_project(),
            "source_path": source_path,
            "kind": kind,
            "supersede": True,
            "tags": tags or [],
            "metadata": metadata or {},
        })
        out = r.json()
        return {"ok": True, "chunks": out.get("chunks", 0), "source_path": source_path}
    except Exception as e:
        logger.error("store_document failed for source_path=%r: %s", source_path, e)
        return {"ok": False, "error": str(e)}


def _rerank(query: str, results: list[dict], top_k: int) -> list[dict]:
    """Re-rank search results using a small local model via Ollama. Falls
    back to original order on error — a direct Ollama call, not something
    aiserver's API does for the caller."""
    if len(results) <= 1:
        return results[:top_k]
    import httpx as _httpx
    import json as _json
    import time as _time
    OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
    RERANK_MODEL = os.getenv("RERANK_MODEL", "phi4-mini")
    numbered = "\n".join(f"{i+1}. {r.get('text', '')[:200]}" for i, r in enumerate(results))
    prompt = (
        f"Given the search query: \"{query}\"\n\n"
        f"Rank these search results from most to least relevant. "
        f"Return ONLY a JSON array of the original numbers in order of relevance, e.g. [3,1,2,5,4].\n\n"
        f"{numbered}"
    )
    _t0 = _time.perf_counter()
    try:
        resp = _httpx.post(f"{OLLAMA_URL}/api/generate", json={"model": RERANK_MODEL, "prompt": prompt, "stream": False}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        llm_metrics.record_duration(RERANK_MODEL, "rerank", _time.perf_counter() - _t0)
        llm_metrics.record_call(RERANK_MODEL, "rerank", "error")
        logger.warning("Rerank failed, returning original order: %s", e)
        return results[:top_k]

    # The Ollama call succeeded — recorded here regardless of what the ranking
    # parse below does with the response, since a malformed ranking is not an
    # Ollama-call failure.
    llm_metrics.record_duration(RERANK_MODEL, "rerank", _time.perf_counter() - _t0)
    llm_metrics.record_tokens(RERANK_MODEL, data.get("prompt_eval_count", 0), data.get("eval_count", 0))
    llm_metrics.record_call(RERANK_MODEL, "rerank", "ok")

    try:
        raw = data.get("response", "").strip()
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start >= 0 and end > start:
            ranking = _json.loads(raw[start:end])
            reordered = []
            for idx in ranking:
                i = int(idx) - 1
                if 0 <= i < len(results) and results[i] not in reordered:
                    reordered.append(results[i])
            for r in results:
                if r not in reordered:
                    reordered.append(r)
            return reordered[:top_k]
    except Exception as e:
        logger.warning("Rerank ranking-parse failed, returning original order: %s", e)
    return results[:top_k]

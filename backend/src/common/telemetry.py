"""LLM-call telemetry helpers: token-usage normalization and timing.

Reusable across agents to surface per-call token counts and durations in debug
output. Token usage is normalized into a uniform ``{prompt, completion, total}``
dict regardless of the source (neo4j-graphrag ``LLMUsage`` or Microsoft Agent
Framework ``UsageDetails``).
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any


def empty_usage() -> dict[str, int | None]:
    """Return a fresh, all-``None`` token-usage dict (unknown rather than zero)."""
    return {"prompt": None, "completion": None, "total": None}


def elapsed_ms(start: float) -> float:
    """Milliseconds elapsed since ``start`` (a :func:`time.perf_counter` value)."""
    return round((time.perf_counter() - start) * 1000, 1)


def normalize_llm_usage(usage: Any) -> dict[str, int | None]:
    """Normalize a neo4j-graphrag ``LLMUsage`` into a plain token dict."""
    if usage is None:
        return empty_usage()
    return {
        "prompt": getattr(usage, "request_tokens", None),
        "completion": getattr(usage, "response_tokens", None),
        "total": getattr(usage, "total_tokens", None),
    }


def normalize_maf_usage(usage: Any) -> dict[str, int | None]:
    """Normalize a Microsoft Agent Framework ``UsageDetails`` into a plain token dict.

    ``UsageDetails`` is a dict-like with ``input_token_count`` / ``output_token_count``
    / ``total_token_count`` keys; any may be absent if the endpoint does not report
    usage (e.g. some streaming responses), in which case the value stays ``None``.
    """
    if not usage:
        return empty_usage()
    return {
        "prompt": usage.get("input_token_count"),
        "completion": usage.get("output_token_count"),
        "total": usage.get("total_token_count"),
    }


def build_llm_messages(system: str | None, user: Any) -> list[dict[str, str]]:
    """Build a ``[{role, content}]`` request representation for debug telemetry."""
    request: list[dict[str, str]] = []
    if system:
        request.append({"role": "system", "content": str(system)})
    if user is not None:
        request.append({"role": "user", "content": str(user)})
    return request


def serialize_maf_messages(messages: Any) -> list[dict[str, str]]:
    """Flatten Microsoft Agent Framework ``Message`` objects to ``[{role, content}]``.

    Used to capture the *exact* request sent on each agent turn for debug telemetry,
    including tool-call and tool-result messages (so the answer turn faithfully shows
    the retrieved rows the model actually saw). Each message's text and any
    function-call / function-result contents are rendered into a single content string.
    """
    serialized: list[dict[str, str]] = []
    for message in messages or []:
        role = str(getattr(message, "role", "") or "")
        # ``Message.role`` may be an enum-like object; prefer its value when present.
        role = str(getattr(role, "value", role))
        parts: list[str] = []
        for content in getattr(message, "contents", None) or []:
            ctype = getattr(content, "type", None)
            if ctype == "function_call":
                name = getattr(content, "name", "") or ""
                arguments = getattr(content, "arguments", "")
                parts.append(f"→ call {name}({arguments})")
            elif ctype == "function_result":
                result = getattr(content, "result", "")
                parts.append(f"← result: {result}")
            elif ctype == "text":
                text = getattr(content, "text", "") or ""
                if text:
                    parts.append(text)
        if not parts:
            text = getattr(message, "text", "") or ""
            if text:
                parts.append(text)
        serialized.append({"role": role, "content": "\n".join(parts)})
    return serialized


# Per-request sink for token usage from LLM calls that don't otherwise surface it
# (e.g. neo4j-graphrag's internal cypher-generation ``llm.invoke``). A recorder
# appends normalized usage here when a sink is active. ``asyncio.to_thread`` copies
# the calling context into the worker thread, and because the value is a shared
# mutable list, appends made inside the thread are visible to the request task
# afterwards. Each request binds its own list, so concurrent requests stay isolated.
usage_sink: ContextVar[list[dict[str, Any]] | None] = ContextVar("llm_usage_sink", default=None)

# Per-request sink for the knowledge-graph retrieval tool's output. When the MAF
# agent invokes the ``search_knowledge_graph`` tool mid-run, the tool appends its
# generated Cypher, rows and timing here so the request task can emit the
# ``metadata`` event and ``stats`` timings without re-running retrieval. Bound per
# request (like :data:`usage_sink`) so concurrent requests stay isolated.
retrieval_sink: ContextVar[list[dict[str, Any]] | None] = ContextVar("kg_retrieval_sink", default=None)

# Per-request sink for the MAF agent's per-turn LLM calls. A ``ChatMiddleware`` fires
# once per agent turn (tool-planning, then answer generation) and appends each turn's
# stage, normalized usage, duration and request messages here. This is needed because
# MAF aggregates ``usage_details`` across all turns of a run, which would otherwise
# collapse the distinct planning and answer LLM calls into a single figure. Bound per
# request (like :data:`usage_sink`) so concurrent requests stay isolated.
maf_call_sink: ContextVar[list[dict[str, Any]] | None] = ContextVar("kg_maf_call_sink", default=None)

# Per-request callback used to surface live pipeline phase changes to the streaming
# ``/ask`` response. The agent's pipeline (tool-planning, cypher generation, graph
# query, answer generation) runs largely opaque to the client until the answer
# streams, so each stage calls :func:`emit_progress` at its boundary. ``ask`` binds a
# thread-safe callback here that enqueues a ``progress`` event onto the response
# stream, letting the UI status reflect the stage actually in flight. Bound per
# request (like :data:`usage_sink`) so concurrent requests stay isolated.
progress_sink: ContextVar[Any] = ContextVar("kg_progress_sink", default=None)


def emit_progress(phase: str) -> None:
    """Report a pipeline ``phase`` change to the active request's progress callback.

    No-op when no callback is bound (e.g. off-line evaluation or tests). The bound
    callback must be safe to call from worker threads — cypher generation runs via
    ``asyncio.to_thread`` — which :func:`KnowledgeGraphAgent.ask` guarantees by routing
    through ``loop.call_soon_threadsafe``.
    """
    callback = progress_sink.get()
    if callback is not None:
        callback(phase)

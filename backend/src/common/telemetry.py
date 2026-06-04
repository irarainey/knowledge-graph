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


# Per-request sink for token usage from LLM calls that don't otherwise surface it
# (e.g. neo4j-graphrag's internal cypher-generation ``llm.invoke``). A recorder
# appends normalized usage here when a sink is active. ``asyncio.to_thread`` copies
# the calling context into the worker thread, and because the value is a shared
# mutable list, appends made inside the thread are visible to the request task
# afterwards. Each request binds its own list, so concurrent requests stay isolated.
usage_sink: ContextVar[list[dict[str, Any]] | None] = ContextVar("llm_usage_sink", default=None)

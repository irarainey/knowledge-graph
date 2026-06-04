"""HTTP client for the backend's streaming `/ask/stream` endpoint."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import requests

from config import CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS


def stream_answer(base_url: str, question: str, holder: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield streaming events from the backend `/ask/stream` NDJSON endpoint.

    Yields small event dicts so the caller can both render answer text and reflect
    the current phase: ``{"type": "metadata"}`` once the graph data is back (the
    cypher-generation LLM call finished) and ``{"type": "token", "text": ...}`` as
    the answer-generation LLM streams. Cypher/records, stats and any error are
    stashed into ``holder``. Each NDJSON line is one complete JSON event.
    """
    timeout = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
    try:
        with requests.post(f"{base_url}/ask/stream", json={"question": question}, stream=True, timeout=timeout) as response:
            if response.status_code == 503:
                holder["error"] = "The /ask endpoint is disabled because the backend has no Azure OpenAI credentials configured."
                return
            if not response.ok:
                holder["error"] = f"Backend returned HTTP {response.status_code}: {response.reason}"
                return
            for raw_line in response.iter_lines():
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                if event_type == "metadata":
                    holder["cypher"] = event.get("cypher_used", [])
                    holder["records"] = event.get("records", [])
                    yield {"type": "metadata"}
                elif event_type == "token":
                    text = event.get("text", "")
                    if text:
                        yield {"type": "token", "text": text}
                elif event_type == "stats":
                    holder["stats"] = {k: v for k, v in event.items() if k != "type"}
                elif event_type == "error":
                    holder["error"] = event.get("message", "Answer generation failed.")
                    return
                elif event_type == "done":
                    return
    except requests.Timeout:
        holder["error"] = "The request timed out. The backend may still be processing."
    except requests.RequestException as exc:
        holder["error"] = f"Could not reach the backend at {base_url}: {exc}"

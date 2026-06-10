"""HTTP client for the backend's streaming `/ask` endpoint."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import requests

from config import CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS

# Used when the backend can't be reached for the identity list, so the UI still renders
# with a single least-privilege identity rather than an empty selector.
_FALLBACK_USERS: list[dict[str, Any]] = [{"id": "public", "displayName": "Public (least privilege)"}]


def fetch_users(base_url: str) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(identities, ok)`` from the backend's ``/users`` endpoint.

    The list is policy-driven (defined in the backend's access policy), so the UI never
    hard-codes it. ``ok`` is ``True`` only when the backend was reached and returned a
    usable list; on failure it returns the single least-privilege fallback with
    ``ok=False`` so the caller can render the page now but retry on the next run rather
    than caching the degraded list for the whole session.
    """
    try:
        response = requests.get(f"{base_url}/users", timeout=(CONNECT_TIMEOUT_SECONDS, CONNECT_TIMEOUT_SECONDS))
        response.raise_for_status()
        users = response.json().get("users") or []
    except (requests.RequestException, ValueError):
        return list(_FALLBACK_USERS), False
    if not users:
        return list(_FALLBACK_USERS), False
    return users, True


def stream_answer(
    base_url: str, question: str, holder: dict[str, Any], user: str | None = None, as_of: str | None = None
) -> Iterator[dict[str, Any]]:
    """Yield streaming events from the backend `/ask` NDJSON endpoint.

    Yields small event dicts so the caller can both render answer text and reflect
    the current phase: ``{"type": "progress", "phase": ...}`` as the backend advances
    through its pipeline stages, ``{"type": "metadata"}`` once the graph data is back,
    and ``{"type": "token", "text": ...}`` as the answer streams. Cypher/records, stats
    and any error are stashed into ``holder``. Each NDJSON line is one complete JSON event.

    ``user`` is the id of the selected identity; the backend resolves it to a principal
    and answers within that identity's authorization. ``as_of`` is an optional ISO date
    (YYYY-MM-DD): when set, the backend answers from the version of each versioned entity
    that was valid on that date; when omitted, it answers from the current version.
    """
    timeout = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
    payload = {"question": question, "user": user, "as_of": as_of}
    try:
        with requests.post(f"{base_url}/ask", json=payload, stream=True, timeout=timeout) as response:
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
                if event_type == "progress":
                    yield {"type": "progress", "phase": event.get("phase", "")}
                elif event_type == "metadata":
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

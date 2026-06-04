"""Environment-derived configuration for the chat UI.

Centralises the `.env` loading and the URLs/timeouts/examples the app needs, so the
rest of the modules can import ready-to-use values without touching ``os.environ``.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

NEO4J_BROWSER_PORT = 7474


def _neo4j_browser_url() -> str:
    """Build the Neo4j Browser URL from the Neo4j connection details.

    Honours an explicit ``NEO4J_BROWSER_URL`` override; otherwise derives the host
    from ``NEO4J_URI`` (e.g. ``bolt://localhost:7687``) and points at the Browser's
    HTTP port (``7474``).
    """
    override = os.environ.get("NEO4J_BROWSER_URL")
    if override:
        return override.rstrip("/") + "/"
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    host = urlparse(uri).hostname or "localhost"
    return f"http://{host}:{NEO4J_BROWSER_PORT}/browser/"


BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8080").rstrip("/")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173").rstrip("/")
NEO4J_BROWSER_URL = _neo4j_browser_url()

# The /ask/stream call drives an LLM (text-to-Cypher + answer generation). Use a short
# connect timeout so an unreachable backend fails fast, and a long read timeout so
# slow reasoning models have time to stream tokens.
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 180

EXAMPLE_QUESTIONS = [
    "How many flying hours has the engine had since 2026-05-25?",
    "How much ground distance has the front tyre covered between 2026-05-01 and 2026-05-31?",
    "Which aerodromes has the aircraft flown to?",
    "What components make up the fuel system?",
]

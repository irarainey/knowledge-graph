"""Streamlit chat UI for the Knowledge Graph natural-language `/ask` endpoint.

This is a thin client: it sends a question to the FastAPI backend's ``POST /ask``
endpoint and renders the answer along with the Cypher the agent ran and the graph
rows it retrieved. It holds no business logic — all retrieval and reasoning happen
in the backend (see ``backend/src/agent.py``).

Run with:

    cd streamlit-ui
    uv sync
    uv run streamlit run app.py

The backend must be running (``cd backend && uv run poe serve``) and configured with
Azure OpenAI credentials for ``/ask`` to work.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

import requests
import streamlit as st
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


DEFAULT_BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8080").rstrip("/")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173").rstrip("/")
NEO4J_BROWSER_URL = _neo4j_browser_url()
# The /ask call drives an LLM (text-to-Cypher + answer generation). Use a short
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


def stream_answer(base_url: str, question: str, holder: dict[str, Any]) -> Any:
    """Yield answer-token strings from the backend `/ask/stream` NDJSON endpoint.

    Metadata (Cypher, records) and any error are stashed into ``holder`` rather than
    yielded, so the generator only emits display text suitable for
    ``st.write_stream``. Each NDJSON line is one complete JSON event.
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
                elif event_type == "token":
                    text = event.get("text", "")
                    if text:
                        yield text
                elif event_type == "error":
                    holder["error"] = event.get("message", "Answer generation failed.")
                    return
                elif event_type == "done":
                    return
    except requests.Timeout:
        holder["error"] = "The request timed out. The backend may still be processing."
    except requests.RequestException as exc:
        holder["error"] = f"Could not reach the backend at {base_url}: {exc}"


def render_supporting_detail(cyphers: list[str], records: list[dict[str, Any]]) -> None:
    """Render the Cypher-used and retrieved-rows expanders for an assistant turn."""
    if cyphers:
        with st.expander(f"Cypher used ({len(cyphers)})"):
            for cypher in cyphers:
                st.code(cypher, language="cypher")
    if records:
        with st.expander(f"Retrieved rows ({len(records)})"):
            st.dataframe(records, use_container_width=True)


def render_message(message: dict[str, Any]) -> None:
    """Render a single chat message (user or assistant) and its supporting detail."""
    with st.chat_message(message["role"]):
        content = message.get("content")
        if content:
            st.markdown(content)
        if message.get("error"):
            st.error(message["error"])

        if message["role"] != "assistant":
            return

        render_supporting_detail(message.get("cypher") or [], message.get("records") or [])


def handle_question(base_url: str, question: str) -> None:
    """Render the question and stream the answer live, then store both in history.

    Rendering happens inline *after* the history loop has already run, and we do not
    call ``st.rerun()`` — so the live turn renders exactly once now, and once from
    history on subsequent runs.
    """
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    holder: dict[str, Any] = {"cypher": [], "records": [], "error": None}
    with st.chat_message("assistant"):
        status = st.status("Thinking — querying the knowledge graph…", expanded=False)
        message_placeholder = st.empty()
        parts: list[str] = []
        for token in stream_answer(base_url, question, holder):
            if not parts:
                status.update(label="Generating answer…")
            parts.append(token)
            message_placeholder.markdown("".join(parts) + " ▌")
        answer = "".join(parts)
        message_placeholder.markdown(answer)

        if holder["error"]:
            status.update(label="Generation failed", state="error")
            st.error(holder["error"])
        else:
            status.update(label="Done", state="complete")
        render_supporting_detail(holder["cypher"], holder["records"])

    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": answer,
        "cypher": holder["cypher"],
        "records": holder["records"],
    }
    if holder["error"]:
        assistant_message["error"] = holder["error"]
    st.session_state.messages.append(assistant_message)


def main() -> None:
    st.set_page_config(page_title="Cessna 172S Skyhawk — Ask", page_icon="✈️", layout="centered")

    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

        html, body, [class*="css"], button, input, textarea {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                Helvetica, Arial, sans-serif;
        }

        /* Match the renderer's comfortable reading column and spacing. */
        .block-container { max-width: 900px; padding-top: 3rem; }
        h1 { font-size: 2rem; font-weight: 700; letter-spacing: -0.01em; }

        /* Consistent rounded controls, matching the Vue app's 8px radius. */
        .stButton button, .stLinkButton a, [data-testid="stChatInput"] textarea {
            border-radius: 8px;
        }
        section[data-testid="stSidebar"] .stButton button,
        section[data-testid="stSidebar"] .stLinkButton a {
            text-align: left;
            justify-content: flex-start;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    if "messages" not in st.session_state:
        st.session_state.messages = []

    base_url = DEFAULT_BACKEND_URL

    with st.sidebar:
        if st.button("New conversation", use_container_width=True):
            st.session_state.messages = []
            st.rerun()

        st.link_button(
            "Open graph renderer ↗",
            FRONTEND_URL,
            use_container_width=True,
            help="Open the interactive knowledge graph (Vue app) in a new tab.",
        )

        st.link_button(
            "Open Neo4j browser ↗",
            NEO4J_BROWSER_URL,
            use_container_width=True,
            help="Open the Neo4j database browser in a new tab.",
        )

        st.divider()
        st.caption("Example questions")
        for example in EXAMPLE_QUESTIONS:
            if st.button(example, use_container_width=True):
                st.session_state.pending_question = example
                st.rerun()

    st.title("✈️ Cessna 172S Skyhawk (G-ECHO)")
    st.caption("Ask natural-language questions about the aircraft, its systems, components, flights and maintenance.")

    for message in st.session_state.messages:
        render_message(message)

    pending = st.session_state.pop("pending_question", None)
    typed = st.chat_input("Ask a question about the aircraft…")
    question = pending or typed
    if question:
        handle_question(base_url, question)


if __name__ == "__main__":
    main()

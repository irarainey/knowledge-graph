"""Streamlit chat UI for the Knowledge Graph natural-language `/ask` endpoint.

This is a thin client: it sends a question to the FastAPI backend's ``POST /ask``
endpoint and renders the answer along with the Cypher the agent ran and the graph
rows it retrieved. It holds no business logic — all retrieval and reasoning happen
in the backend (see ``backend/src/agents/knowledge_graph_agent.py``).

The supporting pieces live in sibling modules: configuration in ``config``, the
streaming HTTP client in ``backend_client``, the debug-panel rendering in
``debug_panel`` and the page CSS in ``styles``. This file wires the chat page
together.

Run with:

    cd frontend/chat-ui
    uv sync
    uv run streamlit run app.py

The backend must be running (``cd backend && uv run poe serve``) and configured with
Azure OpenAI credentials for ``/ask`` to work.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from backend_client import stream_answer
from config import BACKEND_URL, EXAMPLE_QUESTIONS, FRONTEND_URL, NEO4J_BROWSER_URL
from debug_panel import render_debug
from styles import PAGE_CSS


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

        render_debug(message.get("stats"), message.get("cypher") or [], message.get("records") or [])


def handle_question(base_url: str, question: str) -> None:
    """Render the question and stream the answer live, then store both in history.

    Rendering happens inline *after* the history loop has already run, and we do not
    call ``st.rerun()`` — so the live turn renders exactly once now, and once from
    history on subsequent runs.
    """
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    holder: dict[str, Any] = {"cypher": [], "records": [], "error": None, "stats": None}
    with st.chat_message("assistant"):
        # The "thinking" indicator lives in its own placeholder above the answer so it
        # can be cleared on completion — the finished answer then sits at the top with
        # the debug panel below it (no lingering "Done" box).
        status_placeholder = st.empty()
        message_placeholder = st.empty()
        status = status_placeholder.status("Asking the LLM for the graph data…", expanded=False)
        parts: list[str] = []
        for event in stream_answer(base_url, question, holder):
            if event["type"] == "metadata":
                # Cypher-generation (LLM call #1) done and the graph queried; the
                # answer-generation LLM call (#2) is next.
                status.update(label="Asking the LLM to generate the answer…")
            elif event["type"] == "token":
                parts.append(event["text"])
                message_placeholder.markdown("".join(parts) + " ▌")
        answer = "".join(parts)
        message_placeholder.markdown(answer)

        status_placeholder.empty()
        if holder["error"]:
            st.error(holder["error"])
        render_debug(holder["stats"], holder["cypher"], holder["records"])

    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": answer,
        "cypher": holder["cypher"],
        "records": holder["records"],
        "stats": holder["stats"],
    }
    if holder["error"]:
        assistant_message["error"] = holder["error"]
    st.session_state.messages.append(assistant_message)


def main() -> None:
    st.set_page_config(page_title="Cessna 172S Skyhawk — Ask", page_icon="✈️", layout="centered")
    st.markdown(PAGE_CSS, unsafe_allow_html=True)

    if "messages" not in st.session_state:
        st.session_state.messages = []

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
        handle_question(BACKEND_URL, question)


if __name__ == "__main__":
    main()

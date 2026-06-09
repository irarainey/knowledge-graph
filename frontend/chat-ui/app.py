"""Streamlit chat UI for the Knowledge Graph natural-language `/ask` endpoint.

This is a thin client: it sends a question to the FastAPI backend's ``POST /ask``
endpoint and renders the streamed answer along with the Cypher the agent ran and the
graph rows it retrieved. It holds no business logic — all retrieval and reasoning happen
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

from backend_client import fetch_users, stream_answer
from config import BACKEND_URL, EXAMPLE_QUESTIONS, FRONTEND_URL, NEO4J_BROWSER_URL
from debug_panel import render_debug
from styles import PAGE_CSS

# Human-readable status labels for each backend pipeline ``progress`` phase, shown in
# the "thinking" status box so it reflects the stage actually in flight. Mirrors the
# steps in the debug panel: tool-planning → build query → run query → answer.
_PROGRESS_LABELS = {
    "planning": "Deciding what to fetch…",
    "cypher": "Building the query…",
    "querying": "Querying the graph database…",
    "answering": "Generating the answer…",
}


def render_message(message: dict[str, Any]) -> None:
    """Render a single chat message (user or assistant) and its supporting detail."""
    with st.chat_message(message["role"]):
        # Always emit a content slot (even when empty) so the live turn and this
        # history re-render produce the same element structure — otherwise a slot
        # mismatch strands the previous answer's debug panel as a ghost on rerun.
        st.markdown(message.get("content") or "")
        if message.get("error"):
            st.error(message["error"])

        if message["role"] != "assistant":
            return

        render_debug(message.get("stats"), message.get("cypher") or [], message.get("records") or [])


def handle_question(base_url: str, question: str, user: str | None) -> None:
    """Render the question and stream the answer live, then store both in history.

    Rendering happens inline *after* the history loop has already run, and we do not
    call ``st.rerun()`` — so the live turn renders exactly once now, and once from
    history on subsequent runs. ``user`` is the selected identity the backend answers as.
    """
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    holder: dict[str, Any] = {"cypher": [], "records": [], "error": None, "stats": None}
    with st.chat_message("assistant"):
        # A SINGLE placeholder carries the turn from the "thinking" status through the
        # streamed answer, so the live turn has exactly the same element structure as
        # the history re-render: one content slot followed by the debug panel. Using a
        # separate status placeholder added an extra slot, which left the previous
        # answer's debug expander stranded as a ghost panel during the next question.
        message_placeholder = st.empty()
        status = message_placeholder.status(_PROGRESS_LABELS["planning"], expanded=False)
        parts: list[str] = []
        for event in stream_answer(base_url, question, holder, user=user):
            if event["type"] == "progress":
                # The backend advances through planning → cypher → querying → answering;
                # reflect the live stage so the wait isn't one opaque label.
                label = _PROGRESS_LABELS.get(event["phase"])
                if label:
                    status.update(label=label)
            elif event["type"] == "metadata":
                # Graph rows are back; the debug panel will render them once complete.
                pass
            elif event["type"] == "token":
                parts.append(event["text"])
                # The first token replaces the status box in the same slot.
                message_placeholder.markdown("".join(parts) + " ▌")
        answer = "".join(parts)
        message_placeholder.markdown(answer)

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


def _on_user_change() -> None:
    """Reset the conversation when the acting identity changes.

    The chat history is part of the model's context, so carrying it across an identity
    switch could leak data the new identity isn't authorized to see (the previous answers
    were generated for a different principal). Clearing it makes each identity start fresh.
    """
    st.session_state.messages = []
    st.session_state.pop("pending_question", None)


def _render_identity_selector(base_url: str) -> str:
    """Render the 'Ask as' identity selector and return the selected user id."""
    users = st.session_state.get("users")
    if users is None:
        # Only cache a successful fetch; a transient backend outage must not lock the
        # session to the least-privilege fallback. On failure we render the fallback now
        # but leave the cache empty so the next rerun retries.
        users, ok = fetch_users(base_url)
        if ok:
            st.session_state.users = users

    ids = [user["id"] for user in users]
    labels = {user["id"]: user.get("displayName", user["id"]) for user in users}
    st.caption("Ask as")
    selected = st.selectbox(
        "Ask as",
        options=ids,
        format_func=lambda user_id: labels.get(user_id, user_id),
        key="user_id",
        on_change=_on_user_change,
        label_visibility="collapsed",
        help="The identity the backend answers as. Switching identities starts a new conversation.",
    )
    return selected


def main() -> None:
    st.set_page_config(page_title="Cessna 172S Skyhawk — Ask", page_icon="✈️", layout="centered")
    st.markdown(PAGE_CSS, unsafe_allow_html=True)

    if "messages" not in st.session_state:
        st.session_state.messages = []

    with st.sidebar:
        user_id = _render_identity_selector(BACKEND_URL)

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
        handle_question(BACKEND_URL, question, user_id)


if __name__ == "__main__":
    main()

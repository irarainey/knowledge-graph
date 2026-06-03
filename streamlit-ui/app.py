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

import html
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


def _fmt_int(value: Any) -> str:
    """Render an integer token/count, using an em dash when it is unknown."""
    return f"{value:,}" if isinstance(value, int) else "—"


def _fmt_ms(value: Any) -> str:
    """Render a millisecond duration, using an em dash when it is unknown."""
    return f"{value:,.0f}" if isinstance(value, (int, float)) else "—"


_STAGE_LABELS = {
    "cypher_generation": "Cypher generation (LLM)",
    "answer_generation": "Answer generation (LLM)",
}


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Build a compact Markdown table — far smaller than ``st.dataframe`` widgets."""
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _payload_details(summary: str, messages: list[dict[str, Any]]) -> str:
    """Build a collapsed native HTML ``<details>`` panel for a (large) LLM request.

    Streamlit forbids nesting ``st.expander`` inside another expander, so the request
    payloads — which can be very large — are rendered via ``st.html`` as native
    ``<details>`` elements that start collapsed and live inside the outer "Debug
    details" expander. Inline styles keep the monospace payload self-contained.
    """
    pre_style = (
        "font-family:'Source Code Pro',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
        "font-size:0.72rem;line-height:1.35;white-space:pre-wrap;word-break:break-word;"
        "max-height:16rem;overflow:auto;margin:0;padding:0.4rem 0.55rem;"
        "background:rgba(128,128,128,0.08);border-radius:4px;"
    )
    role_style = "display:inline-block;font-size:0.7rem;font-weight:600;text-transform:uppercase;letter-spacing:0.03em;color:#6b7280;margin-bottom:0.2rem;"
    blocks = []
    for msg in messages:
        role = html.escape(str(msg.get("role", "")))
        content = html.escape(str(msg.get("content", "")))
        blocks.append(
            f'<div style="margin-top:0.4rem;"><span style="{role_style}">{role}</span><pre style="{pre_style}">{content}</pre></div>'
        )
    return f'<details class="llm-payload"><summary>{html.escape(summary)}</summary>{"".join(blocks)}</details>'


def _call_meta(call: dict[str, Any]) -> str:
    """One-line timing/token caption for a single LLM call."""
    return f"⏱ {_fmt_ms(call.get('duration_ms'))} ms · {_fmt_int(call.get('total'))} tokens"


def render_debug(
    stats: dict[str, Any] | None,
    cyphers: list[str],
    records: list[dict[str, Any]],
) -> None:
    """Render the single per-request "Debug details" expander for an assistant turn.

    The contents flow in chronological workflow order so they read top-to-bottom like
    the pipeline the agent ran:
    (1) call the LLM with the graph schema, (2) the Cypher the LLM produced,
    (3) run that Cypher against Neo4j, (4) call the LLM again with the retrieved rows
    to write the answer — each LLM request payload sits in a collapsed panel — followed
    by an overall summary of tokens and timings at the bottom.
    """
    stats = stats or {}
    calls = stats.get("calls") or []
    tokens = stats.get("tokens") or {}
    durations = stats.get("durations_ms") or {}
    cypher_calls = [c for c in calls if c.get("stage") == "cypher_generation"]
    answer_calls = [c for c in calls if c.get("stage") == "answer_generation"]

    if not (stats or cyphers or records):
        return

    with st.expander("Debug details"):
        # Step 1 — Call the LLM with the graph schema to generate a query.
        st.markdown("**1 · Call the LLM with the graph schema** &nbsp;`LLM`")
        if cypher_calls:
            for call in cypher_calls:
                st.caption(_call_meta(call))
                request = call.get("request") or []
                if request:
                    st.html(_payload_details("Prompt sent to the LLM (includes the graph schema)", request))
        else:
            st.caption("No telemetry was reported for this step.")

        # Step 2 — The Cypher the LLM produced.
        st.markdown("**2 · Generate the Cypher query**")
        if cyphers:
            for cypher in cyphers:
                st.code(cypher, language="cypher")
        else:
            st.caption("No Cypher was generated.")

        # Step 3 — Run the Cypher against Neo4j.
        st.markdown("**3 · Query the graph database** &nbsp;`Neo4j`")
        st.caption(f"⏱ {_fmt_ms(durations.get('graph_query'))} ms · {_fmt_int(stats.get('record_count') or len(records))} rows")
        if records:
            st.dataframe(records, use_container_width=True, hide_index=True)
        else:
            st.caption("No rows were retrieved.")

        # Step 4 — Call the LLM again with the retrieved rows to write the answer.
        st.markdown("**4 · Call the LLM with the retrieved data** &nbsp;`LLM`")
        if answer_calls:
            for call in answer_calls:
                st.caption(_call_meta(call))
                request = call.get("request") or []
                if request:
                    st.html(_payload_details("Prompt sent to the LLM (includes the retrieved rows)", request))
        else:
            st.caption("No telemetry was reported for this step.")
        st.caption("The generated answer is shown above.")

        # Summary — overall tokens and timings
        if calls or tokens or durations:
            st.markdown("**Summary**")
            st.markdown(
                f"**Model:** `{stats.get('model') or '—'}` &nbsp;·&nbsp; "
                f"**LLM calls:** {_fmt_int(stats.get('llm_calls'))} &nbsp;·&nbsp; "
                f"**Cypher queries:** {_fmt_int(stats.get('cypher_count'))} &nbsp;·&nbsp; "
                f"**Records:** {_fmt_int(stats.get('record_count'))}"
            )

            token_rows = [
                [_STAGE_LABELS.get(call["stage"], call["stage"]), _fmt_int(call.get("prompt")), _fmt_int(call.get("completion")), _fmt_int(call.get("total"))]
                for call in calls
            ]
            token_rows.append(["**Total**", _fmt_int(tokens.get("prompt")), _fmt_int(tokens.get("completion")), _fmt_int(tokens.get("total"))])
            st.markdown("**Tokens**")
            st.markdown(_md_table(["Call", "Prompt", "Completion", "Total"], token_rows))

            timing_rows = [["Cypher generation (LLM)", _fmt_ms(call.get("duration_ms"))] for call in cypher_calls]
            timing_rows.append(["Graph query", _fmt_ms(durations.get("graph_query"))])
            timing_rows += [["Answer generation (LLM)", _fmt_ms(call.get("duration_ms"))] for call in answer_calls]
            timing_rows.append(["**Total**", _fmt_ms(durations.get("total"))])
            st.markdown("**Timings (ms)**")
            st.markdown(_md_table(["Step", "ms"], timing_rows))


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

    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

        html, body, [class*="css"], button, input, textarea {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                Helvetica, Arial, sans-serif;
        }

        /* Match the renderer's comfortable reading column and spacing. Apply the
           same width + horizontal padding to BOTH the main content column and the
           fixed chat-input bar so the heading lines up over the question box. */
        [data-testid="stMainBlockContainer"],
        [data-testid="stBottomBlockContainer"] {
            max-width: 900px;
            margin-left: auto;
            margin-right: auto;
            padding-left: 1rem;
            padding-right: 1rem;
        }
        [data-testid="stMainBlockContainer"] { padding-top: 3rem; }
        /* Reserve the scrollbar gutter so the centred columns don't shift when the
           main area scrolls but the fixed bottom bar does not. */
        [data-testid="stMain"] { scrollbar-gutter: stable; }
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

        /* Keep the "Debug details" expander compact and quiet. */
        [data-testid="stExpander"] summary { font-size: 0.85rem; }
        [data-testid="stExpander"] [data-testid="stExpanderDetails"] { font-size: 0.8rem; }
        [data-testid="stExpander"] [data-testid="stExpanderDetails"] p { margin-bottom: 0.35rem; }
        [data-testid="stExpander"] [data-testid="stExpanderDetails"] table { font-size: 0.8rem; }
        [data-testid="stExpander"] [data-testid="stExpanderDetails"] code { font-size: 0.75rem; }

        /* Collapsible LLM request payloads (native <details>) inside the debug box.
           The message/role/pre styling is applied inline in _payload_details so it
           survives st.html rendering; only the panel chrome lives here. */
        details.llm-payload {
            border: 1px solid rgba(128, 128, 128, 0.3);
            border-radius: 6px;
            padding: 0.25rem 0.6rem;
            margin: 0.25rem 0 0.6rem 0;
        }
        details.llm-payload > summary {
            cursor: pointer;
            font-size: 0.78rem;
            font-weight: 600;
            color: #6b7280;
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

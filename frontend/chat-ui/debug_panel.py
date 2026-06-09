"""The per-request "Debug details" expander rendering and its formatting helpers.

This panel lays out the agent's workflow in chronological order, driven entirely by the
``stats`` event the backend emits on the stream. A single Microsoft Agent Framework agent
is forced to call its text-to-Cypher retrieval tool, then writes the answer from the rows
the tool returns.
"""

from __future__ import annotations

import html
from typing import Any

import streamlit as st


def _fmt_int(value: Any) -> str:
    """Render an integer token/count, using an em dash when it is unknown."""
    return f"{value:,}" if isinstance(value, int) else "—"


def _fmt_ms(value: Any) -> str:
    """Render a millisecond duration, using an em dash when it is unknown."""
    return f"{value:,.0f}" if isinstance(value, (int, float)) else "—"


_STAGE_LABELS = {
    "agent_planning": "Agent planning (LLM)",
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
    the agent's run, surfacing every one of the three LLM calls a question makes:
    (1) the agent's tool-planning turn (it decides to call the retrieval tool),
    (2) the cypher-generation call inside that tool (its prompt carries the graph schema)
    and the Cypher it produced, (3) running that Cypher against Neo4j, and (4) the
    answer-generation turn (its request includes the retrieved rows as the tool result).
    Each LLM request payload sits in a collapsed panel, followed by an overall summary of
    tokens and timings at the bottom.
    """
    stats = stats or {}
    calls = stats.get("calls") or []
    tokens = stats.get("tokens") or {}
    durations = stats.get("durations_ms") or {}
    planning_calls = [c for c in calls if c.get("stage") == "agent_planning"]
    cypher_calls = [c for c in calls if c.get("stage") == "cypher_generation"]
    answer_calls = [c for c in calls if c.get("stage") == "answer_generation"]

    if not (stats or cyphers or records):
        return

    with st.expander("Debug details"):
        # Step 1 — The agent's tool-planning turn: it decides to call the retrieval tool.
        st.markdown("**1 · Agent selects the retrieval tool** &nbsp;`LLM`")
        if planning_calls:
            for call in planning_calls:
                st.caption(_call_meta(call))
                request = call.get("request") or []
                if request:
                    st.html(_payload_details("Prompt sent to the LLM (the agent decides which tool to call)", request))
        else:
            st.caption("No telemetry was reported for this step.")

        # Step 2 — Cypher generation inside the tool (prompt carries the graph schema).
        st.markdown("**2 · Generate the Cypher query** &nbsp;`LLM`")
        if cypher_calls:
            for call in cypher_calls:
                st.caption(_call_meta(call))
                request = call.get("request") or []
                if request:
                    st.html(_payload_details("Prompt sent to the LLM (includes the graph schema)", request))
        else:
            st.caption("No telemetry was reported for this step.")
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

        # Step 4 — The agent writes the answer; its request includes the tool-result rows.
        st.markdown("**4 · Generate the answer from the retrieved data** &nbsp;`LLM`")
        if answer_calls:
            for call in answer_calls:
                st.caption(_call_meta(call))
                request = call.get("request") or []
                if request:
                    st.html(
                        _payload_details(
                            "Prompt sent to the LLM (system instructions, your question, and the "
                            "retrieved rows delivered as the tool result)",
                            request,
                        )
                    )
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

            # Timings in workflow order: planning → cypher generation → graph query →
            # answer generation → total.
            timing_rows = [["Agent planning (LLM)", _fmt_ms(call.get("duration_ms"))] for call in planning_calls]
            timing_rows += [["Cypher generation (LLM)", _fmt_ms(call.get("duration_ms"))] for call in cypher_calls]
            timing_rows.append(["Graph query", _fmt_ms(durations.get("graph_query"))])
            timing_rows += [["Answer generation (LLM)", _fmt_ms(call.get("duration_ms"))] for call in answer_calls]
            timing_rows.append(["**Total**", _fmt_ms(durations.get("total"))])
            st.markdown("**Timings (ms)**")
            st.markdown(_md_table(["Step", "ms"], timing_rows))

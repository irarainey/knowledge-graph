"""Page-level CSS for the chat UI, injected once via ``st.markdown``."""

from __future__ import annotations

# Loaded into a single <style> tag in main(). Kept out of app.py so the page
# wiring stays readable.
PAGE_CSS = """
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
/* Pin the main scroll container so messages scroll *inside* it and the sticky
   chat-input bar stays put. When a chat_input is present Streamlit swaps the
   main element's test id to stAppScrollToBottomContainer (an auto-scroll-to-
   bottom wrapper), so both ids must be targeted. The 100vh fallback before
   100dvh guards browsers/preview panes that mishandle dynamic viewport units —
   without a constrained height the whole document scrolls instead, which makes
   the page un-scrollable and the sticky input drift up and down.
   scrollbar-gutter keeps the centred column from shifting when the bar appears. */
[data-testid="stMain"],
[data-testid="stAppScrollToBottomContainer"] {
    height: 100vh;
    height: 100dvh;
    max-height: 100vh;
    max-height: 100dvh;
    overflow-y: auto;
    overflow-x: hidden;
    scrollbar-gutter: stable;
}
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
"""

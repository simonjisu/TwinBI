from __future__ import annotations

import requests
import streamlit as st

from ui.components import (
    render_chat_panel,
    render_figures,
    render_filters_panel,
    render_schema_panel,
)
from ui.state import session_state


def app_body() -> None:
    session_state.init_session_state()
    api_base = st.session_state.api_base
    _sync_state(api_base)

    with st.sidebar:
        render_filters_panel(api_base)

    st.title("Agent4OLAP — NLQ Dashboard")
    st.caption("Ask questions in natural language, explore charts, and click data points to filter.")
    _inject_theme()

    cols = st.columns([1.6, 1.0])
    with cols[0]:
        render_schema_panel()
        render_figures(api_base)
        _render_tables()
    with cols[1]:
        render_chat_panel(api_base)


def _render_tables() -> None:
    if not st.session_state.tables:
        return
    with st.expander("Data preview", expanded=False):
        for idx, tbl in enumerate(st.session_state.tables):
            st.dataframe(tbl.get("preview", []), width="stretch", key=f"tbl-{idx}")


def _sync_state(api_base: str) -> None:
    try:
        response = requests.get(
            f"{api_base}/state/context",
            params={"session_id": st.session_state.session_id},
            timeout=5,
        )
        response.raise_for_status()
        data = response.json()
        st.session_state.active_filters = data.get("active_filters", [])
        st.session_state.last_selection = data.get("last_selection")
        st.session_state.conversation = data.get("conversation", [])
    except Exception:
        # Best-effort sync only.
        return


def _inject_theme() -> None:
    st.markdown(
        """
        <style>
            .main {
                background: #f5f7fb;
                color: #111827;
            }
            h1, h2, h3, h4, h5, h6, p, span, div {
                color: #111827 !important;
                font-family: "DM Sans", "Inter", system-ui, -apple-system, sans-serif;
            }
            .stButton button {
                border-radius: 999px;
                background: linear-gradient(90deg, #2563eb, #22c55e);
                color: #f8fafc;
                border: none;
                font-weight: 700;
            }
            .stChatMessage {
                background: rgba(0, 0, 0, 0.04);
                border-radius: 12px;
                padding: 8px 12px;
            }
            /* Keep right column fixed and chat input reachable */
            [data-testid="column"]:nth-of-type(2) {
                position: sticky;
                top: 0;
                height: 100vh;
                align-self: flex-start;
            }
            [data-testid="column"]:nth-of-type(2) > div {
                display: flex;
                flex-direction: column;
                height: 100%;
            }
            [data-testid="column"]:nth-of-type(2) [data-testid="stVerticalBlock"] {
                display: flex;
                flex-direction: column;
                height: 100%;
                gap: 0.5rem;
            }
            /* Push chat input to bottom of right column and keep it visible */
            [data-testid="stChatInput"] {
                position: sticky;
                bottom: 0;
                background: #f5f7fb;
                padding-top: 8px;
                border-top: 1px solid #e5e7eb;
                z-index: 5;
            }
            [data-testid="stChatMessageContainer"] {
                flex: 1 1 auto;
                overflow-y: auto;
                padding-bottom: 0.5rem;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    app_body()

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional

import streamlit as st


def init_session_state() -> None:
    if "session_id" not in st.session_state:
        st.session_state.session_id = str(uuid.uuid4())
    st.session_state.setdefault("active_filters", [])
    st.session_state.setdefault("last_selection", None)
    st.session_state.setdefault("conversation", [])
    st.session_state.setdefault("figures", [])
    st.session_state.setdefault("tables", [])
    st.session_state.setdefault("api_base", os.getenv("BACKEND_URL", "http://localhost:8000"))


def set_context(active_filters: List[Dict[str, Any]], last_selection: Optional[Dict[str, Any]]) -> None:
    st.session_state.active_filters = active_filters
    st.session_state.last_selection = last_selection


def add_message(role: str, content: str) -> None:
    st.session_state.conversation.append({"role": role, "content": content})


def set_figures(figures: List[Dict[str, Any]]) -> None:
    st.session_state.figures = figures


def set_tables(tables: List[Dict[str, Any]]) -> None:
    st.session_state.tables = tables


def reset_session() -> None:
    session_id = st.session_state.get("session_id")
    st.session_state.clear()
    st.session_state.session_id = session_id or str(uuid.uuid4())
    init_session_state()

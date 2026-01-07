from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests
import streamlit as st

from ui.state import session_state


def render_chat_panel(api_base: str) -> Optional[Dict[str, Any]]:
    st.markdown("### Chat with your cube")
    for message in st.session_state.conversation:
        with st.chat_message(message["role"]):
            st.write(message["content"])

    prompt = st.chat_input("Ask a question about sales, products, or stores")
    if not prompt:
        return None

    session_state.add_message("user", prompt)
    payload = {
        "session_id": st.session_state.session_id,
        "message": prompt,
        "active_filters": st.session_state.active_filters,
        "last_selection": st.session_state.last_selection,
    }

    try:
        response = requests.post(f"{api_base}/query/render", json=payload, timeout=30)
        response.raise_for_status()
        data: Dict[str, Any] = response.json()
    except Exception as exc:  # noqa: BLE001
        with st.chat_message("assistant"):
            st.error(f"Request failed: {exc}")
        return None

    session_state.add_message("assistant", data.get("assistant_message", ""))
    session_state.set_context(data.get("active_filters", []), data.get("last_selection"))
    session_state.set_figures(data.get("figures", []))
    session_state.set_tables(data.get("tables", []))

    with st.chat_message("assistant"):
        st.write(data.get("assistant_message", ""))
        if data.get("plan"):
            st.caption(" • ".join(data["plan"]))
    return data

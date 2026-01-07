from __future__ import annotations

import requests
import streamlit as st

from ui.state import session_state


def render_filters_panel(api_base: str) -> None:
    st.markdown("### Active Filters")
    if not st.session_state.active_filters:
        st.caption("No filters applied.")
    else:
        for flt in st.session_state.active_filters:
            st.write(f"- {flt['member']} {flt.get('operator', 'equals')} {flt.get('values')}")

    cols = st.columns(2)
    with cols[0]:
        if st.button("Clear filters", width="stretch"):
            st.session_state.active_filters = []
            _persist_state(api_base)
            st.rerun()
    with cols[1]:
        if st.button("Reset conversation", type="secondary", width="stretch"):
            _reset_state(api_base)
            st.rerun()


def _persist_state(api_base: str) -> None:
    payload = {
        "session_id": st.session_state.session_id,
        "active_filters": st.session_state.active_filters,
        "last_selection": st.session_state.last_selection,
        "conversation": st.session_state.conversation,
    }
    try:
        requests.post(f"{api_base}/state/context", json=payload, timeout=10)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not persist state: {exc}")


def _reset_state(api_base: str) -> None:
    payload = {"session_id": st.session_state.session_id, "active_filters": []}
    try:
        requests.post(f"{api_base}/state/reset", json=payload, timeout=10)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not reset state: {exc}")
    session_state.reset_session()

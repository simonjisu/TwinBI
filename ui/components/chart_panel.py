from __future__ import annotations

import json
from typing import Any, Dict, List

import requests
import streamlit as st

try:  # Optional dependency to capture Plotly click/select events.
    from streamlit_plotly_events import plotly_events
except Exception:  # noqa: BLE001
    plotly_events = None

import plotly.io as pio

from ui.events.chart_events import event_to_filter


def render_figures(api_base: str) -> None:
    if not st.session_state.figures:
        st.info("Run a query to see charts.")
        return

    for figure in st.session_state.figures:
        fig_obj = pio.from_json(json.dumps(figure["plotly_spec"]))
        st.markdown(f"#### {figure.get('meta', {}).get('chart_type', '').title()} view")

        events: List[Dict[str, Any]] = []
        if plotly_events:
            events = plotly_events(
                fig_obj,
                click_event=True,
                select_event=True,
                hover_event=False,
                key=figure["figure_id"],
            )
        else:
            st.plotly_chart(fig_obj, width="stretch", key=figure["figure_id"])

        if events:
            handle_interaction(api_base, figure, events[0])


def handle_interaction(api_base: str, figure: Dict[str, Any], event: Dict[str, Any]) -> None:
    cube_filter = event_to_filter(event, figure.get("interaction_mapping") or {})
    if not cube_filter:
        return

    st.session_state.active_filters.append(cube_filter)
    payload = {
        "session_id": st.session_state.session_id,
        "active_filters": st.session_state.active_filters,
        "last_selection": {
            "figure_id": figure.get("figure_id"),
            "payload": event,
            "derived_filters": [cube_filter],
        },
        "conversation": st.session_state.conversation,
    }
    try:
        requests.post(f"{api_base}/state/context", json=payload, timeout=10)
        st.toast(
            f"Added filter: {cube_filter['member']} = {cube_filter['values'][0]}"
        )
        st.rerun()
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not persist selection: {exc}")

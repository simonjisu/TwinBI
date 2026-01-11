from __future__ import annotations

import os
import streamlit.components.v1 as components

_RELEASE = os.getenv("COMPONENT_DEV", "0") != "1"

# In production (Docker), use built frontend files
_build_dir = os.path.join(os.path.dirname(__file__), "frontend", "dist")

if _RELEASE:
    _component_func = components.declare_component(
        "superset_embed",
        path=_build_dir,
    )
else:
    # For local dev only (vite dev server)
    _component_func = components.declare_component(
        "superset_embed",
        url="http://localhost:5173",
    )


def superset_embed(
    *,
    dashboard_id: str,
    superset_domain: str,
    guest_token: str,
    height: int = 900,
    key: str = "superset_embed",
):
    """
    Embed a Superset dashboard using the official Embedded SDK.
    """
    return _component_func(
        dashboardId=dashboard_id,
        supersetDomain=superset_domain,
        guestToken=guest_token,
        height=height,
        key=key,
        default=None,
    )
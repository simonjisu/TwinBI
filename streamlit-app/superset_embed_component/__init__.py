from __future__ import annotations

import os
from pathlib import Path
import streamlit.components.v1 as components

# 템플릿(v1) 관례: 배포(build 포함)면 True, 개발(dev server)면 False
_RELEASE = os.getenv("SUPERSET_EMBED_COMPONENT_RELEASE", "0") == "1"
if _RELEASE:
    build_dir = Path(__file__).parent / "frontend" / "build"
    _component_func = components.declare_component("superset_embed_component", path=str(build_dir))
else:
    _component_func = components.declare_component(
        "superset_embed_component",
        url=os.getenv("SUPERSET_EMBED_COMPONENT_URL", "http://localhost:3001"),
    )


def superset_embed(
    dashboard_id: str,
    superset_domain: str,
    guest_token: str,
    height: int = 900,
    ui_config: dict | None = None,
    key: str | None = None,
):
    """
    dashboard_id: Superset embed uuid (URL: /embedded/<uuid> 의 uuid)
    superset_domain: 브라우저가 접근 가능한 superset 주소 (보통 http://localhost:8088)
    guest_token: /api/v1/security/guest_token/ 로 발급받은 토큰 문자열
    """
    if ui_config is None:
        ui_config = {
            "hideTitle": False,
            "hideChartControls": False,
            "hideTab": False,
            "filters": {"expanded": True},
        }

    return _component_func(
        dashboardId=dashboard_id,
        supersetDomain=superset_domain,
        guestToken=guest_token,
        height=height,
        uiConfig=ui_config,
        key=key,
        default=None,
    )

# from __future__ import annotations

# import os
# import streamlit.components.v1 as components

# _RELEASE = os.getenv("COMPONENT_DEV", "0") != "1"

# # In production (Docker), use built frontend files
# _build_dir = os.path.join(os.path.dirname(__file__), "frontend", "dist")

# if _RELEASE:
#     _component_func = components.declare_component(
#         "superset_embed",
#         path=_build_dir,
#     )
# else:
#     # For local dev only (vite dev server)
#     _component_func = components.declare_component(
#         "superset_embed",
#         url="http://localhost:5173",
#     )


# def superset_embed(
#     *,
#     dashboard_id: str,
#     superset_domain: str,
#     guest_token: str,
#     height: int = 900,
#     key: str = "superset_embed",
# ):
#     """
#     Embed a Superset dashboard using the official Embedded SDK.
#     """
#     return _component_func(
#         dashboardId=dashboard_id,
#         supersetDomain=superset_domain,
#         guestToken=guest_token,
#         height=height,
#         key=key,
#         default=None,
#     )

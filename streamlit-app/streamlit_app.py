from __future__ import annotations

import os
import json
import requests
import streamlit as st
import streamlit.components.v1 as components
import uuid
import urllib.parse
import sys
from typing import Any
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent / "src"
if _SRC_ROOT.exists():
    sys.path.insert(0, str(_SRC_ROOT))

from superset_embed_component import superset_embed
from graph_vis import extract_fact_view, make_plotly_figure
from schema_processor import hierarchy_from_json


from streamlit_agraph import agraph, Node, Edge, Config

SUPERSET_PUBLIC_URL = os.getenv("SUPERSET_PUBLIC_URL", "http://localhost:58088")
SUPERSET_INTERNAL_URL = os.getenv("SUPERSET_INTERNAL_URL", "http://superset_app:8088")
FASTAPI_INTERNAL_URL = os.getenv("FASTAPI_INTERNAL_URL", "http://localhost:8000")
FASTAPI_PUBLIC_URL = os.getenv("FASTAPI_PUBLIC_URL", FASTAPI_INTERNAL_URL)
STREAMLIT_USER_ID = os.getenv("STREAMLIT_USER_ID", "streamlit_user")
DEFAULT_EMBED_AUTH_MODE = os.getenv("SUPERSET_EMBED_AUTH_MODE", "session_iframe")
if DEFAULT_EMBED_AUTH_MODE == "embedded_sdk":
    DEFAULT_EMBED_AUTH_MODE = "guest_token"

DEFAULT_SUPERSET_USERNAME = os.getenv("SUPERSET_USERNAME", "admin")
DEFAULT_SUPERSET_PASSWORD = os.getenv("SUPERSET_PASSWORD", "admin")
MODEL_OPTIONS = [
    "gpt-5-nano",
    "gpt-5-mini",
    "gpt-4.1-nano",
    "gpt-4.1-mini",
    "gpt-4o-mini",
]
DEFAULT_DASHBOARD_ID_BY_USER = {
    "admin": "12",
    "test1": "13",
    "test2": "14",
    "test3": "15",
    "test4": "16",
    "test5": "17",
}

GUEST_TOKEN_AUD = os.getenv("SUPERSET_GUEST_AUD", "superset")


def _api_session_with_bearer(
    superset_username: str,
    superset_password: str,
) -> tuple[requests.Session, str]:
    s = requests.Session()
    r = s.post(
        f"{SUPERSET_INTERNAL_URL}/api/v1/security/login",
        json={
            "username": superset_username,
            "password": superset_password,
            "provider": "db",
            "refresh": False,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Superset login failed {r.status_code}: {r.text}")

    access_token = r.json()["access_token"]
    s.headers.update({"Authorization": f"Bearer {access_token}"})
    return s, access_token


def _api_get_csrf(session: requests.Session) -> str:
    r = session.get(f"{SUPERSET_INTERNAL_URL}/api/v1/security/csrf_token/", timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"CSRF fetch failed {r.status_code}: {r.text}")
    return r.json()["result"]


@st.cache_data(ttl=240)
def get_guest_token(
    dashboard_uuid: str,
    superset_username: str,
    superset_password: str,
) -> str:
    sess, _access = _api_session_with_bearer(superset_username, superset_password)
    csrf = _api_get_csrf(sess)

    payload = {
        "resources": [{"type": "dashboard", "id": str(dashboard_uuid)}],
        "rls": [],
        "user": {"username": str(superset_username or "streamlit-guest")},
        "aud": os.getenv("SUPERSET_GUEST_AUD", "superset"),
    }

    headers = {
        "X-CSRFToken": csrf,
        "X-CSRF-Token": csrf,
        "Referer": f"{SUPERSET_INTERNAL_URL}/",
        "Content-Type": "application/json",
    }

    r = sess.post(
        f"{SUPERSET_INTERNAL_URL}/api/v1/security/guest_token/",
        json=payload,
        headers=headers,
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Guest token failed {r.status_code}: {r.text}")

    return r.json()["token"]

#-------------------Schema Graph----------------------------------------
@st.cache_data
def _load_graph_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))

NODE_FONT = {"color": "white", "size": 18, "strokeWidth": 2, "strokeColor": "rgba(0,0,0,0.55)"}
MEASURE_FONT = {"color": "white", "size": 16, "strokeWidth": 2, "strokeColor": "rgba(0,0,0,0.55)"}
ATTR_FONT = {"color": "white", "size": 15, "strokeWidth": 2, "strokeColor": "rgba(0,0,0,0.55)"}


@st.cache_data
def _load_html_template(name: str) -> str:
    return (Path(__file__).parent / "javascripts" / name).read_text(encoding="utf-8")

@st.cache_data
def _load_hierarchies(path: str) -> dict:
    hierarchies: dict[str, Any] = {}
    hierarchy_dir = Path(path)
    if not hierarchy_dir.exists():
        return hierarchies
    for item in sorted(hierarchy_dir.glob("*.json")):
        try:
            tree = hierarchy_from_json(item)
        except Exception:
            continue
        hierarchies[tree.table] = tree
    return hierarchies

def _unwrap_schema_graph(data: dict) -> dict:
    if "nodes" in data:
        return data
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, dict) and ("nodes" in value or "edges" in value):
                return value
    return data

def _schema_nodes_edges(graph_data: dict, hierarchies: dict | None) -> tuple[list[dict], list[dict], str]:
    graph = _unwrap_schema_graph(graph_data)
    fact_id = str(graph.get("fact") or "fact_sales")
    graph_dict = {fact_id: graph}
    nodes, edges = extract_fact_view(graph_dict, fact_id, hierarchies=hierarchies, node_weights=None)
    for node in nodes:
        data = node["data"]
        if "label" not in data:
            data["label"] = data.get("name") or data["id"]
    return nodes, edges, fact_id

def render_schema_graph_html(path: str, api_base: str, session_id: str):
    data = _load_graph_json(path)
    html = (
        _load_html_template("schema_graph.html")
        .replace("{{SCHEMA_CONTAINER_ID}}", f"schema-graph-{uuid.uuid4().hex}")
        .replace("{{FASTAPI_PUBLIC_URL}}", api_base)
        .replace("{{SESSION_ID}}", session_id)
        .replace("{{SCHEMA_JSON}}", json.dumps(data))
    )
    components.html(html, height=720)

def render_schema_graph(path: str):
    data = _load_graph_json(path)

    # unwrap nested: {"fact_sales": {...}}
    if "nodes" not in data and isinstance(data, dict):
        for _k, v in data.items():
            if isinstance(v, dict) and ("nodes" in v or "edges" in v):
                data = v
                break

    raw_nodes = data.get("nodes", []) or []
    raw_edges = data.get("edges", []) or []
    measures = data.get("measures", []) or []

    def nid(n): return str(n.get("id") or "")
    def ntype(n): return str(n.get("type") or "dimension").lower()

    fact_id = str(data.get("fact") or "fact_sales")

    # ---- Build nodes (4 types) ----
    nodes = []
    node_kind = {}  # id -> kind for styling/legend

    # Fact node (center)
    nodes.append(Node(
        id=fact_id, label=fact_id, size=38, shape="square", color="#d9534f",
        font=NODE_FONT
    ))
    node_kind[fact_id] = "fact"

    # Dimension nodes + Attribute nodes
    for n in raw_nodes:
        _id = nid(n)
        t = ntype(n)
        if not _id or _id == fact_id:
            continue

        if t == "dimension":
            nodes.append(Node(
                id=_id, label=_id, size=26, shape="square", color="#7aa6d6",
                font=NODE_FONT
            ))
            node_kind[_id] = "dimension"

            attrs = n.get("attributes") or []
            if isinstance(attrs, list):
                for a in attrs:
                    aid = f"{_id}::{a}"
                    nodes.append(Node(
                        id=aid, label=str(a), size=16, shape="dot", color="#f0ad4e",
                        font=ATTR_FONT
                    ))
                    node_kind[aid] = "attribute"

    # Measure nodes around fact
    for m in measures:
        mid = f"{fact_id}::m::{m}"
        nodes.append(Node(
            id=mid, label=str(m).replace("_"," "), size=18, shape="diamond", color="#5cb85c",
            font=MEASURE_FONT
        ))
        node_kind[mid] = "measure"

    # ---- Build edges (3 types) ----
    edges = []

    # fact -> dimension (solid) using your FK/PK
    for e in raw_edges:
        src = str(e.get("source") or "")
        tgt = str(e.get("target") or "")
        if not src or not tgt:
            continue
        fk = str(e.get("fk_field") or "")
        pk = str(e.get("pk_field") or "")
        label = f"{fk} → {pk}" if (fk or pk) else ""

        edges.append(
            Edge(
                source=src,
                target=tgt,
                label="",
                color="#999999",
                width=2,
            )
        )

    # dimension -> attribute (dashed, light)
    for n in raw_nodes:
        _id = nid(n)
        if not _id or ntype(n) != "dimension":
            continue
        attrs = n.get("attributes") or []
        if isinstance(attrs, list):
            for a in attrs:
                aid = f"{_id}::{a}"
                edges.append(
                    Edge(
                        source=_id,
                        target=aid,
                        label="",
                        color="#c0c0c0",
                        width=1,
                        dashes=True,
                    )
                )

    # fact -> measure (dotted/green)
    for m in measures:
        mid = f"{fact_id}::m::{m}"
        edges.append(
            Edge(
                source=fact_id,
                target=mid,
                label="",
                color="#5cb85c",
                width=1,
                dashes=True,
            )
        )

    # ---- Layout tuning ----
    cfg = Config(
        width="100%",
        height=1000,
        directed=False,
        physics=True,
        hierarchical=False,
        nodeHighlightBehavior=True,
        highlightColor="#F7A7A6",
        collapsible=True,
        min_zoom=0.12,
        max_zoom=2.2,
        options={
            "interaction": {"hover": True, "navigationButtons": True},

            # pread nodes apart
            "physics": {
                "solver": "barnesHut",
                "barnesHut": {
                    "gravitationalConstant": -20000,  # more negative = more repulsion
                    "centralGravity": 0.20,           # lower keeps clusters from collapsing
                    "springLength": 300,              # longer edges
                    "springConstant": 0.02,           # stiffness
                    "damping": 0.35,                  # less jitter
                    "avoidOverlap": 1.0
                },
                "stabilization": {"iterations": 250}
            }
        }
    )


    st.caption(f"schema nodes={len(nodes)} edges={len(edges)}")
    agraph(nodes=nodes, edges=edges, config=cfg)

    # Simple legend (matches your screenshot intent)
    st.markdown(
        """
        **Legend:** 🟥 Fact &nbsp;&nbsp; 🟦 Dimension &nbsp;&nbsp; 🟠 Attribute &nbsp;&nbsp; 🟩 Measure
        """
    )
st.set_page_config(layout="wide", initial_sidebar_state="expanded")
st.session_state.setdefault("superset_username", DEFAULT_SUPERSET_USERNAME)
st.session_state.setdefault("superset_password", DEFAULT_SUPERSET_PASSWORD)
st.session_state.setdefault("agent_model", "gpt-5-mini")
st.session_state.setdefault("embed_auth_mode", DEFAULT_EMBED_AUTH_MODE)
_default_user_for_dashboard = (
    STREAMLIT_USER_ID
    or str(st.session_state.get("superset_username") or "")
    or DEFAULT_SUPERSET_USERNAME
)
_default_dashboard_id = DEFAULT_DASHBOARD_ID_BY_USER.get(
    _default_user_for_dashboard, "12"
)
st.session_state.setdefault("dashboard_id", _default_dashboard_id)
st.markdown("""
<style>
/* Expander title text */
div[data-testid="stExpander"] summary p {
    font-size: 20px !important;
    font-weight: 700 !important;
}
</style>
""", unsafe_allow_html=True)

# --- Sidebar Chat ---
with st.sidebar:
    st.session_state.setdefault("session_id", uuid.uuid4().hex)
    chat_container_id = f"chat-component-{uuid.uuid4().hex}"
    dashboard_id_value = str(st.session_state.get("dashboard_id") or "")
    superset_username_value = str(st.session_state.get("superset_username") or "")
    effective_user_id = STREAMLIT_USER_ID or superset_username_value or DEFAULT_SUPERSET_USERNAME
    superset_password_value = str(st.session_state.get("superset_password") or "")
    agent_model_value = str(st.session_state.get("agent_model") or "gpt-5-nano")
    chat_html = (
        _load_html_template("chat.html")
        .replace("{{CHAT_CONTAINER_ID}}", chat_container_id)
        .replace("{{FASTAPI_PUBLIC_URL}}", FASTAPI_PUBLIC_URL)
        .replace("{{SESSION_ID}}", st.session_state["session_id"])
        .replace("{{USER_ID}}", effective_user_id)
        .replace("{{DASHBOARD_ID}}", dashboard_id_value)
        .replace("{{SUPERSET_USERNAME}}", superset_username_value)
        .replace("{{SUPERSET_PASSWORD}}", superset_password_value)
        .replace("{{AGENT_MODEL}}", agent_model_value)
    )
    components.html(chat_html, height=830)


# --- Main area: Dashboard ---
st.markdown("""
    <style>
    .block-container {
        padding-top: 1rem;
    }
    </style>
    """, unsafe_allow_html=True)
st.title("TwinBI 🐝")
active_context_container_id = f"superset-active-context-{uuid.uuid4().hex}"
active_context_dashboard_id = str(st.session_state.get("dashboard_id") or "")
active_context_superset_username = str(st.session_state.get("superset_username") or "")
active_context_superset_password = str(st.session_state.get("superset_password") or "")
active_context_user_id = STREAMLIT_USER_ID or active_context_superset_username or DEFAULT_SUPERSET_USERNAME
active_context_html = (
    _load_html_template("active_context.html")
    .replace("{{CONTAINER_ID}}", active_context_container_id)
    .replace("{{API_BASE}}", FASTAPI_PUBLIC_URL)
    .replace("{{DASHBOARD_ID}}", active_context_dashboard_id)
    .replace("{{SESSION_ID}}", st.session_state.get("session_id", ""))
    .replace("{{USER_ID}}", active_context_user_id)
    .replace("{{SUPERSET_USERNAME}}", active_context_superset_username)
    .replace("{{SUPERSET_PASSWORD}}", active_context_superset_password)
)
components.html(active_context_html, height=60)

def get_dashboard_uuid_by_id(
    dashboard_id: str,
    superset_username: str,
    superset_password: str,
) -> str:
    if not dashboard_id:
        return ""
    sess, _access_token = _api_session_with_bearer(superset_username, superset_password)
    r = sess.get(f"{SUPERSET_INTERNAL_URL}/api/v1/dashboard/{dashboard_id}", timeout=30)
    r.raise_for_status()
    return r.json()["result"]["uuid"]

# @st.cache_data(ttl=240)
def get_embed_uuid_by_dashboard_id(
    dashboard_id: str,
    superset_username: str,
    superset_password: str,
) -> str:
    if not dashboard_id:
        return ""
    sess, _access_token = _api_session_with_bearer(superset_username, superset_password)
    r = sess.get(f"{SUPERSET_INTERNAL_URL}/api/v1/dashboard/{dashboard_id}/embedded", timeout=30)
    if r.status_code == 404:
        return ""
    r.raise_for_status()
    return r.json()["result"]["uuid"]

with st.sidebar:
    AGENT_MODEL = st.selectbox(
        "OpenAI Model",
        options=MODEL_OPTIONS,
        index=MODEL_OPTIONS.index(st.session_state.get("agent_model", "gpt-5-mini"))
        if st.session_state.get("agent_model", "gpt-5-mini") in MODEL_OPTIONS
        else 0,
        key="agent_model",
    )
    DASHBOARD_ID = st.text_input(
        "Dashboard ID",
        value=st.session_state.get("dashboard_id", _default_dashboard_id),
        help="Superset Dashboard Numeric ID",
        key="dashboard_id",
    )
    SUPERSET_USERNAME = str(st.session_state.get("superset_username", DEFAULT_SUPERSET_USERNAME))
    SUPERSET_PASSWORD = str(st.session_state.get("superset_password", DEFAULT_SUPERSET_PASSWORD))
    current_embed_mode = st.session_state.get("embed_auth_mode", DEFAULT_EMBED_AUTH_MODE)
    if current_embed_mode == "embedded_sdk":
        current_embed_mode = "guest_token"
    EMBED_AUTH_MODE = st.selectbox(
        "Embed Auth Mode",
        options=["session_iframe", "guest_token"],
        index=0
        if current_embed_mode == "session_iframe"
        else 1,
        key="embed_auth_mode",
        help="session_iframe uses your browser Superset login session (non-guest behavior).",
    )
    if EMBED_AUTH_MODE == "session_iframe":
        st.caption(
            "Session iframe mode: login to Superset in this browser first. "
            "If dashboard does not load, open Superset and authenticate, then rerun."
        )
        st.link_button("open superset", f"{SUPERSET_PUBLIC_URL}/login/")
        if st.button("logout superset", key="logout_superset_btn", use_container_width=True):
            logout_url = f"{SUPERSET_PUBLIC_URL}/logout/"
            components.html(
                f"""
                <script>
                (async function() {{
                  try {{
                    await fetch("{logout_url}", {{ credentials: "include", mode: "no-cors" }});
                  }} catch (e) {{}}
                }})();
                </script>
                """,
                height=0,
            )
            st.success("Superset logout request sent. Rerun if needed.")
    DASHBOARD_UUID = (
        get_dashboard_uuid_by_id(DASHBOARD_ID, SUPERSET_USERNAME, SUPERSET_PASSWORD)
        if DASHBOARD_ID and SUPERSET_USERNAME and SUPERSET_PASSWORD
        else ""
    )
    EMBED_UUID = (
        get_embed_uuid_by_dashboard_id(DASHBOARD_ID, SUPERSET_USERNAME, SUPERSET_PASSWORD)
        if DASHBOARD_ID and SUPERSET_USERNAME and SUPERSET_PASSWORD
        else ""
    )
    # st.write("USERNAME:", SUPERSET_USERNAME)
    # st.write("PASSWORD:", SUPERSET_PASSWORD)
    # st.write("DASHBOARD_ID:", DASHBOARD_ID)
    # st.write("DASHBOARD_UUID:", DASHBOARD_UUID)
    # st.write("EMBED_UUID:", EMBED_UUID)
    st.write("SESSION_ID:", st.session_state.get("session_id"))
    # st.write("--------------------------------")
    
dashboard_height = 650
refresh_counter = st.session_state.get("embed_refresh_counter", 0)
try:
    refresh_counter = int(refresh_counter)
except Exception:
    refresh_counter = 0
st.session_state["embed_refresh_counter"] = refresh_counter + 1

if EMBED_AUTH_MODE == "session_iframe":
    if not DASHBOARD_ID:
        st.error("Dashboard ID is required for session iframe embed.")
        st.stop()
    session_user_id = STREAMLIT_USER_ID or SUPERSET_USERNAME or DEFAULT_SUPERSET_USERNAME
    iframe_query = {
        "standalone": "1",
        "show_filters": "1",
        "expand_filters": "0",
        "_r": str(st.session_state["embed_refresh_counter"]),
        # Session iframe does not expose rich postMessage events reliably.
        # Persist user/session hints in referrer query so FastAPI poller can
        # route Superset logs to the intended per-user DB.
        "agent_user_name": str(session_user_id or ""),
        "agent_user_key": str(session_user_id or ""),
        "agent_session_id": str(st.session_state.get("session_id", "") or ""),
    }
    iframe_src = (
        f"{SUPERSET_PUBLIC_URL}/superset/dashboard/{urllib.parse.quote(str(DASHBOARD_ID))}/"
        f"?{urllib.parse.urlencode(iframe_query)}"
    )
    iframe_bridge_id = f"superset-iframe-bridge-{uuid.uuid4().hex}"
    session_iframe_html = f"""
    <div style="width:100%;height:{dashboard_height}px;overflow:hidden;border-radius:12px;">
      <iframe id="{iframe_bridge_id}" src="{iframe_src}" style="width:100%;height:100%;border:0;" allowfullscreen></iframe>
    </div>
    <script>
      (() => {{
        const apiBase = {json.dumps(FASTAPI_PUBLIC_URL)};
        const supersetOrigin = (() => {{
          try {{ return new URL({json.dumps(SUPERSET_PUBLIC_URL)}).origin; }} catch {{ return ""; }}
        }})();
        const dashboardId = {json.dumps(str(DASHBOARD_ID))};
        const sessionId = {json.dumps(st.session_state.get("session_id", ""))};
        const userId = {json.dumps(session_user_id)};
        const deviceStorageKey = "agent4olap_device_id";
        const deviceId = (() => {{
          try {{
            let id = localStorage.getItem(deviceStorageKey);
            if (!id) {{
              id = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `${{Date.now()}}-${{Math.random().toString(16).slice(2)}}`;
              localStorage.setItem(deviceStorageKey, id);
            }}
            return id;
          }} catch {{
            return "";
          }}
        }})();
        const postEvent = async (eventType, payload) => {{
          if (!apiBase || !sessionId) return;
          try {{
            await fetch(`${{apiBase.replace(/\\/+$/, "")}}/events`, {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{
                session_id: sessionId,
                user_id: userId || null,
                device_id: deviceId || null,
                event_type: eventType,
                payload: payload || {{}},
              }}),
            }});
          }} catch (err) {{
            console.warn("[session-iframe] event post failed", err);
          }}
        }};
        const activeCtxUrl = (() => {{
          const qs = new URLSearchParams();
          if (dashboardId) qs.set("dashboard_id", String(dashboardId));
          if (sessionId) qs.set("session_id", String(sessionId));
          if (userId) qs.set("user_name", String(userId));
          if (deviceId) qs.set("device_id", String(deviceId));
          return `${{apiBase.replace(/\\/+$/, "")}}/superset/charts/active?${{qs.toString()}}`;
        }})();
        let lastActiveSig = "";
        let lastUiSig = "";
        const emitActiveContextChanges = async () => {{
          if (!activeCtxUrl) return;
          try {{
            const res = await fetch(activeCtxUrl, {{ method: "GET" }});
            if (!res.ok) return;
            const data = await res.json();
            const chart = data?.interacting_chart || null;
            const tab = data?.active_tab || null;
            const chartSig = chart ? `${{chart.slice_id || ""}}|${{chart.name || ""}}` : "";
            const tabSig = tab ? `${{tab.tab_id || ""}}|${{tab.tab_name || ""}}` : "";
            const nextActiveSig = `${{chartSig}}||${{tabSig}}`;
            if (nextActiveSig && nextActiveSig !== lastActiveSig) {{
              lastActiveSig = nextActiveSig;
              if (chart && chart.slice_id) {{
                await postEvent("chart_activity", {{
                  dashboard_id: Number(dashboardId) || null,
                  slice_id: chart.slice_id,
                  chart_name: chart.name || null,
                  source: "active_context_poll",
                }});
              }}
              if (tab && (tab.tab_id || tab.tab_name)) {{
                await postEvent("superset_tab_active", {{
                  dashboard_id: Number(dashboardId) || null,
                  tab_id: tab.tab_id || null,
                  tab_name: tab.tab_name || null,
                  source: "active_context_poll",
                }});
              }}
            }}
            const ui = data?.last_ui_event || null;
            if (ui && ui.action) {{
              const uiSig = `${{ui.superset_log_id || ""}}|${{ui.action}}|${{ui.slice_id || ""}}`;
              if (uiSig !== lastUiSig) {{
                lastUiSig = uiSig;
                await postEvent(String(ui.action), {{
                  dashboard_id: Number(dashboardId) || null,
                  slice_id: ui.slice_id || null,
                  payload: ui.payload || null,
                  source: "active_context_poll",
                }});
              }}
            }}
          }} catch (err) {{
            console.debug("[session-iframe] active context poll failed", err);
          }}
        }};
        postEvent("iframe_loaded", {{ dashboard_id: Number(dashboardId) || null, source: "session_iframe" }});
        emitActiveContextChanges();
        window.setInterval(emitActiveContextChanges, 1500);

        window.addEventListener("message", (e) => {{
          const originOk = (
            (supersetOrigin && e.origin === supersetOrigin) ||
            String(e.origin || "").includes(":58088")
          );
          if (!originOk || !e.data) return;
          let data = e.data;
          if (typeof data === "string") {{
            try {{ data = JSON.parse(data); }} catch {{}}
          }}
          const obj = (data && typeof data === "object") ? data : {{}};
          const payload = (obj.payload && typeof obj.payload === "object") ? obj.payload : {{}};
          const eventType = String(obj.event || obj.event_type || payload.event || payload.event_type || "");
          const tabLike = /tab/i.test(typeof e.data === "string" ? e.data : JSON.stringify(e.data));
          if (eventType || obj.chart_id || obj.chartId || payload.chart_id || payload.chartId || tabLike) {{
            postEvent(eventType || (tabLike ? "superset_tab_click" : "superset_ui_event"), {{
              dashboard_id: Number(dashboardId) || null,
              slice_id: obj.chart_id || obj.chartId || payload.chart_id || payload.chartId || payload.slice_id || payload.sliceId || null,
              payload: obj.payload || obj,
              raw: typeof e.data === "string" ? e.data : null,
            }});
          }}
        }}, true);
      }})();
    </script>
    """
    components.html(session_iframe_html, height=dashboard_height + 8, scrolling=False)
else:
    token = (
        get_guest_token(DASHBOARD_ID, SUPERSET_USERNAME, SUPERSET_PASSWORD)
        if DASHBOARD_ID and SUPERSET_USERNAME and SUPERSET_PASSWORD
        else ""
    )
    if not token:
        st.error(
            "Failed to get guest token for Superset dashboard. "
            f"EMBED_UUID={bool(EMBED_UUID)} DASHBOARD_UUID={bool(DASHBOARD_UUID)}"
        )
        st.stop()

    superset_embed(
        dashboard_id=EMBED_UUID,
        superset_domain=SUPERSET_PUBLIC_URL,
        guest_token=token,
        event_api_base=FASTAPI_PUBLIC_URL,
        session_id=st.session_state.get("session_id"),
        user_id=(STREAMLIT_USER_ID or SUPERSET_USERNAME or DEFAULT_SUPERSET_USERNAME),
        height=dashboard_height,
        key=f"dash_{DASHBOARD_ID}_{st.session_state['embed_refresh_counter']}",
    )


# Results section with tabs
st.markdown("---")  # Divider line

st.markdown("""
    <style>
    /* Make tabs tighter */
    .stTabs [data-baseweb="tab-list"] button [data-testid="stMarkdownContainer"] p {
        font-size: 20px;
    }
    .stTabs [data-baseweb="tab-list"] {
        padding-top: 0rem;
    }
    /* Reduce space after divider */
    hr {
        margin-bottom: 1rem;
    }
    /* Reduce space around header */
    h2 {
        margin-top: 0rem;
        margin-bottom: 0.5rem;
    }
    /* Reduce space above tabs */
    .stTabs {
        margin-top: -0.5rem;
    }
    </style>
    """, unsafe_allow_html=True)

with st.expander("Output", expanded=True):

    # Create tabs
    # tab1, tab2, tab3 = st.tabs(["Table", "SQL", "Logs"])
    tab1, tab2, tab3 = st.tabs(["SQL", "Schema", "Logs"])

    # with tab1:
    #     result = st.session_state.get("last_chat_result")
    #     error = st.session_state.get("last_chat_error")
    #     rows = result.get("data") if isinstance(result, dict) else None
    #     if error:
    #         st.error(f"FastAPI error: {error}")
    #     elif rows:
    #         st.dataframe(rows, use_container_width=True)
    #     else:
    #         st.info("No query results yet. Ask a question in the chat sidebar.")

    with tab1:
        sql_params = {
            "limit": 50,
            "poll_interval_sec": 1.0,
            "source": "superset",
            "action": "ChartDataRestApi.data",
        }
        sql_user_name = STREAMLIT_USER_ID or SUPERSET_USERNAME or DEFAULT_SUPERSET_USERNAME
        if sql_user_name:
            sql_params["user_name"] = sql_user_name
        if DASHBOARD_ID:
            sql_params["dashboard_id"] = DASHBOARD_ID
        sql_query = urllib.parse.urlencode(sql_params)
        sql_stream_base = f"{FASTAPI_PUBLIC_URL}/events/stream?{sql_query}"
        charts_url = f"{FASTAPI_PUBLIC_URL}/superset/dashboards/{DASHBOARD_ID}/charts"
        sql_container_id = f"superset-sql-stream-{uuid.uuid4().hex}"
        sql_storage_key = f"superset-sql-last-id-{DASHBOARD_ID or 'all'}"
        log_storage_key = f"superset-log-last-id-{DASHBOARD_ID or 'all'}"

        sql_html = (
            _load_html_template("tab1.html")
            .replace("{{SQL_CONTAINER_ID}}", sql_container_id)
            .replace("{{SQL_STORAGE_KEY}}", sql_storage_key)
            .replace("{{LOG_STORAGE_KEY}}", log_storage_key)
            .replace("{{SQL_STREAM_BASE}}", sql_stream_base)
            .replace("{{CHARTS_URL}}", charts_url)
            .replace("{{SUPERSET_USERNAME}}", SUPERSET_USERNAME or "")
            .replace("{{SUPERSET_PASSWORD}}", SUPERSET_PASSWORD or "")
        )
        components.html(sql_html, height=560) # 560

    with tab2:
        st.subheader("Schema graph")
        graph_data = _load_graph_json("/app/cube_data/sales/sales-star-graph.json")
        hierarchies = _load_hierarchies("/app/cube_data/sales/hierarchy")
        nodes, edges, fact_id = _schema_nodes_edges(graph_data, hierarchies)
        
        fig = make_plotly_figure(
            nodes,
            edges,
            layout="kamada_kawai",
            root_id=fact_id,
            height="1000px",
            width="100%",
            legend_toggles_labels=False,
            node_opacity=1.0,
            node_spacing={"measure": 1.2, "dimension": 1.1, "attribute": 0.8, "default": 0.9},
            node_properties={"fontsize": {"fact": 16, "dimension": 14, "default": 14}},
        )
        st.plotly_chart(
            fig,
            use_container_width=True,
        )

    with tab3:
        st.subheader("Logs")
        filter_cols = st.columns([1.2, 1.2, 1.2])
        source_filter = filter_cols[0].selectbox(
            "Source",
            options=["superset"],
            index=0,
            key="log_source_filter",
        )
        superset_user_id = filter_cols[1].text_input(
            "Superset user_id",
            value=st.session_state.get("superset_user_id_filter", ""),
            key="superset_user_id_filter",
        )
        action_filter = filter_cols[2].text_input(
            "Action",
            value=st.session_state.get("action_filter", ""),
            key="action_filter",
        )
        params = {
            "limit": 50,
            "poll_interval_sec": 1.0,
            "source": source_filter,
        }
        if DASHBOARD_ID:
            params["dashboard_id"] = DASHBOARD_ID
        if superset_user_id.isdigit():
            params["user_id"] = int(superset_user_id)
        if action_filter:
            params["action"] = action_filter
        query = urllib.parse.urlencode(params)
        stream_base_url = f"{FASTAPI_PUBLIC_URL}/events/stream?{query}"
        container_id = f"superset-log-stream-{uuid.uuid4().hex}"
        dashboard_key = str(DASHBOARD_ID or "all")

        logs_html = (
            _load_html_template("tab3.html")
            .replace("{{CONTAINER_ID}}", container_id)
            .replace("{{API_BASE}}", FASTAPI_PUBLIC_URL)
            .replace("{{STREAM_BASE_URL}}", stream_base_url)
            .replace("{{DASHBOARD_KEY}}", dashboard_key)
        )
        components.html(logs_html, height=620)

        # st.subheader("Last Activated Context")
        # context_controls = st.columns([1, 1, 6])
        # see_context = context_controls[0].button("See Context", key="see_context_btn")
        # clear_context = context_controls[1].button("Clear Context", key="clear_context_btn")
        # if clear_context:
        #     try:st.divider()
        #         requests.delete(f"{FASTAPI_INTERNAL_URL}/chat/context", timeout=3)
        #         st.session_state["context_cleared_notice"] = True
        #     except Exception:
        #         st.session_state["context_cleared_notice"] = False
        # if see_context:
        #     st.session_state["context_cleared_notice"] = False
        #     try:
        #         resp = requests.get(f"{FASTAPI_INTERNAL_URL}/chat/context/latest", timeout=3)
        #         if resp.ok:
        #             st.session_state["context_override"] = resp.json().get("context")
        #     except Exception:
        #         st.session_state["context_override"] = None
        # context_payload = None
        # try:
        #     resp = requests.get(f"{FASTAPI_INTERNAL_URL}/chat/context/latest", timeout=3)
        #     if resp.ok:
        #         context_payload = resp.json().get("context")
        # except Exception:
        #     context_payload = None
        # if st.session_state.get("context_override") is not None:
        #     context_payload = st.session_state.get("context_override")
        # if st.session_state.get("context_cleared_notice"):
        #     st.caption("Context cleared.")
        # if context_payload is None:
        #     st.info("No context available yet.")
        # else:
        #     st.json(context_payload, expanded=False)

#         st.subheader("Chat logs")
#         debug_container_id = f"chat-debug-{uuid.uuid4().hex}"
#         debug_html = f"""
#         <div id="{debug_container_id}" style="font-family: sans-serif; color:#fff;">
#           <pre style="font-size:12px; color:#ddd; white-space:pre-wrap; margin:0; max-height:500px; overflow:auto;">
# Loading debug log...
#           </pre>
#         </div>
#         <script>
#           const root = document.getElementById("{debug_container_id}");
#           const pre = root.querySelector("pre");
#           const apiBase = "{FASTAPI_PUBLIC_URL}";
#           async function loadDebug() {{
#             try {{
#               const res = await fetch(`${{apiBase}}/chat/debug/latest`);
#               if (!res.ok) return;
#               const data = await res.json();
#               pre.textContent = JSON.stringify(data.debug || "No debug yet.", null, 2);
#             }} catch (err) {{}}
#           }}
#           loadDebug();
#           setInterval(loadDebug, 2000);
#         </script>
#         """
#         components.html(debug_html, height=220)

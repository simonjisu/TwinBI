from __future__ import annotations

import os
import json
import requests
import streamlit as st
import streamlit.components.v1 as components
import uuid
import urllib.parse
from pathlib import Path

from superset_embed_component import superset_embed


from streamlit_agraph import agraph, Node, Edge, Config

SUPERSET_PUBLIC_URL = os.getenv("SUPERSET_PUBLIC_URL", "http://localhost:8088")
SUPERSET_INTERNAL_URL = os.getenv("SUPERSET_INTERNAL_URL", "http://superset_app:8088")
FASTAPI_INTERNAL_URL = os.getenv("FASTAPI_INTERNAL_URL", "http://localhost:8000")
FASTAPI_PUBLIC_URL = os.getenv("FASTAPI_PUBLIC_URL", FASTAPI_INTERNAL_URL)
STREAMLIT_USER_ID = os.getenv("STREAMLIT_USER_ID", "streamlit_user")

SUPERSET_USERNAME = os.getenv("SUPERSET_USERNAME", "admin")
SUPERSET_PASSWORD = os.getenv("SUPERSET_PASSWORD", "admin")

GUEST_TOKEN_AUD = os.getenv("SUPERSET_GUEST_AUD", "superset")


def _api_session_with_bearer() -> tuple[requests.Session, str]:
    s = requests.Session()
    r = s.post(
        f"{SUPERSET_INTERNAL_URL}/api/v1/security/login",
        json={
            "username": SUPERSET_USERNAME,
            "password": SUPERSET_PASSWORD,
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
def get_guest_token(dashboard_uuid: str) -> str:
    sess, _access = _api_session_with_bearer()
    csrf = _api_get_csrf(sess)

    payload = {
        "resources": [{"type": "dashboard", "id": str(dashboard_uuid)}],
        "rls": [],
        "user": {"username": "streamlit-guest"},
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
        height=560,
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
    chat_html = (
        _load_html_template("chat.html")
        .replace("{{CHAT_CONTAINER_ID}}", chat_container_id)
        .replace("{{FASTAPI_PUBLIC_URL}}", FASTAPI_PUBLIC_URL)
        .replace("{{SESSION_ID}}", st.session_state["session_id"])
        .replace("{{USER_ID}}", STREAMLIT_USER_ID)
        .replace("{{DASHBOARD_ID}}", dashboard_id_value)
    )
    components.html(chat_html, height=600)

    st.divider()

    with st.expander("Schema", expanded=False):
        render_schema_graph("/app/cube_data/sales/sales-star-graph.json")

# --- Main area: Dashboard ---
st.markdown("""
    <style>
    .block-container {
        padding-top: 1rem;
    }
    </style>
    """, unsafe_allow_html=True)
st.title("Agent4OLAP")


def get_dashboard_uuid_by_id(dashboard_id: str) -> str:
    if not dashboard_id:
        return ""
    sess, access_token = _api_session_with_bearer()
    r = sess.get(f"{SUPERSET_INTERNAL_URL}/api/v1/dashboard/{dashboard_id}", timeout=30)
    r.raise_for_status()
    return r.json()["result"]["uuid"]

# @st.cache_data(ttl=240)
def get_embed_uuid_by_dashboard_id(dashboard_id: str) -> str:
    if not dashboard_id:
        return ""
    sess, access_token = _api_session_with_bearer()
    r = sess.get(f"{SUPERSET_INTERNAL_URL}/api/v1/dashboard/{dashboard_id}/embedded", timeout=30)
    if r.status_code == 404:
        return ""
    r.raise_for_status()
    return r.json()["result"]["uuid"]

with st.sidebar:
    DASHBOARD_ID = st.text_input(
        "Dashboard ID",
        value=st.session_state.get("dashboard_id", "12"),
        help="Superset Dashboard Numeric ID",
        key="dashboard_id",
    )
    DASHBOARD_UUID = get_dashboard_uuid_by_id(DASHBOARD_ID) if DASHBOARD_ID else ""
    EMBED_UUID = get_embed_uuid_by_dashboard_id(DASHBOARD_ID) if DASHBOARD_ID else ""
    st.write("USERNAME:", SUPERSET_USERNAME)
    st.write("PASSWORD:", SUPERSET_PASSWORD)
    st.write("DASHBOARD_ID (api):", DASHBOARD_ID)
    st.write("DASHBOARD_UUID (api):", DASHBOARD_UUID)
    st.write("EMBED_UUID (api):", EMBED_UUID)
    st.write("SESSION_ID:", st.session_state.get("session_id"))
    st.write("--------------------------------")
    
token = get_guest_token(DASHBOARD_ID) if DASHBOARD_ID else ""
if not token:
    st.error(f"Failed to get guest token for Superset dashboard. Maybe the UUID is not reachable? EMBED_UUID={bool(EMBED_UUID)} or DASHBOARD_UUID={bool(DASHBOARD_UUID)} is invalid or embedding is not enabled.")
    st.stop()
else:
    superset_embed(
        dashboard_id=EMBED_UUID,
        superset_domain=SUPERSET_PUBLIC_URL,
        guest_token=token,
        event_api_base=FASTAPI_PUBLIC_URL,
        session_id=st.session_state.get("session_id"),
        user_id=STREAMLIT_USER_ID,
        height=1000,
        key="dash_2",
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
    tab1, tab2 = st.tabs(["SQL", "Logs"])

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
        sql_params = {"limit": 100, "poll_interval_sec": 1.0}
        if DASHBOARD_ID:
            sql_params["dashboard_id"] = DASHBOARD_ID
        sql_query = urllib.parse.urlencode(sql_params)
        sql_stream_base = f"{FASTAPI_PUBLIC_URL}/superset/logs/stream?{sql_query}"
        charts_url = f"{FASTAPI_PUBLIC_URL}/superset/dashboards/{DASHBOARD_ID}/charts"
        sql_container_id = f"superset-sql-stream-{uuid.uuid4().hex}"
        sql_storage_key = f"superset-sql-last-id-{DASHBOARD_ID or 'all'}"

        sql_html = (
            _load_html_template("tab1.html")
            .replace("{{SQL_CONTAINER_ID}}", sql_container_id)
            .replace("{{SQL_STORAGE_KEY}}", sql_storage_key)
            .replace("{{SQL_STREAM_BASE}}", sql_stream_base)
            .replace("{{CHARTS_URL}}", charts_url)
        )
        components.html(sql_html, height=560)

    with tab2:
        st.subheader("Superset logs")
        params = {"limit": 100, "poll_interval_sec": 1.0}
        if DASHBOARD_ID:
            params["dashboard_id"] = DASHBOARD_ID
        query = urllib.parse.urlencode(params)
        stream_base_url = f"{FASTAPI_PUBLIC_URL}/superset/logs/stream?{query}"
        ui_params = {"limit": 100, "poll_interval_sec": 1.0}
        session_id = st.session_state.get("session_id")
        if session_id:
            ui_params["session_id"] = session_id
        ui_query = urllib.parse.urlencode(ui_params)
        ui_stream_base_url = f"{FASTAPI_PUBLIC_URL}/events/stream?{ui_query}"
        container_id = f"superset-log-stream-{uuid.uuid4().hex}"
        dashboard_key = str(DASHBOARD_ID or "all")

        logs_html = (
            _load_html_template("tab2.html")
            .replace("{{CONTAINER_ID}}", container_id)
            .replace("{{API_BASE}}", FASTAPI_PUBLIC_URL)
            .replace("{{STREAM_BASE_URL}}", stream_base_url)
            .replace("{{UI_STREAM_BASE_URL}}", ui_stream_base_url)
            .replace("{{DASHBOARD_KEY}}", dashboard_key)
        )
        components.html(logs_html, height=620)

        st.subheader("Chat logs")
        debug_container_id = f"chat-debug-{uuid.uuid4().hex}"
        debug_html = f"""
        <div id="{debug_container_id}" style="font-family: sans-serif; color:#fff;">
          <pre style="font-size:12px; color:#ddd; white-space:pre-wrap; margin:0; max-height:200px; overflow:auto;">
Loading debug log...
          </pre>
        </div>
        <script>
          const root = document.getElementById("{debug_container_id}");
          const pre = root.querySelector("pre");
          const apiBase = "{FASTAPI_PUBLIC_URL}";
          async function loadDebug() {{
            try {{
              const res = await fetch(`${{apiBase}}/chat/debug/latest`);
              if (!res.ok) return;
              const data = await res.json();
              pre.textContent = JSON.stringify(data.debug || "No debug yet.", null, 2);
            }} catch (err) {{}}
          }}
          loadDebug();
          setInterval(loadDebug, 2000);
        </script>
        """
        components.html(debug_html, height=220)

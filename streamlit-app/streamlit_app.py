from __future__ import annotations

import os
import json
import requests
import streamlit as st
import streamlit.components.v1 as components
from pathlib import Path
import html

from superset_embed_component import superset_embed

from streamlit_agraph import agraph, Node, Edge, Config

SUPERSET_PUBLIC_URL = os.getenv("SUPERSET_PUBLIC_URL", "http://localhost:8088")
SUPERSET_INTERNAL_URL = os.getenv("SUPERSET_INTERNAL_URL", "http://superset_app:8088")

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
    st.title("Chat")

    st.markdown(
        """
        <style>
        /* Panel */
        section[data-testid="stSidebar"] .chat-panel{
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 14px;
            padding: 7px;
            background: rgba(255,255,255,0.03);
        }

        /* CSS-only "stick-to-bottom" scroller */
        section[data-testid="stSidebar"] .scroller{
            overflow: auto;
            height: 420px;              /* <-- controls where input sits */
            display: flex;
            flex-direction: column-reverse;
            overflow-anchor: auto !important;
        }

        section[data-testid="stSidebar"] .scroller-content{
            display: flex;
            flex-direction: column;
            gap: 10px;
            padding: 6px 0 12px 0;
        }

        /* Bubble + meta */
        section[data-testid="stSidebar"] .meta{
            display:flex; align-items:center;
            margin: 0 2px -6px 2px;
            opacity: 0.9;
        }
        section[data-testid="stSidebar"] .meta.user{ justify-content:flex-end; }
        section[data-testid="stSidebar"] .meta.assistant{ justify-content:flex-start; }

        section[data-testid="stSidebar"] .avatar{
            width: 22px; height: 22px; border-radius: 999px;
            display:inline-flex; align-items:center; justify-content:center;
            font-size: 14px;
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.10);
            transform: translateZ(0); /* iOS Safari repaint fix */
        }

        section[data-testid="stSidebar"] .bubble{
            padding: 10px 12px;
            border-radius: 16px;
            max-width: 92%;
            line-height: 1.35;
            font-size: 0.95rem;
            white-space: pre-wrap;
            box-shadow: 0 1px 2px rgba(0,0,0,0.25);
        }
        section[data-testid="stSidebar"] .bubble.user{
            margin-left:auto;
            background: rgba(0, 140, 255, 0.22);
            border: 1px solid rgba(0, 140, 255, 0.35);
        }
        section[data-testid="stSidebar"] .bubble.assistant{
            margin-right:auto;
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.12);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # state (safe + minimal)
    st.session_state.setdefault("msgs", [])

    # Slot so the chat box stays ABOVE the input visually,
    # but we can still process input before rendering messages.
    chat_box = st.empty()

    # Input with the arrow inside the field
    prompt = st.chat_input("Ask about what you see…")
    if prompt and prompt.strip():
        st.session_state.msgs.append(("user", prompt.strip()))
        # replace with your real response
        st.session_state.msgs.append(("assistant", "Tell me which chart/scenario you mean and what you want to analyze."))

    # Build ALL chat HTML in one go (reliable DOM)
    parts = []
    for role, msg in st.session_state.msgs[-80:]:
        if not msg or not msg.strip():
            continue
        role_cls = "user" if role == "user" else "assistant"
        icon = "🧑" if role_cls == "user" else "🤖"   # swap to SVG if you want
        safe = html.escape(msg)

        parts.append(
            f'<div class="meta {role_cls}"><span class="avatar">{icon}</span></div>'
            f'<div class="bubble {role_cls}">{safe}</div>'
        )

    chat_html = (
        '<div class="chat-panel">'
        '<div class="scroller">'
        '<div class="scroller-content">'
        + "".join(parts) +
        '</div></div></div>'
    )

    chat_box.markdown(chat_html, unsafe_allow_html=True)

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

# EMBED_UUID = os.getenv("SUPERSET_EMBED_UUID", "")
# DASHBOARD_UUID = os.getenv("SUPERSET_DASHBOARD_UUID", "")
# DASHBOARD_ID = os.getenv("SUPERSET_DASHBOARD_ID", "12")


DASHBOARD_ID = os.getenv("SUPERSET_DASHBOARD_ID", "12")
DASHBOARD_UUID = get_dashboard_uuid_by_id(DASHBOARD_ID)
EMBED_UUID = get_embed_uuid_by_dashboard_id(DASHBOARD_ID)
    
    # st.write("DASHBOARD_ID (env):", DASHBOARD_ID)
    # st.write("DASHBOARD_UUID (env):", DASHBOARD_UUID)
    # st.write("EMBED_UUID (env):", EMBED_UUID)
    # st.write("--------------------------------")
    # st.write("DASHBOARD_ID (api):", DASHBOARD_ID)
    # st.write("DASHBOARD_UUID (api):", DASHBOARD_UUID)
    # st.write("EMBED_UUID (api):", EMBED_UUID)
    # st.write("--------------------------------")
    
token = get_guest_token(DASHBOARD_ID)
# with st.sidebar:
#     st.write("Guest Token:", token)
if not token:
    st.error(f"Failed to get guest token for Superset dashboard. Maybe the UUID is not reachable? EMBED_UUID={bool(EMBED_UUID)} or DASHBOARD_UUID={bool(DASHBOARD_UUID)} is invalid or embedding is not enabled.")
    st.stop()
else:
    superset_embed(
        dashboard_id=EMBED_UUID,
        superset_domain=SUPERSET_PUBLIC_URL,
        guest_token=token,
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
    tab1, tab2 = st.tabs(["Table", "SQL"])

    with tab1:
        # Example: Show a dataframe
        # st.dataframe(your_data)
        
        # Example placeholder content
        import pandas as pd
        sample_data = pd.DataFrame({
            'Date': ['2024-01-01', '2024-01-02', '2024-01-03'],
            'Sales': [1000, 1500, 1200],
            'Revenue': [5000, 7500, 6000]
        })
        st.dataframe(sample_data, use_container_width=True)

    with tab2:
        # Show SQL query
        sql_query = """
    SELECT 
        date,
        SUM(sales) as total_sales,
        SUM(revenue) as total_revenue
    FROM sales_data
    WHERE date >= '2024-01-01'
    GROUP BY date
    ORDER BY date;
    """
        st.code(sql_query, language="sql")

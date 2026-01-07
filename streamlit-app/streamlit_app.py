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

DASHBOARD_ID = os.getenv("SUPERSET_DASHBOARD_ID", "12")

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
def get_guest_token(dashboard_id: str) -> str:
    sess, _access = _api_session_with_bearer()
    csrf = _api_get_csrf(sess)

    payload = {
        "resources": [{"type": "dashboard", "id": str(dashboard_id)}],
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

def render_schema_graph(path: str):
    data = _load_graph_json(path)

    # allow multiple possible keys
    raw_nodes = data.get("nodes", [])
    raw_edges = data.get("edges", [])

    def node_id(n):
        return str(n.get("id") or n.get("name") or n.get("key"))

    def node_label(n):
        return str(n.get("label") or n.get("name") or n.get("id") or n.get("key"))

    nodes = [
        Node(id=node_id(n), label=node_label(n), size=22)
        for n in raw_nodes
        if node_id(n)
    ]

    edges = []
    for e in raw_edges:
        src = e.get("source") or e.get("from")
        tgt = e.get("target") or e.get("to")
        if src and tgt:
            edges.append(Edge(source=str(src), target=str(tgt), label=str(e.get("label", ""))))

    cfg = Config(
        width="100%",
        height=520,
        directed=True,
        physics=True,
    )

    agraph(nodes=nodes, edges=edges, config=cfg)

st.set_page_config(layout="wide", initial_sidebar_state="expanded")

# --- Sidebar Chat ---
# with st.sidebar:
#     st.header("Chat")

#     if "msgs" not in st.session_state:
#         st.session_state.msgs = []

#     # put the input in the sidebar too
#     prompt = st.chat_input("Ask about what you see…")
#     if prompt:
#         st.session_state.msgs.append(("user", prompt))
#         st.session_state.msgs.append(
#             ("assistant", "Tell me which chart/scenario you mean and what you want to analyze.")
#         )

#     for role, msg in st.session_state.msgs:
#         with st.chat_message(role):
#             st.write(msg)

# --- Sidebar Chat ---
with st.sidebar:
    st.header("Chat")

    st.markdown(
        """
        <style>
        /* Panel */
        section[data-testid="stSidebar"] .chat-panel{
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 14px;
            padding: 10px;
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

    # with st.popover("🧬 Schema", use_container_width=True):
    #     st.write("Schema Explorer")
    #     render_schema_graph("/app/cube_data/tutorial/tutorial-star-graph.json")

# --- Main area: Dashboard ---
st.markdown("""
    <style>
    .block-container {
        padding-top: 1rem;
    }
    </style>
    """, unsafe_allow_html=True)
st.title("Agent4OLAP")

EMBED_UUID = os.getenv("SUPERSET_EMBED_UUID", "")
# DASHBOARD_UUID = os.getenv("SUPERSET_DASHBOARD_UUID", "") 

token = get_guest_token(DASHBOARD_ID)  # guest token resource should reference the DASHBOARD uuid
superset_embed(
    dashboard_id=EMBED_UUID,   # the iframe should load /embedded/<EMBED_UUID>
    superset_domain=SUPERSET_PUBLIC_URL,
    guest_token=token,
    height=1550,
    key="dash_2",
)


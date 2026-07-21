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

SUPERSET_PUBLIC_URL = os.getenv("SUPERSET_PUBLIC_URL", "http://localhost:8088")
SUPERSET_INTERNAL_URL = os.getenv("SUPERSET_INTERNAL_URL", "http://superset_app:8088")
FASTAPI_INTERNAL_URL = os.getenv("FASTAPI_INTERNAL_URL", "http://localhost:8000")
FASTAPI_PUBLIC_URL = os.getenv("FASTAPI_PUBLIC_URL", FASTAPI_INTERNAL_URL)
STREAMLIT_USER_ID = os.getenv("STREAMLIT_USER_ID", "streamlit_user")
DEFAULT_EMBED_AUTH_MODE = os.getenv("SUPERSET_EMBED_AUTH_MODE", "session_iframe")

DEFAULT_SUPERSET_USERNAME = ""
DEFAULT_SUPERSET_PASSWORD = ""
DEFAULT_DASHBOARD_ID = os.getenv("DEFAULT_DASHBOARD_ID", "")
MODEL_OPTIONS = [
    "gpt-5.6-terra",
    "gpt-5-nano",
    "gpt-5-mini",
    "gpt-4.1-nano",
    "gpt-4.1-mini",
    "gpt-4o-mini",
]

GUEST_TOKEN_AUD = os.getenv("SUPERSET_GUEST_AUD", "superset")


def _query_param_str(key: str) -> str:
    raw = st.query_params.get(key, "")
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    return str(raw or "").strip()


def _ensure_stable_session_id() -> str:
    query_sid = _query_param_str("session_id")
    state_sid = str(st.session_state.get("session_id") or "").strip()
    stable_sid = query_sid or state_sid or uuid.uuid4().hex
    st.session_state["session_id"] = stable_sid
    if query_sid != stable_sid:
        st.query_params["session_id"] = stable_sid
    return stable_sid


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


def apply_superset_login_meta(
    *,
    session_id: str,
    superset_username: str,
    superset_password: str,
) -> tuple[bool, str]:
    username = str(superset_username or "").strip()
    password = str(superset_password or "")
    if not username or not password:
        return False, "Please enter Superset username and password."

    me_payload: dict[str, Any] = {}
    superset_user_id: int | None = None
    first_dashboard_id: str | None = None
    try:
        sess, _access = _api_session_with_bearer(username, password)
        try:
            me_resp = sess.get(f"{SUPERSET_INTERNAL_URL}/api/v1/me/", timeout=15)
            if me_resp.ok:
                me_payload = me_resp.json() if isinstance(me_resp.json(), dict) else {}
        except Exception:
            me_payload = {}
        try:
            dashboards_resp = sess.get(
                f"{SUPERSET_INTERNAL_URL}/api/v1/dashboard/",
                params={"q": "(page:0,page_size:100)"},
                timeout=20,
            )
            if dashboards_resp.ok:
                dashboards_payload = dashboards_resp.json()
                dashboards = dashboards_payload.get("result") if isinstance(dashboards_payload, dict) else None
                if isinstance(dashboards, list) and dashboards:
                    first_row = dashboards[0] if isinstance(dashboards[0], dict) else {}
                    raw_dashboard_id = first_row.get("id")
                    if raw_dashboard_id is not None:
                        first_dashboard_id = str(raw_dashboard_id)
        except Exception:
            first_dashboard_id = None
    except Exception as exc:
        return False, f"Superset login failed: {exc}"

    me_result = me_payload.get("result") if isinstance(me_payload.get("result"), dict) else {}
    raw_user_id = me_result.get("user_id") or me_result.get("id") or me_payload.get("user_id")
    try:
        superset_user_id = int(raw_user_id) if raw_user_id is not None else None
    except Exception:
        superset_user_id = None

    dashboard_id_for_meta = first_dashboard_id or str(st.session_state.get("dashboard_id") or "").strip()
    dashboard_id_for_meta_int = int(dashboard_id_for_meta) if str(dashboard_id_for_meta).isdigit() else None
    event_payload = {
        "source": "streamlit_login_button",
        "username": username,
        "superset_user_id": superset_user_id,
        "dashboard_id": dashboard_id_for_meta_int,
        "me": me_payload,
    }
    event_req = {
        "session_id": session_id,
        "user_id": username,
        "event_type": "superset_user_identified",
        "payload": event_payload,
    }
    try:
        event_resp = requests.post(
            f"{FASTAPI_INTERNAL_URL.rstrip('/')}/events",
            json=event_req,
            timeout=10,
        )
        if not event_resp.ok:
            return (
                False,
                f"Failed to apply metadata: FastAPI /events {event_resp.status_code}",
            )
    except Exception as exc:
        return False, f"Failed to apply metadata: {exc}"

    st.session_state["superset_meta_user"] = username
    st.session_state["superset_meta_user_id"] = superset_user_id
    st.session_state["superset_meta_bound"] = True
    if first_dashboard_id:
        st.session_state["dashboard_id"] = first_dashboard_id
        return True, f"Login metadata applied: {username} (dashboard_id={first_dashboard_id})"
    return True, f"Login metadata applied: {username}"


def has_recent_dashboard_log(
    *,
    dashboard_id: str,
    user_key: str | None = None,
    session_id: str | None = None,
    action: str | None = None,
) -> bool:
    params: dict[str, Any] = {"limit": 1}
    if dashboard_id:
        params["dashboard_id"] = dashboard_id
    if user_key:
        params["user_key"] = user_key
    if session_id:
        params["session_id"] = session_id
    if action:
        params["action"] = action
    try:
        resp = requests.get(
            f"{FASTAPI_INTERNAL_URL.rstrip('/')}/superset/logs/latest",
            params=params,
            timeout=3,
        )
        if not resp.ok:
            return False
        payload = resp.json()
        return isinstance(payload, list) and len(payload) > 0
    except Exception:
        return False


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
st.session_state.setdefault("dashboard_id", DEFAULT_DASHBOARD_ID)
st.session_state.setdefault("agent_model", "gpt-5-mini")
st.session_state.setdefault("embed_auth_mode", DEFAULT_EMBED_AUTH_MODE)
st.session_state.setdefault("superset_meta_user", "")
st.session_state.setdefault("superset_meta_user_id", None)
st.session_state.setdefault("superset_meta_bound", False)
st.session_state.setdefault("superset_auto_login_once", False)
_ensure_stable_session_id()
if st.session_state.get("agent_model") not in MODEL_OPTIONS:
    st.session_state["agent_model"] = MODEL_OPTIONS[0]
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
    chat_container_id = f"chat-component-{uuid.uuid4().hex}"
    dashboard_id_value = str(st.session_state.get("dashboard_id") or "")
    superset_username_value = str(st.session_state.get("superset_username") or "")
    effective_user_id = superset_username_value or STREAMLIT_USER_ID
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
# active_context UI is temporarily disabled.

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
        key="agent_model",
    )
    DASHBOARD_ID = str(st.session_state.get("dashboard_id", DEFAULT_DASHBOARD_ID)).strip()
    st.text_input("Superset Username", key="superset_username")
    st.text_input("Superset Password", key="superset_password", type="password")
    SUPERSET_USERNAME = str(st.session_state.get("superset_username", DEFAULT_SUPERSET_USERNAME))
    SUPERSET_PASSWORD = str(st.session_state.get("superset_password", DEFAULT_SUPERSET_PASSWORD))
    if st.button("Login & Apply Meta", key="apply_superset_login_btn", use_container_width=True):
        ok, msg = apply_superset_login_meta(
            session_id=str(st.session_state.get("session_id") or ""),
            superset_username=SUPERSET_USERNAME,
            superset_password=SUPERSET_PASSWORD,
        )
        if ok:
            st.session_state["superset_auto_login_once"] = True
            DASHBOARD_ID = str(st.session_state.get("dashboard_id", DEFAULT_DASHBOARD_ID)).strip()
            st.success(msg)
        else:
            st.error(msg)
    EMBED_AUTH_MODE = st.selectbox(
        "Embed Auth Mode",
        options=["session_iframe", "guest_token"],
        index=0
        if st.session_state.get("embed_auth_mode", DEFAULT_EMBED_AUTH_MODE) == "session_iframe"
        else 1,
        key="embed_auth_mode",
        help="session_iframe uses your browser Superset login session (non-guest behavior).",
    )
    if EMBED_AUTH_MODE == "session_iframe":
        st.caption(
            "Session iframe mode: click Login & Apply Meta first, then authenticate on the Superset login page if needed."
        )
        bound_meta_user = str(st.session_state.get("superset_meta_user") or "").strip()
        if bound_meta_user:
            st.caption(f"Bound user: {bound_meta_user}")
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

if EMBED_AUTH_MODE == "session_iframe" and SUPERSET_USERNAME.strip() and SUPERSET_PASSWORD.strip():
    login_url = f"{SUPERSET_PUBLIC_URL}/login/"
    if not DASHBOARD_ID:
        dashboard_iframe_src = login_url
    else:
        dashboard_iframe_src = (
            f"{SUPERSET_PUBLIC_URL}/superset/dashboard/{urllib.parse.quote(str(DASHBOARD_ID))}/"
            "?standalone=1&show_filters=1&expand_filters=0"
            f"&_r={st.session_state['embed_refresh_counter']}"
        )
    should_auto_login = bool(st.session_state.pop("superset_auto_login_once", False))
    iframe_src = login_url if should_auto_login else dashboard_iframe_src
    parsed_superset_url = urllib.parse.urlparse(SUPERSET_PUBLIC_URL)
    superset_origin = (
        f"{parsed_superset_url.scheme}://{parsed_superset_url.netloc}"
        if parsed_superset_url.scheme and parsed_superset_url.netloc
        else SUPERSET_PUBLIC_URL
    )
    session_iframe_id = f"session-iframe-{uuid.uuid4().hex}"
    dashboard_id_for_event = int(DASHBOARD_ID) if DASHBOARD_ID.isdigit() else None
    bound_user_for_events = str(st.session_state.get("superset_meta_user") or "").strip()
    if not bound_user_for_events:
        bound_user_for_events = SUPERSET_USERNAME.strip()
    session_embed_html = f"""
    <div style="width:100%;height:{dashboard_height}px;overflow:hidden;border-radius:12px;">
      <iframe
        id="{session_iframe_id}"
        src={json.dumps(iframe_src)}
        style="width:100%;height:100%;border:0;"
        referrerpolicy="no-referrer-when-downgrade"
        allow="clipboard-read; clipboard-write"
      ></iframe>
    </div>
    <script>
    (() => {{
      const iframe = document.getElementById({json.dumps(session_iframe_id)});
      const eventApiBase = {json.dumps(FASTAPI_PUBLIC_URL.rstrip('/'))};
      const sessionId = {json.dumps(str(st.session_state.get("session_id") or ""))};
      const defaultDashboardId = {json.dumps(dashboard_id_for_event)};
      const supersetOrigin = {json.dumps(superset_origin)};
      const iframeHeight = {json.dumps(dashboard_height)};
      const autoLoginEnabled = {json.dumps(should_auto_login)};
      const loginUrl = {json.dumps(login_url)};
      const postLoginTargetUrl = {json.dumps(dashboard_iframe_src)};
      const autoLoginUsername = {json.dumps(SUPERSET_USERNAME)};
      const autoLoginPassword = {json.dumps(SUPERSET_PASSWORD)};
      let resolvedUser = {json.dumps(bound_user_for_events or None)};
      let currentDashboardId = defaultDashboardId;
      let lastSignature = "";
      let lastSignatureAt = 0;

      const EVENT_TYPES = new Set([
        "superset_tab_click",
        "chart_click",
        "legend_toggle",
        "legend_toggle_activate",
        "legend_toggle_deactivate",
        "cross_filter_added",
        "cross_filter_removed",
        "native_filter_added",
        "native_filter_removed",
        "global_filter_added",
        "global_filter_removed",
        "drill_to_detail",
        "drill_by",
      ]);

      function normalizeEventType(rawType) {{
        if (!rawType || typeof rawType !== "string") return "";
        const text = rawType.trim();
        if (!text) return "";
        const alias = {{
          filter_added: "cross_filter_added",
          filter_removed: "cross_filter_removed",
          global_filter_added: "native_filter_added",
          global_filter_removed: "native_filter_removed",
        }};
        return alias[text] || text;
      }}

      function toInt(value) {{
        if (value === null || value === undefined || value === "") return null;
        const num = Number(value);
        return Number.isFinite(num) ? Math.trunc(num) : null;
      }}

      function pickDashboardId(data, payload) {{
        return (
          toInt(data.dashboard_id) ??
          toInt(data.dashboardId) ??
          toInt(payload.dashboard_id) ??
          toInt(payload.dashboardId) ??
          currentDashboardId
        );
      }}

      function parseDashboardIdFromUrl(urlText) {{
        if (!urlText || typeof urlText !== "string") return null;
        const match = urlText.match(/\\/superset\\/dashboard\\/(\\d+)/) || urlText.match(/\\/dashboard\\/(\\d+)/);
        if (!match) return null;
        return toInt(match[1]);
      }}

      function pickSliceId(data, payload) {{
        return (
          toInt(data.slice_id) ??
          toInt(data.sliceId) ??
          toInt(data.chart_id) ??
          toInt(data.chartId) ??
          toInt(payload.slice_id) ??
          toInt(payload.sliceId) ??
          toInt(payload.chart_id) ??
          toInt(payload.chartId) ??
          null
        );
      }}

      function sanitizePayload(payload) {{
        const out = {{}};
        for (const [key, value] of Object.entries(payload || {{}})) {{
          if (value !== undefined) out[key] = value;
        }}
        return out;
      }}

      function resolveLegendEvent(data, payload, eventType) {{
        if (eventType !== "legend_toggle") {{
          return {{ eventType, legend_name: null, legend_active: null, selected: null }};
        }}
        const clickedNameRaw =
          data.clicked_name ??
          data.clickedName ??
          payload.clicked_name ??
          payload.clickedName ??
          payload.name ??
          data.name ??
          null;
        const clickedName =
          typeof clickedNameRaw === "string" && clickedNameRaw.trim()
            ? clickedNameRaw.trim()
            : null;

        const selectedRaw =
          (payload.selected && typeof payload.selected === "object" && !Array.isArray(payload.selected))
            ? payload.selected
            : (data.selected && typeof data.selected === "object" && !Array.isArray(data.selected))
              ? data.selected
              : null;

        let legendActive = null;
        if (
          clickedName &&
          selectedRaw &&
          Object.prototype.hasOwnProperty.call(selectedRaw, clickedName)
        ) {{
          const raw = selectedRaw[clickedName];
          if (typeof raw === "boolean") legendActive = raw;
          else if (raw === 0 || raw === 1) legendActive = Boolean(raw);
        }}

        const mappedType =
          legendActive === true
            ? "legend_toggle_activate"
            : legendActive === false
              ? "legend_toggle_deactivate"
              : "legend_toggle";
        return {{
          eventType: mappedType,
          legend_name: clickedName,
          legend_active: legendActive,
          selected: selectedRaw,
        }};
      }}

      async function postEvent(eventType, payload) {{
        if (!sessionId || !eventType) return;
        const normalizedPayload = sanitizePayload(payload);
        if (
          (normalizedPayload.dashboard_id === undefined || normalizedPayload.dashboard_id === null) &&
          currentDashboardId !== null
        ) {{
          normalizedPayload.dashboard_id = currentDashboardId;
        }}
        const body = {{
          session_id: sessionId,
          user_id: resolvedUser || null,
          event_type: eventType,
          payload: normalizedPayload,
        }};
        const signature = `${{eventType}}|${{JSON.stringify(body.payload)}}`;
        const now = Date.now();
        if (signature === lastSignature && now - lastSignatureAt < 800) return;
        lastSignature = signature;
        lastSignatureAt = now;
        try {{
          await fetch(`${{eventApiBase}}/events`, {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify(body),
          }});
        }} catch (err) {{
          console.warn("session_iframe event post failed", err);
        }}
      }}

      function normalizeMessageToEvent(rawData) {{
        let data = rawData;
        if (typeof data === "string") {{
          try {{
            data = JSON.parse(data);
          }} catch {{
            return null;
          }}
        }}
        if (!data || typeof data !== "object") return null;
        const payload = data.payload && typeof data.payload === "object" ? data.payload : {{}};
        const eventType = normalizeEventType(
          data.event_type || data.event || payload.event_type || payload.event
        );
        const dashboardId = pickDashboardId(data, payload);
        if (dashboardId !== null) currentDashboardId = dashboardId;
        const sliceId = pickSliceId(data, payload);
        const legendResolved = resolveLegendEvent(data, payload, eventType);
        const filterType =
          payload.filter_type ||
          data.filter_type ||
          (String(eventType).startsWith("native_filter_")
            ? "native"
            : String(eventType).startsWith("cross_filter_")
              ? "cross"
              : null);

        if (data.id === "superset-ui-event" && eventType) {{
          return {{
            eventType: legendResolved.eventType,
            payload: {{
              dashboard_id: dashboardId,
              slice_id: sliceId,
              source_slice_id:
                toInt(data.source_slice_id) ??
                toInt(data.sourceSliceId) ??
                toInt(payload.source_slice_id) ??
                toInt(payload.sourceSliceId) ??
                null,
              tab_id: data.tab_id || data.tabId || payload.tab_id || payload.tabId || null,
              tab_name:
                data.tab_name || data.tabName || payload.tab_name || payload.tabName || null,
              filter: payload.filter || data.filter || null,
              filter_type: filterType,
              chart_name: data.chart_name || data.chartName || payload.chart_name || payload.chartName || null,
              viz_type: data.viz_type || data.vizType || payload.viz_type || payload.vizType || null,
              drill_filters: payload.filters || data.filters || null,
              drill_column: payload.column || data.column || null,
              legend_name: legendResolved.legend_name,
              legend_active: legendResolved.legend_active,
              selected: legendResolved.selected,
            }},
          }};
        }}

        if (eventType && EVENT_TYPES.has(eventType)) {{
          const resolvedType =
            eventType === "legend_toggle" ? legendResolved.eventType : eventType;
          return {{
            eventType: resolvedType,
            payload: {{
              dashboard_id: dashboardId,
              slice_id: sliceId,
              source_slice_id:
                toInt(data.source_slice_id) ??
                toInt(data.sourceSliceId) ??
                toInt(payload.source_slice_id) ??
                toInt(payload.sourceSliceId) ??
                null,
              tab_id: data.tab_id || data.tabId || payload.tab_id || payload.tabId || null,
              tab_name:
                data.tab_name || data.tabName || payload.tab_name || payload.tabName || null,
              filter: payload.filter || data.filter || null,
              filter_type: filterType,
              chart_name: data.chart_name || data.chartName || payload.chart_name || payload.chartName || null,
              viz_type: data.viz_type || data.vizType || payload.viz_type || payload.vizType || null,
              drill_filters: payload.filters || data.filters || null,
              drill_column: payload.column || data.column || null,
              legend_name: legendResolved.legend_name,
              legend_active: legendResolved.legend_active,
              selected: legendResolved.selected,
            }},
          }};
        }}

        const tabId = data.tab_id || data.tabId || data.active_tab_id || data.activeTabId;
        const tabName = data.tab_name || data.tabName || data.active_tab_name || data.activeTabName;
        if (tabId || tabName) {{
          return {{
            eventType: "superset_tab_click",
            payload: {{
              dashboard_id: dashboardId,
              tab_id: tabId || null,
              tab_name: tabName || null,
            }},
          }};
        }}
        return null;
      }}

      function triggerAutoLogin() {{
        if (!autoLoginEnabled || !iframe) return;
        if (!autoLoginUsername || !autoLoginPassword) return;
        try {{
          const form = document.createElement("form");
          form.method = "POST";
          form.action = loginUrl;
          form.target = iframe.id;
          form.style.display = "none";
          const fields = {{
            username: autoLoginUsername,
            password: autoLoginPassword,
            provider: "db",
          }};
          Object.entries(fields).forEach(([name, value]) => {{
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = name;
            input.value = String(value ?? "");
            form.appendChild(input);
          }});
          document.body.appendChild(form);
          form.submit();
          setTimeout(() => {{
            if (postLoginTargetUrl) iframe.src = postLoginTargetUrl;
          }}, 1200);
          setTimeout(() => {{
            if (!postLoginTargetUrl) return;
            const sep = postLoginTargetUrl.includes("?") ? "&" : "?";
            iframe.src = `${{postLoginTargetUrl}}${{sep}}_al=${{Date.now()}}`;
          }}, 2600);
          setTimeout(() => bindParentBridge(), 3200);
          form.remove();
        }} catch (err) {{
          console.warn("session_iframe auto login failed", err);
        }}
      }}

      window.addEventListener("message", (event) => {{
        if (!event || !event.data) return;
        if (supersetOrigin && event.origin && event.origin !== supersetOrigin) return;
        if (iframe && iframe.contentWindow && event.source && event.source !== iframe.contentWindow) return;
        const normalized = normalizeMessageToEvent(event.data);
        if (!normalized) return;
        postEvent(normalized.eventType, normalized.payload);
      }});

      function bindParentBridge() {{
        if (!iframe || !iframe.contentWindow) return;
        try {{
          iframe.contentWindow.postMessage({{ id: "bind-parent" }}, supersetOrigin);
          iframe.contentWindow.postMessage(
            {{
              type: "resize",
              width: iframe.clientWidth || window.innerWidth || null,
              height: iframeHeight || null,
            }},
            supersetOrigin
          );
        }} catch (err) {{
          console.warn("session_iframe bind-parent failed", err);
        }}
      }}

      if (iframe) {{
        iframe.addEventListener("load", () => {{
          const iframeUrl = iframe.getAttribute("src") || iframe.src || "";
          const loadedDashboardId = parseDashboardIdFromUrl(iframeUrl);
          if (loadedDashboardId !== null) {{
            currentDashboardId = loadedDashboardId;
          }}
          if (currentDashboardId !== null) {{
            postEvent("superset_dashboard_loaded", {{
              dashboard_id: currentDashboardId,
              source: "iframe_load",
              url: iframeUrl || null,
            }});
          }}
          bindParentBridge();
          [200, 600, 1200, 2400].forEach((delay) => {{
            setTimeout(() => bindParentBridge(), delay);
          }});
        }});
      }}
      window.addEventListener("resize", () => bindParentBridge());
      triggerAutoLogin();

      (async () => {{
        try {{
          const meRes = await fetch(
            {json.dumps(SUPERSET_PUBLIC_URL + "/api/v1/me/")},
            {{ credentials: "include" }}
          );
          if (!meRes.ok) return;
          const meJson = await meRes.json();
          const result =
            meJson && typeof meJson === "object" && meJson.result && typeof meJson.result === "object"
              ? meJson.result
              : meJson;
          const usernameRaw = result && typeof result === "object" ? result.username : null;
          const supersetUserIdRaw =
            result && typeof result === "object" ? (result.user_id ?? result.id) : null;
          const username =
            typeof usernameRaw === "string" && usernameRaw.trim() ? usernameRaw.trim() : null;
          const supersetUserId = toInt(supersetUserIdRaw);
          if (!username) return;
          resolvedUser = username;
          await postEvent("superset_user_identified", {{
            source: "session_iframe_me_sync",
            username,
            superset_user_id: supersetUserId,
            dashboard_id: currentDashboardId,
            me: meJson,
          }});
        }} catch (err) {{
          console.warn("session_iframe me sync failed", err);
        }}
      }})();
    }})();
    </script>
    """
    components.html(session_embed_html, height=dashboard_height + 8)
elif EMBED_AUTH_MODE == "session_iframe":
    st.info("After entering Superset Username/Password, click `Login & Apply Meta` to show the iframe.")
    components.html(
        f"""
        <div style="
          width:100%;
          height:{dashboard_height}px;
          border:1px dashed #666;
          border-radius:12px;
          display:flex;
          align-items:center;
          justify-content:center;
          color:#bbb;
          background:#111;
          font-family:system-ui, -apple-system, sans-serif;
          font-size:14px;
        ">
          Enter Superset credentials first.
        </div>
        """,
        height=dashboard_height + 8,
    )
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
        user_id=(SUPERSET_USERNAME or STREAMLIT_USER_ID),
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
        active_user_key = str(st.session_state.get("superset_meta_user") or "").strip()
        if not active_user_key:
            active_user_key = SUPERSET_USERNAME.strip()

        if not active_user_key:
            st.info("SQL output appears after `Login & Apply Meta`.")
        elif not DASHBOARD_ID:
            st.info("Select a dashboard first to show SQL output.")
        elif not has_recent_dashboard_log(
            dashboard_id=str(DASHBOARD_ID),
            action="ChartDataRestApi.data",
        ):
            st.info("SQL output appears after dashboard logs are detected.")
        else:
            sql_params = {
                "limit": 50,
                "poll_interval_sec": 1.0,
                "source": "superset",
                "action": "ChartDataRestApi.data",
                "dashboard_id": DASHBOARD_ID,
            }
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
        params = {"limit": 50, "poll_interval_sec": 1.0, "source": source_filter}
        if DASHBOARD_ID:
            params["dashboard_id"] = DASHBOARD_ID
        if superset_user_id.isdigit():
            params["user_id"] = int(superset_user_id)
        active_user_key = str(st.session_state.get("superset_meta_user") or "").strip()
        if not active_user_key:
            active_user_key = SUPERSET_USERNAME.strip()
        if active_user_key:
            params["user_key"] = active_user_key
        current_session_id = str(st.session_state.get("session_id") or "").strip()
        if current_session_id:
            params["session_id"] = current_session_id
            if active_user_key:
                st.caption(
                    f"Log scope: user={active_user_key}, session={current_session_id}"
                )
            else:
                st.caption(f"Log scope: session={current_session_id} (resolved user)")
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

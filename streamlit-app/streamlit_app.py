from __future__ import annotations

import os
import requests
import streamlit as st

from superset_embed_component import superset_embed

SUPERSET_PUBLIC_URL = os.getenv("SUPERSET_PUBLIC_URL", "http://localhost:8088")
SUPERSET_INTERNAL_URL = os.getenv("SUPERSET_INTERNAL_URL", "http://superset_app:8088")

SUPERSET_USERNAME = os.getenv("SUPERSET_USERNAME", "admin")
SUPERSET_PASSWORD = os.getenv("SUPERSET_PASSWORD", "admin")

DASHBOARD_UUID = os.getenv(
    "SUPERSET_DASHBOARD_UUID",
    "9c450c75-5b02-4d29-bc63-dee2fc541337",
)

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
        "resources": [{"type": "dashboard", "id": dashboard_uuid}],
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


st.set_page_config(layout="wide", initial_sidebar_state="expanded")

# --- Sidebar Chat ---
with st.sidebar:
    st.header("Chat")

    if "msgs" not in st.session_state:
        st.session_state.msgs = []

    # put the input in the sidebar too
    prompt = st.chat_input("Ask about what you see…")
    if prompt:
        st.session_state.msgs.append(("user", prompt))
        st.session_state.msgs.append(
            ("assistant", "Tell me which chart/scenario you mean and what you want to analyze.")
        )

    for role, msg in st.session_state.msgs:
        with st.chat_message(role):
            st.write(msg)

# --- Main area: Dashboard ---
st.title("Agent4OLAP Dashboard")

token = get_guest_token(DASHBOARD_UUID)

superset_embed(
    dashboard_id=DASHBOARD_UUID,
    superset_domain=SUPERSET_PUBLIC_URL,
    guest_token=token,
    height=1550,
    key="dash",
)

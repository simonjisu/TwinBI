from __future__ import annotations
import sys 
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
import streamlit as st

from ui.pages.dashboard import app_body

st.set_page_config(
    page_title="Agent4OLAP NLQ Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

app_body()


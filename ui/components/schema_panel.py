from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

import streamlit as st

# Ensure project root is on the path so we can import src.* helpers.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from src.graph_vis import (  # type: ignore  # noqa: E402
    _select_node_labels,
    extract_fact_view,
    make_plotly_figure,
)
from src.schema_processor import SchemaExplorer  # type: ignore  # noqa: E402

DEFAULT_NODE_SPACING: Dict[str, float] = {
    "measure": 1.2,
    "dimension": 1.1,
    "attribute": 0.8,
    "default": 0.9,
}

DEFAULT_NODE_PROPS: Dict[str, Dict[str, int]] = {
    "fontsize": {"fact": 16, "dimension": 14, "default": 14},
}


def render_schema_panel() -> None:
    """Show a fixed tutorial star schema around fact_sales."""
    st.markdown("### Schema graph")
    dataset = "tutorial"
    schema_type = "star"
    fact_table = "fact_sales"

    data_path = PROJECT_ROOT / "data" / dataset
    graph_dict = data_path / f"{dataset}-{schema_type}-graph.json"
    if not graph_dict.exists():
        st.warning(f"Schema graph not found at {graph_dict}")
        return

    try:
        explorer = SchemaExplorer(data_path, schema_type)
        nodes, edges = extract_fact_view(
            explorer.star,
            fact_table,
            explorer.hierarchies,
            node_weights=None,
        )
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not build graph: {exc}")
        return

    prepared_nodes = _select_node_labels(nodes, label_mode="name")
    fig = make_plotly_figure(
        prepared_nodes,
        edges,
        layout="kamada_kawai",
        height="600px",
        width="100%",
        legend_toggles_labels=False,
        node_opacity=1.0,
        node_spacing=DEFAULT_NODE_SPACING,
        node_properties=DEFAULT_NODE_PROPS,
    )
    st.plotly_chart(fig, width="stretch", key="schema-chart-tutorial-star")

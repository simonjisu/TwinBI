# schema_cytoscape.py
from __future__ import annotations

import json
from pathlib import Path
import streamlit as st
from st_cytoscape import cytoscape


@st.cache_data(show_spinner=False)
def load_schema_json(path: str | Path) -> dict:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_elements(schema_block: dict) -> list[dict]:
    """
    schema_block is like:
      {
        "schema_type":"star",
        "fact":"fact_sales",
        "nodes":[...],
        "edges":[...]
      }
    """
    elements: list[dict] = []

    # nodes
    for n in schema_block["nodes"]:
        nid = n["id"]
        ntype = n.get("type", "dimension")  # "fact" or "dimension"
        attrs = n.get("attributes", [])
        elements.append(
            {
                "data": {
                    "id": nid,
                    "label": nid,
                    "kind": "fact" if ntype == "fact" else "dimension",
                    "attributes": ", ".join(attrs) if attrs else "",
                }
            }
        )

    # edges
    for e in schema_block["edges"]:
        src = e["source"]
        tgt = e["target"]
        fk = e.get("fk_field", "")
        pk = e.get("pk_field", "")
        eid = f"{src}->{tgt}:{fk}->{pk}"
        edge_label = f"{fk} → {pk}" if fk or pk else ""
        elements.append(
            {
                "data": {
                    "id": eid,
                    "source": src,
                    "target": tgt,
                    "label": edge_label,
                    "fk_field": fk,
                    "pk_field": pk,
                }
            }
        )

    return elements


def render_schema(elements: list[dict], height: str = "650px", layout_name: str = "breadthfirst", key: str = "schema"):
    stylesheet = [
        # base nodes
        {
            "selector": "node",
            "style": {
                "label": "data(label)",
                "font-size": 12,
                "text-valign": "center",
                "text-halign": "center",
                "text-wrap": "wrap",
                "text-max-width": 160,
                "border-width": 1,
                "border-color": "rgba(255,255,255,0.18)",
            },
        },
        # fact node
        {
            "selector": 'node[kind = "fact"]',
            "style": {
                "shape": "round-rectangle",
                "width": 130,
                "height": 38,
            },
        },
        # dimension nodes
        {
            "selector": 'node[kind = "dimension"]',
            "style": {
                "shape": "ellipse",
                "width": 40,
                "height": 40,
            },
        },
        # edges
        {
            "selector": "edge",
            "style": {
                "width": 2,
                "curve-style": "bezier",
                "target-arrow-shape": "triangle",
                "arrow-scale": 0.9,
                "label": "data(label)",
                "font-size": 10,
                "text-rotation": "autorotate",
                "text-margin-y": -8,
            },
        },
        # selection highlight
        {"selector": ":selected", "style": {"border-width": 4}},
    ]

    layout = {
        "name": layout_name,      # breadthfirst works great for star
        "directed": True,
        "spacingFactor": 1.35,
        "animate": False,
    }

    return cytoscape(
        elements=elements,
        stylesheet=stylesheet,
        layout=layout,
        height=height,
        key=key,
        min_zoom=0.2,
        max_zoom=2.5,
    )

from __future__ import annotations

import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Tuple

from IPython.display import display
import ipywidgets as W

import networkx as nx
import plotly.graph_objects as go
from src.hierarchy_duckdb import HierarchyTree, Node
from src.schema_processor import SchemaExplorer

__all__ = [
    "display_graph",
    "extract_fact_view",
    "make_plotly_figure",
]

_DEFAULT_NODE_WEIGHTS: dict[str, float] = {
    "fact": 1.0,
    "dimension": 1.0,
    "attribute": 1.0,
    "measure": 1.0,
}

_NODE_STYLES: dict[str, dict[str, object]] = {
    "fact": {
        "color": "#fb6a4a",
        "border": "#ef3b2c",
        "symbol": "square",
        "size": 28,
    },
    "dimension": {
        "color": "#9ecae1",
        "border": "#6baed6",
        "symbol": "square",
        "size": 24,
    },
    "attribute": {
        "color": "#fdd47f",
        "border": "#fca735",
        "symbol": "circle",
        "size": 18,
    },
    "measure": {
        "color": "#74c476",
        "border": "#238b45",
        "symbol": "diamond",
        "size": 20,
    },
}

_EDGE_STYLES: dict[str, dict[str, object]] = {
    "schema": {"color": "#9e9e9e", "width": 1.6, "dash": "solid"},
    "hierarchy": {"color": "#bdbdbd", "width": 1.2, "dash": "dot"},
    "measure": {"color": "#74c476", "width": 1.4, "dash": "dot"},
}

if nx is not None:
    _NETWORKX_LAYOUTS: dict[str, Callable[..., dict[str, tuple[float, float]]]] = {
        "spring": nx.spring_layout,
        "kamada_kawai": nx.kamada_kawai_layout,
        "circular": nx.circular_layout,
        "shell": nx.shell_layout,
        "spectral": nx.spectral_layout,
        "spiral": nx.spiral_layout,
        "random": nx.random_layout,
    }
else:  # pragma: no cover - executed only when networkx missing
    _NETWORKX_LAYOUTS = {}

_DEFAULT_LAYOUT_PARAMS: dict[str, dict[str, object]] = {
    "spring": {"seed": 42},
    "random": {"seed": 42},
    "spiral": {"resolution": 0.35},
}

_DEFAULT_NODE_OPACITY = 0.8

if go is not None:
    try:
        _SUPPORTS_TRACE_LEGEND_CALLBACKS = hasattr(go.Scatter(), "on_legendclick")
    except Exception:  # pragma: no cover - defensive in case plotly initialisation fails
        _SUPPORTS_TRACE_LEGEND_CALLBACKS = False
else:  # pragma: no cover - executed only when plotly missing
    _SUPPORTS_TRACE_LEGEND_CALLBACKS = False


def _ensure_widget_dependencies() -> None:
    missing: list[str] = []
    if display is None:
        missing.append("IPython")
    if W is None:
        missing.append("ipywidgets")
    if nx is None:
        missing.append("networkx")
    if go is None:
        missing.append("plotly")
    if missing:
        deps = ", ".join(missing)
        raise RuntimeError(f"graph_vis requires {deps} to render interactive graphs.")


def _resolve_dataset_path(raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if path.is_file():
        path = path.parent

    dataset_names = ("tpcds", "tutorial")
    if path.name in dataset_names and (path / "hierarchy").exists():
        return path

    for candidate in dataset_names:
        candidate_path = path / candidate
        if candidate_path.exists() and (candidate_path / "hierarchy").exists():
            return candidate_path

    raise FileNotFoundError(
        f"Could not locate a dataset directory under {path}. "
        "Expected a subdirectory named 'tpcds' or 'tutorial' with a 'hierarchy/' folder."
    )


def extract_fact_view(
    graph_dict: dict,
    fact_key: str,
    hierarchies: dict[str, HierarchyTree] | None = None,
    node_weights: Mapping[str, float] | None = None,
) -> Tuple[list[dict], list[dict]]:
    if fact_key not in graph_dict:
        raise KeyError(f"{fact_key} not in graph_dict keys: {list(graph_dict.keys())}")

    graph = graph_dict[fact_key]
    node_idx = {node["id"]: node for node in graph.get("nodes", [])}
    weights = dict(_DEFAULT_NODE_WEIGHTS)
    if node_weights:
        weights.update(node_weights)

    def is_fact(node_id: str) -> bool:
        return node_idx[node_id]["type"] == "fact"

    def is_dimension(node_id: str) -> bool:
        return not is_fact(node_id)

    out_edges = defaultdict(list)
    in_edges = defaultdict(list)
    dimension_adj = defaultdict(set)

    for edge in graph.get("edges", []):
        source, target = edge["source"], edge["target"]
        out_edges[source].append(edge)
        in_edges[target].append(edge)
        if source in node_idx and target in node_idx:
            if is_dimension(source) and is_dimension(target):
                dimension_adj[source].add(target)
                dimension_adj[target].add(source)

    keep_nodes = {fact_key}
    frontier = set()

    for edge in out_edges.get(fact_key, []):
        target = edge["target"]
        if target in node_idx and is_dimension(target):
            keep_nodes.add(target)
            frontier.add(target)

    for edge in in_edges.get(fact_key, []):
        source = edge["source"]
        if source in node_idx and is_dimension(source):
            keep_nodes.add(source)
            frontier.add(source)

    to_visit = deque(sorted(frontier))
    visited = set(frontier)
    while to_visit:
        node = to_visit.popleft()
        for neighbor in dimension_adj[node]:
            if neighbor not in visited:
                visited.add(neighbor)
                keep_nodes.add(neighbor)
                to_visit.append(neighbor)

    keep_edges = []
    for edge in graph.get("edges", []):
        source, target = edge["source"], edge["target"]
        if source not in keep_nodes or target not in keep_nodes:
            continue
        if is_fact(source) and source != fact_key:
            continue
        if is_fact(target) and target != fact_key:
            continue
        keep_edges.append(edge)

    cy_nodes = []
    for node_id in sorted(keep_nodes):
        node_def = node_idx[node_id]
        label = node_def.get("label", node_id)
        node_type = node_def["type"]
        cy_nodes.append(
            {
                "data": {
                    "id": node_id,
                    "name": node_def.get("name", node_id),
                    "label": label,
                    "type": node_def["type"],
                    "weight": weights.get(node_type, _DEFAULT_NODE_WEIGHTS["attribute"]),
                }
            }
        )

    cy_edges = []
    for edge in keep_edges:
        cy_edges.append(
            {
                "data": {
                    "id": f"schema:{edge['source']}->{edge['target']}",
                    "source": edge["source"],
                    "target": edge["target"],
                    "label": edge.get("fk_field", ""),
                    "kind": "schema",
                }
            }
        )

    if not hierarchies and not any(
        node_idx[node["data"]["id"]].get("attributes")
        for node in cy_nodes
        if node["data"]["id"] in node_idx and is_dimension(node["data"]["id"])
    ):
        return cy_nodes, cy_edges

    node_ids = {node["data"]["id"] for node in cy_nodes}
    edge_ids = {edge["data"]["id"] for edge in cy_edges}

    def append_node(node_dict: dict) -> None:
        node_id = node_dict["data"]["id"]
        if node_id in node_ids:
            return
        cy_nodes.append(node_dict)
        node_ids.add(node_id)

    def append_edge(edge_dict: dict) -> None:
        edge_id = edge_dict["data"]["id"]
        if edge_id in edge_ids:
            return
        cy_edges.append(edge_dict)
        edge_ids.add(edge_id)

    def build_attr_nodes(
        dimension: str,
        parent_id: str,
        nodes: Iterable[Node],
        prefix: tuple[str, ...] = (),
    ) -> None:
        for node in nodes:
            path = prefix + (node.name,)
            path_fragment = "::".join(path)
            attr_id = f"{dimension}:{path_fragment}"
            label = node.label if node.label else node.name

            append_node(
                {
                    "data": {
                        "id": attr_id,
                        "name": node.name,
                        "label": label,
                        "type": "attribute",
                        "dimension": dimension,
                        "weight": weights.get("attribute", _DEFAULT_NODE_WEIGHTS["attribute"]),
                    }
                }
            )
            append_edge(
                {
                    "data": {
                        "id": f"hierarchy:{parent_id}->{attr_id}",
                        "source": parent_id,
                        "target": attr_id,
                        "label": "",
                        "kind": "hierarchy",
                    }
                }
            )

            if node.children:
                build_attr_nodes(dimension, attr_id, node.children, path)

    def build_measure_nodes(
        fact_id: str,
        nodes: Iterable[Node],
        prefix: tuple[str, ...] = (),
    ) -> None:
        for node in nodes:
            path = prefix + (node.name,)
            path_fragment = "::".join(path)
            if node.children:
                build_measure_nodes(fact_id, node.children, path)
                continue

            measure_id = f"{fact_id}:measure:{path_fragment}"
            label = node.label if node.label else node.name
            append_node(
                {
                    "data": {
                        "id": measure_id,
                        "name": node.name,
                        "label": label,
                        "type": "measure",
                        "fact": fact_id,
                        "agg": node.agg or "",
                        "weight": weights.get("measure", _DEFAULT_NODE_WEIGHTS["measure"]),
                    }
                }
            )
            append_edge(
                {
                    "data": {
                        "id": f"measure:{fact_id}->{measure_id}",
                        "source": fact_id,
                        "target": measure_id,
                        "label": (node.agg or "").upper(),
                        "kind": "measure",
                    }
                }
            )

    for node in cy_nodes:
        node_id = node["data"]["id"]
        if node_id not in node_idx or not is_dimension(node_id):
            continue
        tree = (hierarchies or {}).get(node_id)
        # When a hierarchy tree is present, only render nodes sourced from that tree.
        if tree and tree.table_type == "dim":
            build_attr_nodes(node_id, node_id, tree.children)
            continue
        for attribute in node_idx[node_id].get("attributes", []):
            attr_id = f"{node_id}:{attribute}"
            append_node(
                {
                    "data": {
                        "id": attr_id,
                        "name": attribute,
                        "label": attribute,
                        "type": "attribute",
                        "dimension": node_id,
                        "weight": weights.get("attribute", _DEFAULT_NODE_WEIGHTS["attribute"]),
                    }
                }
            )
            append_edge(
                {
                    "data": {
                        "id": f"hierarchy:{node_id}->{attr_id}",
                        "source": node_id,
                        "target": attr_id,
                        "label": "",
                        "kind": "hierarchy",
                    }
                }
            )

    fact_tree = (hierarchies or {}).get(fact_key)
    if fact_tree and fact_tree.table_type == "fact":
        build_measure_nodes(fact_key, fact_tree.children)

    return cy_nodes, cy_edges


def _select_node_labels(
    nodes: list[dict],
    *,
    label_mode: str,
) -> list[dict]:
    prepared: list[dict] = []
    for node in nodes:
        data = node["data"].copy()
        if label_mode == "label":
            preferred = data.get("label") or data.get("name")
        else:
            preferred = data.get("name") or data.get("label")
        data["label"] = str(preferred or data["id"])
        prepared.append({"data": data})
    return prepared


def _coerce_dimension(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    value_str = str(value).strip().lower()
    if not value_str:
        return None
    if value_str.endswith("px"):
        value_str = value_str[:-2]
    if value_str.isdigit():
        return int(value_str)
    return None


def _resolve_layout(
    layout: str | Mapping[str, object],
    overrides: Mapping[str, object] | None = None,
) -> tuple[str, Callable[..., dict], dict[str, object]]:
    if isinstance(layout, Mapping):
        layout_config = dict(layout)
        layout_name = str(layout_config.pop("name", "")).strip().lower()
        parameters = layout_config
    else:
        layout_name = str(layout).strip().lower()
        parameters = {}

    if overrides:
        overrides = dict(overrides)
        override_name = overrides.pop("name", None)
        if override_name:
            layout_name = str(override_name).strip().lower()
        parameters.update(overrides)

    aliases = {
        "breadthfirst": "shell",
        "tree": "shell",
        "grid": "circular",
        "circle": "circular",
        "concentric": "shell",
        "radial": "shell",
        "layered": "kamada_kawai",
        "cose": "spring",
        "cola": "spring",
        "cose-bilkent": "spring",
        "fcose": "spring",
    }
    layout_key = aliases.get(layout_name, layout_name or "spring")
    if layout_key not in _NETWORKX_LAYOUTS:
        available = ", ".join(sorted(_NETWORKX_LAYOUTS.keys())) or "spring, kamada_kawai, circular, shell, spectral, spiral, random"
        raise ValueError(
            f"Unsupported layout '{layout}'. Available NetworkX layouts: {available}."
        )
    defaults = _DEFAULT_LAYOUT_PARAMS.get(layout_key, {})
    for param, value in defaults.items():
        parameters.setdefault(param, value)
    return layout_key, _NETWORKX_LAYOUTS[layout_key], parameters


def _build_networkx_graph(nodes: dict[str, dict], edges: list[dict]) -> "nx.Graph":
    graph = nx.Graph()
    for node_id, attrs in nodes.items():
        graph.add_node(node_id, **attrs)
    for edge in edges:
        data = edge["data"]
        source = data["source"]
        target = data["target"]
        if source not in nodes or target not in nodes:
            continue
        edge_attrs = {
            key: value
            for key, value in data.items()
            if key not in {"source", "target", "id"}
        }
        graph.add_edge(source, target, **edge_attrs)
    return graph


def _compute_networkx_positions(
    nodes: dict[str, dict],
    edges: list[dict],
    *,
    root_id: str,
    layout: str | Mapping[str, object],
    layout_params: Mapping[str, object] | None = None,
) -> dict[str, tuple[float, float]]:
    if nx is None:
        raise RuntimeError("NetworkX is required to compute graph layouts.")

    layout_key, layout_fn, params = _resolve_layout(layout, layout_params)
    graph = _build_networkx_graph(nodes, edges)

    if layout_key == "shell" and "nlist" not in params:
        shells = [
            [root_id],
            sorted(
                node_id
                for node_id, data in nodes.items()
                if data.get("type") == "dimension" and node_id != root_id
            ),
            sorted(
                node_id
                for node_id, data in nodes.items()
                if data.get("type") == "attribute"
            ),
            sorted(
                node_id
                for node_id, data in nodes.items()
                if data.get("type") == "measure"
            ),
        ]
        params["nlist"] = [shell for shell in shells if shell]

    try:
        raw_positions = layout_fn(graph, **params)
    except TypeError as exc:  # layout called with unsupported parameter
        raise TypeError(
            f"NetworkX layout '{layout_key}' does not accept parameters {params}."
        ) from exc

    positions: dict[str, tuple[float, float]] = {}
    for node_id, coord in raw_positions.items():
        if len(coord) < 2:
            positions[node_id] = (float(coord[0]), 0.0)
        else:
            positions[node_id] = (float(coord[0]), float(coord[1]))

    missing = set(nodes.keys()) - set(positions.keys())
    if missing:
        # Place any missing nodes on a circle around the root.
        radius = max((max(abs(x), abs(y)) for x, y in positions.values()), default=1.0) + 1.0
        angle_step = 2 * math.pi / max(len(missing), 1)
        for idx, node_id in enumerate(sorted(missing)):
            angle = idx * angle_step
            positions[node_id] = (radius * math.cos(angle), radius * math.sin(angle))

    return positions


def _normalize_node_spacing_config(node_spacing: Mapping[str, float] | None) -> dict[str, float]:
    if not node_spacing:
        return {}
    normalized: dict[str, float] = {}
    for node_type, raw_value in node_spacing.items():
        try:
            scale = float(raw_value)
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            raise TypeError("node_spacing values must be numeric.") from exc
        if scale <= 0:
            raise ValueError("node_spacing values must be positive.")
        key = str(node_type).strip().lower()
        if not key:
            continue
        if key in {"*", "default"}:
            key = "default"
        normalized[key] = scale
    return normalized


def _normalize_fontsize_config(
    node_properties: Mapping[str, Mapping[str, object]] | None,
    *,
    fallback: float | int | None,
) -> tuple[float, dict[str, float]]:
    default_size = 12.0 if fallback is None else float(fallback)
    font_mapping: dict[str, float] = {}
    if not node_properties:
        return default_size, font_mapping

    font_section: Mapping[str, object] | None = None
    for key, value in node_properties.items():
        normalized_key = str(key).strip().lower()
        if normalized_key in {"fontsize", "font_size", "textfont", "text_font"} and isinstance(value, Mapping):
            font_section = value
            break
    if not font_section:
        return default_size, font_mapping

    for node_type, raw_value in font_section.items():
        type_key = str(node_type).strip().lower()
        if not type_key:
            continue
        try:
            parsed_value = float(raw_value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise TypeError("Font sizes in node_properties must be numeric.") from exc
        if parsed_value <= 0:
            raise ValueError("Font sizes in node_properties must be positive.")
        if type_key in {"*", "default"}:
            default_size = parsed_value
        else:
            font_mapping[type_key] = parsed_value
    return default_size, font_mapping


def _apply_node_spacing(
    positions: dict[str, tuple[float, float]],
    node_map: Mapping[str, Mapping[str, object]],
    *,
    root_id: str,
    node_spacing: Mapping[str, float] | None,
) -> dict[str, tuple[float, float]]:
    """Push nodes farther/closer to the fact/root based on their types."""
    if not node_spacing:
        return positions
    if root_id not in positions:
        return positions

    adjusted = dict(positions)
    root_x, root_y = positions[root_id]
    default_scale = node_spacing.get("default", 1.0)

    for node_id, data in node_map.items():
        if node_id == root_id:
            continue
        current = positions.get(node_id)
        if current is None:
            continue
        node_type = str(data.get("type", "")).strip().lower() or "dimension"
        scale = node_spacing.get(node_type, default_scale)
        if scale is None or math.isclose(scale, 1.0):
            continue
        if scale <= 0:
            raise ValueError("node_spacing values must be positive.")
        dx = current[0] - root_x
        dy = current[1] - root_y
        if dx == 0 and dy == 0:
            dx = 1e-3
        adjusted[node_id] = (root_x + dx * scale, root_y + dy * scale)
    return adjusted


def _build_plotly_traces(
    nodes: dict[str, dict],
    edges: list[dict],
    positions: dict[str, tuple[float, float]],
    *,
    node_opacity: float = 1.0,
    split_label_traces: bool = False,
    text_font_size: float | int | None = None,
    text_font_size_map: Mapping[str, float] | None = None,
) -> tuple[list[go.Scatter], list[dict]]:
    edge_traces: list[go.Scatter] = []
    annotations: list[dict] = []

    default_font_size = 12 if text_font_size is None else text_font_size
    font_size_map = text_font_size_map or {}

    grouped_edges: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"x": [], "y": []})
    for edge in edges:
        data = edge["data"]
        kind = data.get("kind", "schema")
        source = data["source"]
        target = data["target"]
        if source not in positions or target not in positions:
            continue
        x0, y0 = positions[source]
        x1, y1 = positions[target]
        grouped = grouped_edges[kind]
        grouped["x"].extend([x0, x1, None])
        grouped["y"].extend([y0, y1, None])
        label = data.get("label")
        if label:
            source_type = nodes.get(source, {}).get("type")
            target_type = nodes.get(target, {}).get("type")
            if {source_type, target_type} == {"fact", "dimension"}:
                label = ""
        if label:
            annotations.append(
                {
                    "x": (x0 + x1) / 2,
                    "y": (y0 + y1) / 2,
                    "text": label,
                    "showarrow": False,
                    "font": {"size": 14, "color": "#555"},
                    "bgcolor": "rgba(255, 255, 255, 0.7)",
                    "bordercolor": "rgba(200, 200, 200, 0.5)",
                }
            )

    for kind, coords in grouped_edges.items():
        style = _EDGE_STYLES.get(kind, _EDGE_STYLES["schema"])
        edge_traces.append(
            go.Scatter(
                x=coords["x"],
                y=coords["y"],
                mode="lines",
                line=dict(color=style["color"], width=style["width"], dash=style["dash"]),
                hoverinfo="skip",
                showlegend=False,
            )
        )

    node_groups: dict[str, dict[str, list]] = defaultdict(lambda: {"x": [], "y": [], "text": [], "hover": []})
    for node_id, data in nodes.items():
        if node_id not in positions:
            continue
        node_type = str(data.get("type", "dimension")).lower()
        node_type_label = node_type.title()
        style = _NODE_STYLES.get(node_type, _NODE_STYLES["dimension"])
        x, y = positions[node_id]
        text = str(data.get("label") or data.get("name") or node_id).replace("_", "<br>")
        display_text = text
        if node_type in {"fact", "dimension"}:
            display_text = f"<b>{display_text}</b>"
        hover_lines = [
            f"<b>{text}</b>",
            f"Type: {node_type_label}",
        ]
        if node_type == "attribute" and data.get("dimension"):
            hover_lines.append(f"Dimension: {data['dimension']}")
        if node_type == "measure":
            if data.get("agg"):
                hover_lines.append(f"Aggregation: {data['agg']}")
            if data.get("fact"):
                hover_lines.append(f"Fact: {data['fact']}")
        group = node_groups[node_type]
        group["x"].append(x)
        group["y"].append(y)
        group["text"].append(display_text)
        group["hover"].append("<br>".join(hover_lines))

    node_traces: list[go.Scatter] = []
    marker_traces: list[go.Scatter] = []
    label_traces: list[go.Scatter] = []
    for node_type, coords in node_groups.items():
        style = _NODE_STYLES.get(node_type, _NODE_STYLES["dimension"])
        marker_style = dict(
            symbol=style["symbol"],
            size=style["size"],
            color=style["color"],
            line=dict(color=style["border"], width=2),
            opacity=node_opacity,
        )
        font_size = font_size_map.get(node_type, default_font_size)
        if split_label_traces:
            marker_traces.append(
                go.Scatter(
                    x=coords["x"],
                    y=coords["y"],
                    mode="markers",
                    hoverinfo="text",
                    hovertext=coords["hover"],
                    marker=marker_style,
                    name=node_type.title(),
                    showlegend=False,
                    meta={"node_trace": True, "node_type": node_type},
                )
            )
            legend_marker_style = dict(marker_style)
            legend_marker_style["opacity"] = 0.001
            label_traces.append(
                go.Scatter(
                    x=coords["x"],
                    y=coords["y"],
                    mode="markers+text",
                    text=coords["text"],
                    textposition="top center",
                    textfont=dict(color="#000000", size=font_size),
                    hoverinfo="skip",
                    marker=legend_marker_style,
                    name=node_type.title(),
                    meta={
                        "node_trace": True,
                        "node_type": node_type,
                        "textposition": "top center",
                        "labels_only": True,
                    },
                )
            )
        else:
            node_traces.append(
                go.Scatter(
                    x=coords["x"],
                    y=coords["y"],
                    mode="markers+text",
                    text=coords["text"],
                    textposition="top center",
                    textfont=dict(color="#000000", size=font_size),
                    hoverinfo="text",
                    hovertext=coords["hover"],
                    marker=marker_style,
                    name=node_type.title(),
                    showlegend=True,
                    meta={"node_trace": True, "node_type": node_type, "textposition": "top center"},
                )
            )

    # Render edges first so nodes appear on top.
    if split_label_traces:
        combined_traces = edge_traces + marker_traces + label_traces
    else:
        combined_traces = edge_traces + node_traces
    return combined_traces, annotations


def _attach_legend_label_toggle(figure: go.FigureWidget) -> None:
    """Attach callbacks so legend clicks hide labels without removing nodes."""
    label_states: dict[str, dict[str, Any]] = {}

    def _restore(trace: go.Scatter) -> None:
        state = label_states[trace.uid]
        state["hidden"] = False
        original = state["original_text"]
        trace.text = list(original)
        trace.textposition = state["textposition"]

    for trace in figure.data:
        meta = getattr(trace, "meta", None)
        if not isinstance(meta, dict) or not meta.get("node_trace"):
            continue

        original_text = tuple(trace.text) if trace.text is not None else tuple()
        text_position = meta.get("textposition", getattr(trace, "textposition", "top center"))

        label_states[trace.uid] = {
            "original_text": original_text,
            "hidden": False,
            "textposition": text_position,
        }

        def handle_click(trace: go.Scatter = trace) -> bool:
            state = label_states[trace.uid]
            if state["hidden"]:
                _restore(trace)
            else:
                state["hidden"] = True
                trace.text = [""] * len(state["original_text"])
                trace.textposition = state["textposition"]
            return False

        def handle_double_click(trace: go.Scatter = trace) -> bool:
            for other in figure.data:
                other_meta = getattr(other, "meta", None)
                if not isinstance(other_meta, dict) or not other_meta.get("node_trace"):
                    continue
                _restore(other)
            return False

        trace.on_legendclick(handle_click)
        trace.on_legenddoubleclick(handle_double_click)


def make_plotly_figure(
    nodes: list[dict],
    edges: list[dict],
    *,
    layout: str | Mapping[str, object] = "spring",
    root_id: str | None = None,
    height: str | int | float = "800px",
    width: str | int | float = "100%",
    layout_params: Mapping[str, object] | None = None,
    legend_toggles_labels: bool = False,
    node_opacity: float = _DEFAULT_NODE_OPACITY,
    node_spacing: Mapping[str, float] | None = None,
    node_textfont_size: float | int | None = None,
    node_properties: Mapping[str, Mapping[str, object]] | None = None,
) -> go.FigureWidget:
    if go is None:
        raise RuntimeError("Plotly is required to render graphs. Install the 'plotly' package first.")

    if not nodes:
        raise ValueError("No nodes provided for visualisation.")
    if not (0.0 <= node_opacity <= 1.0):
        raise ValueError("node_opacity must be between 0.0 and 1.0.")
    validated_font_size: float | None = None
    if node_textfont_size is not None:
        try:
            validated_font_size = float(node_textfont_size)
        except (TypeError, ValueError) as exc:
            raise TypeError("node_textfont_size must be numeric.") from exc
        if validated_font_size <= 0:
            raise ValueError("node_textfont_size must be positive.")
    if root_id is None:
        # Choose the fact table if available, otherwise fall back to the first node.
        fact_nodes = [node["data"]["id"] for node in nodes if node["data"].get("type") == "fact"]
        root_id = fact_nodes[0] if fact_nodes else nodes[0]["data"]["id"]

    node_map = {node["data"]["id"]: node["data"] for node in nodes}
    for data in node_map.values():
        data["label"] = str(data.get("label") or data.get("name") or data["id"])

    prepared_edges = []
    for edge in edges:
        data = edge["data"].copy()
        data.setdefault("kind", "schema")
        prepared_edges.append({"data": data})

    spacing_config = _normalize_node_spacing_config(node_spacing)
    if not spacing_config:
        spacing_config = {"measure": 1.4}

    font_default, font_size_map = _normalize_fontsize_config(
        node_properties,
        fallback=validated_font_size,
    )

    positions = _compute_networkx_positions(
        node_map,
        prepared_edges,
        root_id=root_id,
        layout=layout,
        layout_params=layout_params,
    )
    positions = _apply_node_spacing(
        positions,
        node_map,
        root_id=root_id,
        node_spacing=spacing_config or None,
    )

    split_label_traces = legend_toggles_labels and not _SUPPORTS_TRACE_LEGEND_CALLBACKS
    traces, annotations = _build_plotly_traces(
        node_map,
        prepared_edges,
        positions,
        node_opacity=node_opacity,
        split_label_traces=split_label_traces,
        text_font_size=font_default,
        text_font_size_map=font_size_map,
    )
    figure: go.FigureWidget = go.FigureWidget(data=traces)
    figure.update_layout(
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(l=40, r=40, t=40, b=40),
        annotations=annotations,
    )
    figure.update_xaxes(visible=False)
    figure.update_yaxes(visible=False, scaleanchor="x", scaleratio=1)

    height_px = _coerce_dimension(height)
    width_px = _coerce_dimension(width)
    if height_px is not None:
        figure.update_layout(height=height_px)
    if width_px is not None:
        figure.update_layout(width=width_px)

    if legend_toggles_labels and _SUPPORTS_TRACE_LEGEND_CALLBACKS:
        _attach_legend_label_toggle(figure)

    return figure


def display_graph(
    data_path: str | Path,
    *,
    layout: str | Mapping[str, object] = "spring",
    layout_params: Mapping[str, object] | None = None,
    node_weights: Mapping[str, float] | None = None,
    height: str | int | float = "800px",
    width: str | int | float = "100%",
    legend_toggles_labels: bool = False,
    node_opacity: float = _DEFAULT_NODE_OPACITY,
    node_spacing: Mapping[str, float] | None = None,
    node_properties: Mapping[str, Mapping[str, object]] | None = None,
) -> None:
    """
    Layout options mirror the available NetworkX layout algorithms:
    - ``spring`` (default): Fruchterman-Reingold force-directed layout.
    - ``kamada_kawai``: Kamada-Kawai force-directed layout.
    - ``circular``: nodes positioned on a circle.
    - ``shell``: concentric shells (fact, dimensions, attributes, measures).
    - ``spectral``: based on the graph Laplacian eigenvectors.
    - ``spiral``: nodes arranged along an Archimedean spiral.
    - ``random``: random placement with a repeatable seed.
    Legacy Cytoscape names such as ``radial`` or ``cose`` are automatically mapped to the closest NetworkX algorithm.
    Supply extra layout parameters via `layout_params` (for example `layout_params={'k': 0.8}` when using ``spring``),
    or by passing a mapping layout specification such as `layout={'name': 'spring', 'k': 0.8}`.

    The rendered figure height and width can be customised via the `height` and `width` parameters
    (for example `height="600px"` or `height=600`). Percent-based widths fall back to Plotly's auto-sizing.
    Override node weights used by the layout by supplying a mapping via `node_weights`
    (for example `node_weights={'attribute': 0.5, 'measure': 1.5}`).
    Enable legend-driven label hiding by setting `legend_toggles_labels=True` or toggling the UI checkbox.
    Control node opacity via `node_opacity` (between 0 and 1). Pass `node_spacing`
    (e.g. `{'measure': 1.4, 'dimension': 1.1, 'attribute': 1.25, 'default': 0.9}`) for fine-grained control; a `default`
    entry applies to any node type not explicitly mentioned. When omitted, measure nodes default to a spacing of 1.4x.
    Adjust label size via `node_properties={'fontsize': {'measure': 16, 'dimension': 13, 'default': 12}}`
    or fall back to the global `node_textfont_size` provided directly to `make_plotly_figure`.
    """
    _ensure_widget_dependencies()

    dataset_path = _resolve_dataset_path(data_path)
    explorer = SchemaExplorer(dataset_path)

    schema_graphs: dict[str, dict] = {}
    if explorer.star:
        schema_graphs["star"] = explorer.star
    if explorer.snowflake:
        schema_graphs["snowflake"] = explorer.snowflake

    if not schema_graphs:
        raise FileNotFoundError(
            f"No schema graphs found in {dataset_path}. "
            "Expected files '<db_type>-star-graph.json' or '<db_type>-snowflake-graph.json'."
        )

    schema_options = [(name.title(), name) for name in schema_graphs.keys()]
    schema_default = next(iter(schema_graphs.keys()))

    schema_toggle = W.ToggleButtons(
        options=schema_options,
        value=schema_default,
        description="Schema",
        button_style="info",
    )
    fact_dropdown = W.Dropdown(description="Fact", layout=W.Layout(width="250px"))
    label_toggle = W.Dropdown(
        options=[("Name", "name"), ("Label", "label")],
        value="name",
        description="Node Text",
        layout=W.Layout(width="200px"),
    )
    legend_label_checkbox = W.Checkbox(
        value=legend_toggles_labels,
        description="Legend hides node labels",
        indent=False,
        layout=W.Layout(width="220px"),
    )
    output = W.Output()

    def update_fact_options(*_: object) -> None:
        graph_dict = schema_graphs[schema_toggle.value]
        fact_names = sorted(graph_dict.keys())
        fact_dropdown.options = fact_names
        if not fact_names:
            fact_dropdown.value = None
            return
        fact_dropdown.value = fact_dropdown.value if fact_dropdown.value in fact_names else fact_names[0]

    def refresh(*_: object) -> None:
        output.clear_output()
        fact_table = fact_dropdown.value
        if not fact_table:
            return
        graph_dict = schema_graphs[schema_toggle.value]
        nodes, edges = extract_fact_view(
            graph_dict,
            fact_table,
            explorer.hierarchies,
            node_weights=node_weights,
        )

        prepared_nodes = _select_node_labels(nodes, label_mode=label_toggle.value)

        with output:
            display(
                make_plotly_figure(
                    prepared_nodes,
                    edges,
                    layout=layout,
                    layout_params=layout_params,
                    root_id=fact_table,
                    height=height,
                    width=width,
                    legend_toggles_labels=legend_label_checkbox.value,
                    node_opacity=node_opacity,
                    node_spacing=node_spacing,
                    node_properties=node_properties,
                )
            )

    schema_toggle.observe(update_fact_options, "value")
    schema_toggle.observe(refresh, "value")
    fact_dropdown.observe(refresh, "value")
    label_toggle.observe(refresh, "value")
    legend_label_checkbox.observe(refresh, "value")

    update_fact_options()
    refresh()

    controls = W.HBox(
        [schema_toggle, fact_dropdown, label_toggle, legend_label_checkbox],
        layout=W.Layout(gap="10px", align_items="center"),
    )
    header = W.VBox(
        [controls],
        layout=W.Layout(width="100%", align_items="stretch"),
    )
    display(header)
    display(output)

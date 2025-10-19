from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path
from typing import Iterable, Tuple

try:
    from IPython.display import display
except ImportError:  # pragma: no cover - optional dependency
    display = None

try:
    import ipywidgets as W
    from ipycytoscape import CytoscapeWidget
except ImportError:  # pragma: no cover - optional dependency
    W = None
    CytoscapeWidget = None

try:
    from graphviz import Digraph
except ImportError:  # pragma: no cover - optional dependency
    Digraph = None  # type: ignore[misc,assignment]

from hierarchy_duckdb import HierarchyTree, Node
from schema_processor import SchemaExplorer

__all__ = [
    "display_graph",
    "extract_fact_view",
    "make_cyto_widget",
    "export_graph_pdf",
]


def _ensure_widget_dependencies() -> None:
    missing: list[str] = []
    if display is None:
        missing.append("IPython")
    if W is None:
        missing.append("ipywidgets")
    if CytoscapeWidget is None:
        missing.append("ipycytoscape")
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
) -> Tuple[list[dict], list[dict]]:
    if fact_key not in graph_dict:
        raise KeyError(f"{fact_key} not in graph_dict keys: {list(graph_dict.keys())}")

    graph = graph_dict[fact_key]
    node_idx = {node["id"]: node for node in graph.get("nodes", [])}

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
        cy_nodes.append(
            {
                "data": {
                    "id": node_id,
                    "name": node_def.get("name", node_id),
                    "label": label,
                    "type": node_def["type"],
                    "weight": 3 if is_fact(node_id) else 2,
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
                        "weight": 1,
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
                        "weight": 1,
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
                        "weight": 1,
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


def make_cyto_widget(
    nodes: list[dict],
    edges: list[dict],
    *,
    layout: str = "breadthfirst",
    root_id: str | None = None,
) -> CytoscapeWidget:
    for node in nodes:
        node["data"]["label"] = str(node["data"]["label"]).replace("_", "\n")

    cyto = CytoscapeWidget()
    cyto.set_style(
        [
            {
                "selector": "node",
                "style": {
                    "shape": "round-rectangle",
                    "label": "data(label)",
                    "font-size": "11px",
                    "font-weight": "normal",
                    "text-wrap": "wrap",
                    "text-max-width": "90px",
                    "text-valign": "center",
                    "text-halign": "center",
                    "width": "label",
                    "height": "label",
                    "padding": "8px",
                    "background-color": "#9ecae1",
                    "border-width": "2px",
                    "border-color": "#6baed6",
                },
            },
            {
                "selector": "node[type = 'fact']",
                "style": {
                    "background-color": "#fb6a4a",
                    "border-color": "#ef3b2c",
                    "shape": "round-rectangle",
                    "font-weight": "bold",
                },
            },
            {
                "selector": "node[type = 'dimension']",
                "style": {
                    "font-weight": "bold",
                },
            },
            {
                "selector": "node[type = 'attribute']",
                "style": {
                    "shape": "ellipse",
                    "background-color": "#fdd47f",
                    "border-color": "#fca735",
                    "padding": "6px",
                    "font-size": "10px",
                },
            },
            {
                "selector": "node[type = 'measure']",
                "style": {
                    "shape": "ellipse",
                    "background-color": "#74c476",
                    "border-color": "#238b45",
                    "font-size": "10px",
                },
            },
            {
                "selector": "edge",
                "style": {
                    "curve-style": "bezier",
                    "target-arrow-shape": "triangle",
                    "line-color": "#aaa",
                    "target-arrow-color": "#aaa",
                    "width": 1.5,
                    "label": "data(label)",
                    "font-size": "8px",
                    "text-rotation": "autorotate",
                    "text-wrap": "wrap",
                    "text-max-width": "120px",
                    "text-background-color": "#fff",
                    "text-background-opacity": 0.7,
                    "text-background-padding": "2px",
                },
            },
            {
                "selector": "edge[kind = 'hierarchy']",
                "style": {
                    "line-color": "#bdbdbd",
                    "target-arrow-color": "#bdbdbd",
                    "line-style": "dashed",
                    "width": 1.2,
                },
            },
            {
                "selector": "edge[kind = 'measure']",
                "style": {
                    "line-color": "#74c476",
                    "target-arrow-color": "#74c476",
                    "line-style": "dashed",
                },
            },
        ]
    )
    cyto.graph.add_graph_from_json({"nodes": nodes, "edges": edges})

    if layout == "breadthfirst":
        cyto.set_layout(
            name="breadthfirst",
            circle=True,
            spacingFactor=1.15,
            directed=True,
            roots=root_id,
        )
    else:
        cyto.set_layout(name="cose", nodeRepulsion=8000, idealEdgeLength=100)

    cyto.min_zoom = 0.2
    cyto.max_zoom = 3
    cyto.layout.height = "800px"
    cyto.layout.width = "100%"
    return cyto


def display_graph(data_path: str | Path, *, layout: str = "cose") -> None:
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
    label_toggle = W.ToggleButtons(
        options=[("Name", "name"), ("Label", "label")],
        value="name",
        description="Node Text",
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
        nodes, edges = extract_fact_view(graph_dict, fact_table, explorer.hierarchies)

        selected_label = label_toggle.value
        prepared_nodes: list[dict] = []
        for node in nodes:
            data = node["data"].copy()
            node_label = data.get("name") if selected_label == "name" else data.get("label")
            data["label"] = node_label if node_label else data.get("label")
            prepared_nodes.append({"data": data})

        with output:
            display(make_cyto_widget(prepared_nodes, edges, layout=layout, root_id=fact_table))

    schema_toggle.observe(update_fact_options, "value")
    schema_toggle.observe(refresh, "value")
    fact_dropdown.observe(refresh, "value")
    label_toggle.observe(refresh, "value")

    update_fact_options()
    refresh()

    controls = W.HBox(
        [schema_toggle, fact_dropdown, label_toggle],
        layout=W.Layout(gap="10px"),
    )
    legend_html = """
    <div style="display:flex; flex-direction:column; align-items:flex-end; font-size:11px; line-height:1.4;">
      <div style="display:flex; align-items:center; gap:6px; margin-bottom:2px;">
        <span style="width:16px; height:16px; border:2px solid #ef3b2c; background:#fb6a4a; display:inline-block; border-radius:6px;"></span>
        <strong>Fact Table</strong>
      </div>
      <div style="display:flex; align-items:center; gap:6px; margin-bottom:2px;">
        <span style="width:16px; height:16px; border:2px solid #6baed6; background:#9ecae1; display:inline-block; border-radius:6px;"></span>
        <strong>Dimension Table</strong>
      </div>
      <div style="display:flex; align-items:center; gap:6px; margin-bottom:2px;">
        <span style="width:16px; height:16px; border:2px solid #fca735; background:#fdd47f; display:inline-block; border-radius:50%;"></span>
        <span>Attribute</span>
      </div>
      <div style="display:flex; align-items:center; gap:6px;">
        <span style="width:16px; height:16px; border:2px solid #238b45; background:#74c476; display:inline-block; border-radius:6px;"></span>
        <span>Measure</span>
      </div>
    </div>
    """
    legend = W.HTML(
        value=legend_html,
        layout=W.Layout(margin="0 0 0 auto"),
    )
    header = W.HBox(
        [controls, legend],
        layout=W.Layout(width="100%", justify_content="space-between", align_items="flex-start"),
    )
    display(header)
    display(output)


def export_graph_pdf(
    data_path: str | Path,
    fact_table: str,
    *,
    schema: str = "star",
    output_path: str | Path = "schema.pdf",
    label_mode: str = "name",
) -> Path:
    """
    Render the selected fact-view graph to a PDF file.

    This provides a static export path that mirrors the interactive widget. The implementation
    relies on the optional `graphviz` Python package and the Graphviz binaries being available.
    """
    if Digraph is None:  # pragma: no cover - optional dependency
        raise RuntimeError("export_graph_pdf requires the 'graphviz' package to be installed.")

    dataset_path = _resolve_dataset_path(data_path)
    explorer = SchemaExplorer(dataset_path)

    schema_graphs: dict[str, dict] = {}
    if explorer.star:
        schema_graphs["star"] = explorer.star
    if explorer.snowflake:
        schema_graphs["snowflake"] = explorer.snowflake
    if schema not in schema_graphs:
        available = ", ".join(sorted(schema_graphs.keys())) or "none"
        raise ValueError(f"Schema '{schema}' not available. Options: {available}.")

    graph_dict = schema_graphs[schema]
    nodes, edges = extract_fact_view(graph_dict, fact_table, explorer.hierarchies)

    dot = Digraph(comment=f"{fact_table} {schema} schema", format="pdf")
    dot.attr(rankdir="TB")

    def node_label(data: dict) -> str:
        if label_mode == "label":
            return str(data.get("label") or data.get("name") or data["id"])
        return str(data.get("name") or data.get("label") or data["id"])

    for node in nodes:
        data = node["data"]
        node_type = data.get("type", "dimension")
        label = node_label(data)
        style_args = {"style": "filled"}
        if node_type == "fact":
            style_args.update({"shape": "box", "fillcolor": "#fb6a4a", "color": "#ef3b2c"})
        elif node_type == "dimension":
            style_args.update({"shape": "box", "fillcolor": "#9ecae1", "color": "#6baed6"})
        elif node_type == "attribute":
            style_args.update({"shape": "ellipse", "fillcolor": "#fdd47f", "color": "#fca735"})
        elif node_type == "measure":
            style_args.update({"shape": "ellipse", "fillcolor": "#74c476", "color": "#238b45"})
        else:
            style_args.update({"shape": "ellipse", "fillcolor": "#dddddd", "color": "#aaaaaa"})

        dot.node(data["id"], label=label, **style_args)

    for edge in edges:
        data = edge["data"]
        edge_style = {"label": data.get("label", "")}
        kind = data.get("kind")
        if kind == "hierarchy":
            edge_style.update({"style": "dashed", "color": "#bdbdbd"})
        elif kind == "measure":
            edge_style.update({"style": "dashed", "color": "#74c476"})
        else:
            edge_style.update({"color": "#555555"})
        dot.edge(data["source"], data["target"], **edge_style)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = dot.render(
        filename=output_path.stem,
        directory=str(output_path.parent),
        cleanup=True,
    )
    return Path(rendered)

def extract_fact_view(
    graph_dict: dict,
    fact_key: str,
    hierarchies: dict[str, HierarchyTree] | None = None,
):
    if fact_key not in graph_dict:
        raise KeyError(f"{fact_key} not in graph_dict keys: {list(graph_dict.keys())}")
    g = graph_dict[fact_key]

    node_idx = {n["id"]: n for n in g["nodes"]}
    def is_fact(nid): return node_idx[nid]["type"] == "fact"
    def is_dim(nid):  return not is_fact(nid)

    out_edges = defaultdict(list)
    in_edges  = defaultdict(list)
    dim_adj   = defaultdict(set)

    for e in g["edges"]:
        s, t = e["source"], e["target"]
        out_edges[s].append(e)
        in_edges[t].append(e)
        if is_dim(s) and is_dim(t):
            dim_adj[s].add(t)
            dim_adj[t].add(s)

    keep_nodes = {fact_key}
    frontier = set()

    for e in out_edges.get(fact_key, []):
        if is_dim(e["target"]):
            keep_nodes.add(e["target"])
            frontier.add(e["target"])
    for e in in_edges.get(fact_key, []):
        if is_dim(e["source"]):
            keep_nodes.add(e["source"])
            frontier.add(e["source"])

    q = deque(sorted(frontier))
    visited = set(frontier)
    while q:
        u = q.popleft()
        for v in dim_adj[u]:
            if v not in visited:
                visited.add(v)
                keep_nodes.add(v)
                q.append(v)

    keep_edges = []
    for e in g["edges"]:
        s, t = e["source"], e["target"]
        if s in keep_nodes and t in keep_nodes:
            if is_fact(s) and s != fact_key:
                continue
            if is_fact(t) and t != fact_key:
                continue
            keep_edges.append(e)

    cy_nodes = [{
        "data": {
            "id": nid,
            "name": nid,
            "label": nid,
            "type": node_idx[nid]["type"],
            "weight": 3 if is_fact(nid) else 2
        }
    } for nid in keep_nodes]

    cy_edges = [{
        "data": {
            "id": f"schema:{e['source']}->{e['target']}",
            "source": e["source"],
            "target": e["target"],
            "label": e.get("fk_field", ""),
            "kind": "schema",
        }
    } for e in keep_edges]

    if not hierarchies:
        return cy_nodes, cy_edges

    node_ids = {n["data"]["id"] for n in cy_nodes}
    edge_ids = {e["data"]["id"] for e in cy_edges}

    def append_node(node_dict: dict):
        node_id = node_dict["data"]["id"]
        if node_id in node_ids:
            return
        cy_nodes.append(node_dict)
        node_ids.add(node_id)

    def append_edge(edge_dict: dict):
        edge_id = edge_dict["data"]["id"]
        if edge_id in edge_ids:
            return
        cy_edges.append(edge_dict)
        edge_ids.add(edge_id)

    def build_attr_nodes(dimension: str, parent_id: str, nodes: list[Node], prefix: tuple[str, ...] = ()):
        for node in nodes:
            path = prefix + (node.name,)
            path_fragment = "::".join(path)
            attr_id = f"{dimension}:{path_fragment}"
            label = node.label if node.label else node.name
            role = "hierarchy" if node.children else "attribute"
            append_node({
                "data": {
                    "id": attr_id,
                    "name": node.name,
                    "label": label,
                    "type": "attribute",
                    "dimension": dimension,
                    "role": role,
                    "weight": 1,
                }
            })
            append_edge({
                "data": {
                    "id": f"hierarchy:{parent_id}->{attr_id}",
                    "source": parent_id,
                    "target": attr_id,
                    "label": "",
                    "kind": "hierarchy",
                }
            })
            if node.children:
                build_attr_nodes(dimension, attr_id, node.children, path)

    for dimension in sorted(keep_nodes):
        if not is_dim(dimension):
            continue
        tree = hierarchies.get(dimension)
        if not tree:
            continue
        build_attr_nodes(dimension, dimension, tree.children)

    return cy_nodes, cy_edges

def make_cyto_widget(nodes, edges, *, layout="breadthfirst", root_id=None):
    for n in nodes:
        n["data"]["label"] = str(n["data"]["label"]).replace("_", "\n")

    cyto = CytoscapeWidget()
    cyto.set_style([
        {
            "selector": "node",
            "style": {
                "shape": "round-rectangle",
                "label": "data(label)",
                "font-size": "11px",
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
            "selector": "node[type = 'attribute']",
            "style": {
                "shape": "ellipse",
                "background-color": "#c7e9c0",
                "border-color": "#74c476",
                "padding": "6px",
                "font-size": "10px",
            },
        },
        {
            "selector": "node[type = 'attribute'][role = 'hierarchy']",
            "style": {
                "background-color": "#fdd49e",
                "border-color": "#fdbb84",
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
    ])
    cyto.graph.add_graph_from_json({"nodes": nodes, "edges": edges})
    if layout == "breadthfirst":
        cyto.set_layout(name="breadthfirst", circle=True, spacingFactor=1.15, directed=True, roots=root_id)
    else:
        cyto.set_layout(name="cose", nodeRepulsion=8000, idealEdgeLength=100)
    cyto.min_zoom = 0.2
    cyto.max_zoom = 3
    cyto.layout.height = "800px"
    cyto.layout.width = "100%"
    return cyto

def display_graph(data_path: Path):
    data_path = Path(data_path)
    explorer = SchemaExplorer(data_path)
    graph_dir = explorer.graph_dir or resolve_graph_dir(data_path)
    snowflake = explorer.snowflake or {}
    star = explorer.star or {}
    if not snowflake or not star:
        snowflake, star = load_data(graph_dir)

    hierarchies = explorer.hierarchies
    if not hierarchies:
        try:
            hierarchy_dir = resolve_hierarchy_dir(graph_dir, data_path)
            hierarchies = explorer._load_hierarchies(hierarchy_dir)
        except Exception:
            hierarchies = {}
    schema_dd = W.ToggleButtons(options=["star", "snowflake"], 
                                value="star", description="Schema")
    fact_dd   = W.Dropdown(description="Fact")
    label_mode_dd = W.ToggleButtons(options=[("Name", "name"), ("Label", "label")],
                                    value="name", description="Node Text")
    out = W.Output()

    def update_fact_options(*_):
        gdict = snowflake if schema_dd.value == "snowflake" else star
        keys = sorted(gdict.keys())
        fact_dd.options = keys
        if fact_dd.value not in keys:
            fact_dd.value = keys[0] if keys else None

    def refresh(*_):
        out.clear_output()
        with out:
            gdict = snowflake if schema_dd.value == "snowflake" else star
            if not fact_dd.value: return
            nodes, edges = extract_fact_view(gdict, fact_dd.value, hierarchies)

            label_mode = label_mode_dd.value
            prepared_nodes = []
            for node in nodes:
                data = node["data"].copy()
                if label_mode == "name":
                    data["label"] = data.get("name", data.get("label"))
                else:
                    data["label"] = data.get("label")
                prepared_nodes.append({"data": data})

            display(make_cyto_widget(prepared_nodes, edges, layout="cose", root_id=fact_dd.value))

    schema_dd.observe(update_fact_options, "value")
    schema_dd.observe(refresh, "value")
    fact_dd.observe(refresh, "value")
    label_mode_dd.observe(refresh, "value")

    update_fact_options()
    display(W.HBox([schema_dd, fact_dd, label_mode_dd]))
    refresh()
    display(out)
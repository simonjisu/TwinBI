import json
from collections import deque, defaultdict
from pathlib import Path
from typing import Iterable, Iterator, List, Optional

import pandas as pd
from IPython.display import display
from ipycytoscape import CytoscapeWidget
import ipywidgets as W

from hierarchy_duckdb import HierarchyTree, Node
from unique_index import UniqueIndex



FACT_TABLES = {
    "store_sales",
    "store_returns",
    "catalog_sales",
    "catalog_returns",
    "web_sales",
    "web_returns",
    "inventory",
}


def _dedupe_paths(paths: Iterable[Path]) -> Iterator[Path]:
    seen = set()
    for raw in paths:
        candidate = Path(raw)
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        yield candidate


def resolve_graph_dir(data_path: Path) -> Path:
    """Locate the directory that contains the precomputed schema graph JSONs."""
    path = Path(data_path)
    candidates: List[Path] = []
    if path.is_dir():
        candidates.append(path)
        if path.name == "tpcds":
            candidates.append(path.parent)
    else:
        candidates.append(path.parent)
    default_candidate = Path.cwd() / "data"
    candidates.append(default_candidate)

    deduped = list(_dedupe_paths(candidates))
    for candidate in deduped:
        snowflake_path = candidate / "tpcds-snowflake-graph.json"
        star_path = candidate / "tpcds-star-graph.json"
        if snowflake_path.exists() and star_path.exists():
            return candidate

    raise FileNotFoundError(
        f"Could not locate schema graph JSONs relative to {data_path}. Checked: {', '.join(str(c) for c in deduped)}"
    )


def resolve_hierarchy_dir(graph_dir: Path, user_path: Optional[Path] = None) -> Path:
    """Locate the directory holding hierarchy JSON files."""
    candidates: List[Path] = []
    if user_path is not None:
        candidates.append(Path(user_path) / "hierarchy")
    candidates.append(graph_dir / "hierarchy")
    candidates.append(graph_dir.parent / "hierarchy")
    candidates.append(Path.cwd() / "data" / "hierarchy")

    deduped = list(_dedupe_paths(candidates))
    for candidate in deduped:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        f"Could not locate hierarchy JSON files. Checked: {', '.join(str(c) for c in deduped)}"
    )


def _node_from_json_dict(data: dict) -> Node:
    return Node(
        name=data["name"],
        label=data.get("label"),
        stats=data.get("stats"),
        children=[_node_from_json_dict(child) for child in data.get("children", [])],
    )


def hierarchy_from_json(path: Path) -> HierarchyTree:
    payload = json.loads(Path(path).read_text())
    children = [_node_from_json_dict(child) for child in payload.get("children", [])]
    return HierarchyTree(table=payload["table"], children=children)

class SchemaExplorer:
    tpcds_facts = FACT_TABLES

    def __init__(self, data_path: Path | str):
        self.input_path = Path(data_path)
        self.graph_dir: Optional[Path] = None
        self.hierarchy_dir: Optional[Path] = None
        self.project_root: Path = Path.cwd()
        self.snowflake: dict = {}
        self.star: dict = {}
        self.hierarchies: dict[str, HierarchyTree] = {}

        try:
            self.graph_dir = resolve_graph_dir(self.input_path)
            self.project_root = self.graph_dir.parent
            self.hierarchy_dir = resolve_hierarchy_dir(self.graph_dir, self.input_path)
            self.snowflake, self.star = load_data(self.graph_dir)
            self.hierarchies = self._load_hierarchies(self.hierarchy_dir)
        except Exception as e:
            print(
                f"Error initializing SchemaExplorer: {e}\n Make sure to run create_schema_graphs() first: ```python src/schema_processor.py --create_graphs``` "
            )

    def get_facts(self):
        return self.tpcds_facts

    def get_schema(self, schema_type: str, fact_table: str) -> list[dict]:
        """Get the schema of fact and its related dimensions.
        Args:
            schema_type (str): 'snowflake' or 'star'
            fact_table (str): fact table name
        Returns:
            list[dict]: a list of nodes in the schema graph. each node has table name(`id`), type(`fact` or `dimension`).
        """
        gdict = self._get_graph_dict(schema_type)
        
        if fact_table not in gdict:
            raise KeyError(f"{fact_table} not in graph_dict keys: {list(gdict.keys())}")
        
        return list(map(lambda x: {'id': x['id'], 'type': x['type']}, 
                        gdict[fact_table]['nodes']))
    
    def get_attributes(self, schema_type: str, fact_table: str, dimension_table: str) -> list[str]:
        """Get the attributes of a dimension table in the schema of a fact table.
        Args:
            schema_type (str): 'snowflake' or 'star'
            fact_table (str): fact table name
            dimension_table (str): dimension table name
        Returns:
            list[str]: a list of attribute names in the dimension table.
        """
        gdict = self._get_graph_dict(schema_type)
        
        if fact_table not in gdict:
            raise KeyError(f"{fact_table} not in graph_dict keys: {list(gdict.keys())}")
        
        for node in gdict[fact_table]['nodes']:
            if node['id'] == dimension_table and node['type'] == 'dimension':
                return node.get('attributes', [])
        
        raise KeyError(f"{dimension_table} not found as a dimension in the schema of {fact_table}.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_graph_dict(self, schema_type: str) -> dict:
        if schema_type == 'snowflake':
            return self.snowflake
        if schema_type == 'star':
            return self.star
        raise ValueError(f"Invalid schema_type: {schema_type}. Choose 'snowflake' or 'star'.")

    def _load_hierarchies(self, hierarchy_dir: Path) -> dict[str, HierarchyTree]:
        hierarchies: dict[str, HierarchyTree] = {}
        if not hierarchy_dir.exists():
            return hierarchies

        for path in sorted(hierarchy_dir.glob('*.json')):
            try:
                tree = hierarchy_from_json(path)
            except Exception as exc:
                print(f"Failed to load hierarchy from {path}: {exc}")
                continue
            hierarchies[tree.table] = tree
        return hierarchies

    def _iter_dimensions(self, schema_type: str, fact_table: str) -> Iterator[str]:
        gdict = self._get_graph_dict(schema_type)
        if fact_table not in gdict:
            raise KeyError(f"{fact_table} not in graph_dict keys: {list(gdict.keys())}")
        for node in gdict[fact_table]['nodes']:
            if node['type'] == 'dimension':
                yield node['id']

    def _find_attribute_paths(self, tree: HierarchyTree, attribute_name: str) -> List[List[Node]]:
        matches: List[List[Node]] = []

        def dfs(node: Node, path: List[Node]):
            next_path = path + [node]
            if node.name == attribute_name:
                matches.append(next_path)
            for child in node.children:
                dfs(child, next_path)

        for child in tree.children:
            dfs(child, [])

        return matches

    def _build_path_steps(self, tree: HierarchyTree, node_path: List[Node]) -> List[dict]:
        steps: List[dict] = [{
            'type': 'dimension',
            'name': tree.table,
            'label': tree.table,
        }]
        total = len(node_path)
        for idx, node in enumerate(node_path):
            step_type = 'attribute' if idx == total - 1 else 'level'
            step = {
                'type': step_type,
                'name': node.name,
                'label': node.label if node.label else node.name,
            }
            steps.append(step)
        return steps

    def _resolve_index_path(self, raw_path: str | Path) -> Path:
        index_path = Path(raw_path)
        if index_path.is_absolute():
            return index_path
        if self.project_root:
            return (self.project_root / index_path).resolve()
        return (Path.cwd() / index_path).resolve()

    @staticmethod
    def _value_in_list(values: List, target) -> bool:
        if target in values:
            return True
        target_str = str(target)
        return any(str(v) == target_str for v in values)

    # ------------------------------------------------------------------
    # Public search APIs
    # ------------------------------------------------------------------

    def search_attribute(self, schema_type: str, fact_table: str, attribute_name: str) -> List[dict]:
        """Return all hierarchy paths leading to the attribute for the fact schema."""
        results: List[dict] = []

        for dimension in self._iter_dimensions(schema_type, fact_table):
            tree = self.hierarchies.get(dimension)
            if not tree:
                continue
            for node_path in self._find_attribute_paths(tree, attribute_name):
                steps = self._build_path_steps(tree, node_path)
                attr_stats = node_path[-1].stats if node_path else None
                results.append({
                    'dimension': dimension,
                    'attribute': attribute_name,
                    'path': steps,
                    'stats': attr_stats,
                })

        if not results:
            raise KeyError(f"Attribute '{attribute_name}' not found for fact '{fact_table}' in {schema_type} schema.")

        return results

    def search_value(self, schema_type: str, fact_table: str, attribute_name: str, value) -> List[dict]:
        """Return hierarchy paths where the attribute value exists."""
        matches = self.search_attribute(schema_type, fact_table, attribute_name)
        value_matches: List[dict] = []

        for match in matches:
            stats = match.get('stats') or {}
            unique_meta = stats.get('unique_values') if isinstance(stats, dict) else None
            if not unique_meta:
                continue

            utype = unique_meta.get('type')
            found = False

            if utype == 'list':
                found = self._value_in_list(unique_meta.get('values', []), value)
            elif utype == 'bplustree':
                index_path = self._resolve_index_path(unique_meta.get('values'))
                idx = UniqueIndex(str(index_path), fast=False)
                try:
                    found = idx.exists(value)
                finally:
                    idx.close()
            else:
                continue

            if found:
                value_matches.append({
                    'match': match,
                    'value': value,
                    'found': True,
                })

        if not value_matches:
            raise ValueError(
                f"Value '{value}' for attribute '{attribute_name}' not found (fact '{fact_table}', schema '{schema_type}')."
            )

        return value_matches
    

def extract_edges(data_path: Path) -> tuple[dict[str, list], dict[str, dict[str, str]], dict[str, list[str]]]:
    prefix2info: dict[str, dict[str, str]] = {}
    table_attributes: dict[str, list[str]] = {}

    for p in data_path.glob('*.csv'):
        df = pd.read_csv(p)
        if df.empty or 'column' not in df:
            continue

        table_name = p.stem.split('_', 2)[-1].split('_Column_Definitions')[0]
        table_key = table_name.lower()
        first_col = str(df.loc[0, 'column']).split('_')[0]
        prefix2info[first_col] = {'table_name': table_key, 'path': str(p), 'is_fact': is_fact(table_key)}

        if not is_fact(table_key):
            attributes = []
            for raw_col in df['column'].dropna():
                col_name = str(raw_col).strip()
                if not col_name or col_name.lower().endswith('_sk'):
                    continue
                attributes.append(col_name)
            table_attributes[table_key] = attributes

    edges = []
    for p in data_path.glob('*.csv'):
        df = pd.read_csv(p)

        if df.empty or df['Foreign Key'].isnull().all():
            continue

        src_prefix = str(df.loc[0, 'column']).split('_')[0]
        for _, row in df.iterrows():
            src_col = row['column']
            target_col = row['Foreign Key']
            if pd.isna(target_col):
                continue

            target_prefix = str(target_col).split('_')[0]
            edges.append({
                'source_table': prefix2info[src_prefix]['table_name'],
                'source_col': src_col,
                'target_table': prefix2info[target_prefix]['table_name'],
                'target_pk': target_col
            })

    out_edges: dict[str, list] = defaultdict(list)
    for e in edges:
        out_edges[e['source_table']].append(e)

    return out_edges, prefix2info, table_attributes

def is_fact(tbl: str) -> bool:
    return tbl in FACT_TABLES

def build_subgraph(
        out_edges: dict[list], 
        fact_table: str, 
        table_attributes: dict[str, list[str]] | None = None, 
        schema_filter: Optional[list[str]] = None,
        schema_type: str = 'star'):
    nodes = {}  # id -> {'id': name, 'type': 'fact'|'dimension'}
    es = []     # edges for the subgraph

    def ensure_node(name: str, schema_filter: Optional[list[str]] = None):
        if schema_filter and name not in schema_filter:
            return
        if name not in nodes:
            node_type = 'fact' if is_fact(name) else 'dimension'
            node = {'id': name, 'type': node_type}
            if node_type == 'dimension' and table_attributes is not None:
                node['attributes'] = table_attributes.get(name, [])
            nodes[name] = node

    ensure_node(fact_table)

    if schema_type == 'star':
        # 1-hop: fact → dimension
        for e in out_edges.get(fact_table, []):
            ensure_node(e['target_table'])
            es.append({
                'source': e['source_table'],
                'target': e['target_table'],
                'fk_field': e['source_col'],
                'pk_field': e['target_pk'],
            })
    else:
        q = deque([fact_table])
        visited = set([fact_table])
        while q:
            cur = q.popleft()
            for e in out_edges.get(cur, []):
                ensure_node(e['target_table'])
                es.append({
                    'source': e['source_table'],
                    'target': e['target_table'],
                    'fk_field': e['source_col'],
                    'pk_field': e['target_pk'],
                })
                if e['target_table'] not in visited:
                    visited.add(e['target_table'])
                    q.append(e['target_table'])

    node_list = sorted(nodes.values(), key=lambda x: (x['type'] != 'fact', x['id']))
    edge_list = sorted(es, key=lambda x: (x['source'], x['target'], x['fk_field']))

    # attributes = {
    #     node['id']: node.get('attributes', [])
    #     for node in node_list
    #     if node['type'] == 'dimension'
    # }

    return {
        'schema_type': schema_type,
        'fact': fact_table,
        'nodes': node_list,
        'edges': edge_list,
        # 'attributes': attributes,
    }

def create_schema_graphs(data_path: Path):
    # keep only for these schema
    with open(data_path / 'tpcds-schema.json') as f:
        schema: dict[str, list[str]] = json.load(f)

    for schema_type in ['star', 'snowflake']:
        out_edges, prefix2info, table_attributes = extract_edges(data_path / 'tables')
        graph = {}
        for fact in sorted(FACT_TABLES):
            graph[fact] = build_subgraph(
                out_edges, fact, table_attributes, 
                schema_filter=schema.get(fact, []),
                schema_type=schema_type)
            
        with open(data_path / f'tpcds-{schema_type}-graph.json', 'w') as f:
            json.dump(graph, f, indent=2)

        with open(data_path / f'tpcds-prefix_info.json', 'w') as f:
            json.dump(prefix2info, f, indent=2)

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


def load_data(data_path: Path):
    with open(data_path / "tpcds-snowflake-graph.json") as f:
        snowflake = json.load(f)
    with open(data_path / "tpcds-star-graph.json") as f:
        star = json.load(f)
    return snowflake, star


def run_schema_explorer_tests(data_path: Path):
    """Basic smoke tests for SchemaExplorer search utilities."""
    explorer = SchemaExplorer(data_path)

    # Attribute search should locate store dimension hierarchy for store_sales fact.
    attr_results = explorer.search_attribute('star', 'store_sales', 's_store_id')
    store_result = next((res for res in attr_results if res['dimension'] == 'store'), None)
    assert store_result is not None, "Expected to find store dimension for s_store_id"
    assert store_result['path'][-1]['name'] == 's_store_id'

    # Value search against in-memory list metadata.
    assert store_result['stats'], "Expected statistics metadata for s_store_id"
    unique_meta = store_result['stats']['unique_values']
    assert unique_meta['values'], "Unique values list should contain entries"
    sample_value = unique_meta['values'][0]
    value_results = explorer.search_value('star', 'store_sales', 's_store_id', sample_value)
    assert value_results, "Value search should succeed for known list-backed attribute"

    # Attribute/value search for a column backed by a B+Tree unique index.
    cust_results = explorer.search_attribute('star', 'store_sales', 'c_customer_id')
    customer_result = next((res for res in cust_results if res['dimension'] == 'customer'), None)
    assert customer_result is not None, "Expected customer dimension for c_customer_id"
    assert customer_result['stats'], "Expected statistics metadata for c_customer_id"
    customer_meta = customer_result['stats']['unique_values']
    assert customer_meta['type'] == 'bplustree'

    index_path = explorer._resolve_index_path(customer_meta['values'])
    idx = UniqueIndex(str(index_path), fast=False)
    iterator = iter(idx)
    try:
        try:
            sample_customer = next(iterator)
        except StopIteration as exc:
            raise AssertionError('Customer index is unexpectedly empty') from exc
    finally:
        close_iter = getattr(iterator, 'close', None)
        if callable(close_iter):
            close_iter()
        idx.close()

    indexed_results = explorer.search_value('star', 'store_sales', 'c_customer_id', sample_customer)
    assert indexed_results, "Value search should succeed for indexed attribute"

    print("SchemaExplorer tests passed.")

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--data_path', type=str, default='./data/', help='Path to the data directory.')
    parser.add_argument('--create_graphs', action='store_true', help='Flag to create schema graphs.')
    parser.add_argument('--test', action='store_true', help='Run SchemaExplorer search tests.')
    args = parser.parse_args()

    data_path = Path(args.data_path).resolve()
    assert data_path.parent.stem == 'Agent4OLAP', "data_path.parent should be inside 'Agent4OLAP' directory."

    if args.create_graphs:
        # uv run 
        if not data_path.exists():
            data_path.mkdir(parents=True)
        create_schema_graphs(data_path)

    if args.test:
        run_schema_explorer_tests(data_path)

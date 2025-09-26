import json
import pandas as pd
from pathlib import Path
from collections import deque, defaultdict
from IPython.display import display

import json
from collections import defaultdict
from pathlib import Path
from ipycytoscape import CytoscapeWidget
import ipywidgets as W



FACT_TABLES = {
    "store_sales",
    "store_returns",
    "catalog_sales",
    "catalog_returns",
    "web_sales",
    "web_returns",
    "inventory",
}

def extract_edges(data_path: Path) -> tuple[dict[str, list], dict[str, Path]]:
    prefix2info = {}
    for p in data_path.glob('*.csv'):
        table_name = p.stem.split('_', 2)[-1].split('_Column_Definitions')[0]    
        df = pd.read_csv(p)
        prefix = df.loc[0, 'column'].split('_')[0]
        prefix2info[prefix] = {'table_name': table_name.lower(), 'path': str(p)}

    edges = []
    for p in data_path.glob('*.csv'):        
        df = pd.read_csv(p)
        
        if df['Foreign Key'].isnull().all():
            continue
        
        src_prefix = df.loc[0, 'column'].split('_')[0]
        for _, row in df.iterrows():
            src_col = row['column']
            target_col = row['Foreign Key']
            if pd.isna(target_col):
                continue

            target_prefix = target_col.split('_')[0]
            edges.append({
                'source_table': prefix2info[src_prefix]['table_name'],
                'source_col': src_col,
                'target_table': prefix2info[target_prefix]['table_name'],
                'target_pk': target_col
            })

    out_edges = defaultdict(list)
    for e in edges:
        out_edges[e['source_table']].append(e)

    return out_edges, prefix2info

def is_fact(tbl: str) -> bool:
    return tbl in FACT_TABLES

def build_subgraph(out_edges: dict[list], fact_table: str, schema_type: str = 'star'):
    nodes = {}  # id -> {'id': name, 'type': 'fact'|'dimension'}
    es = []     # edges for the subgraph

    def ensure_node(name: str):
        if name not in nodes:
            nodes[name] = {'id': name, 'type': 'fact' if is_fact(name) else 'dimension'}

    ensure_node(fact_table)

    if schema_type == 'star':
        # 1-hop: fact → dimension
        for e in out_edges.get(fact_table, []):
            ensure_node(e['target_table'])
            es.append({
                'source': e['source_table'],
                'target': e['target_table'],
                'relationship': 'many_to_one',
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
                    'relationship': 'many_to_one',
                    'fk_field': e['source_col'],
                    'pk_field': e['target_pk'],
                })
                if e['target_table'] not in visited:
                    visited.add(e['target_table'])
                    q.append(e['target_table'])

    node_list = sorted(nodes.values(), key=lambda x: (x['type'] != 'fact', x['id']))
    edge_list = sorted(es, key=lambda x: (x['source'], x['target'], x['fk_field']))

    return {
        'schema_type': schema_type,
        'fact': fact_table,
        'nodes': node_list,
        'edges': edge_list,
    }

def create_schema_graphs():
    data_path = Path('./data')

    for schema_type in ['star', 'snowflake']:
        out_edges, prefix2info = extract_edges(data_path)
        graph = {}
        for fact in sorted(FACT_TABLES):
            graph[fact] = build_subgraph(out_edges, fact, schema_type=schema_type)
            
        with open(data_path / f'tpcds-{schema_type}-graph.json', 'w') as f:
            json.dump(graph, f, indent=2)

        with open(data_path / f'tpcds-{schema_type}-prefix_info.json', 'w') as f:
            json.dump(prefix2info, f, indent=2)


def extract_fact_view(graph_dict: dict, fact_key: str):
    if fact_key not in graph_dict:
        raise KeyError(f"{fact_key} not in graph_dict keys: {list(graph_dict.keys())}")
    g = graph_dict[fact_key]

    node_idx = {n["id"]: n for n in g["nodes"]}
    def is_fact(nid): return node_idx[nid]["type"] == "fact"
    def is_dim(nid):  return not is_fact(nid)

    # 인접리스트 (dim<->dim은 양방향으로 구성)
    out_edges = defaultdict(list)
    in_edges  = defaultdict(list)
    dim_adj   = defaultdict(set)   # undirected for dimension graph

    for e in g["edges"]:
        s, t = e["source"], e["target"]
        out_edges[s].append(e)
        in_edges[t].append(e)
        # dim-dim 간선이면 양방향로 인접성 추가
        if is_dim(s) and is_dim(t):
            dim_adj[s].add(t)
            dim_adj[t].add(s)

    keep_nodes = {fact_key}
    frontier = set()

    # 1) fact와 직접 연결된 dimension 수집 (양방향 모두 고려)
    for e in out_edges.get(fact_key, []):
        if is_dim(e["target"]):
            keep_nodes.add(e["target"])
            frontier.add(e["target"])
    for e in in_edges.get(fact_key, []):
        if is_dim(e["source"]):
            keep_nodes.add(e["source"])
            frontier.add(e["source"])

    # 2) dimension 그래프를 양방향 BFS로 끝까지 확장
    q = deque(sorted(frontier))
    visited = set(frontier)
    while q:
        u = q.popleft()
        for v in dim_adj[u]:
            if v not in visited:
                visited.add(v)
                keep_nodes.add(v)
                q.append(v)

    # 3) 엣지 선택: (a) fact<->dim, (b) dim<->dim 만.
    #    다른 fact가 한쪽이라도 끼면 제외.
    keep_edges = []
    for e in g["edges"]:
        s, t = e["source"], e["target"]
        if s in keep_nodes and t in keep_nodes:
            if is_fact(s) and s != fact_key:  # 다른 fact 배제
                continue
            if is_fact(t) and t != fact_key:
                continue
            # 선택된 서브그래프 내부의 간선만 유지
            keep_edges.append(e)

    # ipycytoscape용 포맷
    cy_nodes = [{
        "data": {
            "id": nid,
            "label": nid,
            "type": node_idx[nid]["type"],
            "weight": 3 if is_fact(nid) else 2
        }
    } for nid in keep_nodes]

    cy_edges = [{
        "data": {
            "id": f"{e['source']}->{e['target']}",
            "source": e["source"],
            "target": e["target"],
            "label": e.get("fk_field", "")
        }
    } for e in keep_edges]

    return cy_nodes, cy_edges

def make_cyto_widget(nodes, edges, *, layout="breadthfirst", root_id=None):
    # (선택) 언더스코어를 줄바꿈으로 바꿔서 가독성 개선
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
    snowflake, star = load_data(data_path)
    schema_dd = W.ToggleButtons(options=["snowflake", "star"], 
                                value="snowflake", description="Schema")
    fact_dd   = W.Dropdown(description="Fact")
    layout_dd = W.ToggleButtons(options=["cose", "breadthfirst"], 
                                value="cose", description="Layout")
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
            nodes, edges = extract_fact_view(gdict, fact_dd.value)
            display(make_cyto_widget(nodes, edges, layout=layout_dd.value, root_id=fact_dd.value))

    schema_dd.observe(update_fact_options, "value")
    schema_dd.observe(refresh, "value")
    fact_dd.observe(refresh, "value")
    layout_dd.observe(refresh, "value")

    update_fact_options()
    display(W.HBox([schema_dd, fact_dd, layout_dd]))
    refresh()
    display(out)


def load_data(data_path: Path):
    with open(data_path / "tpcds-snowflake-graph.json") as f:
        snowflake = json.load(f)
    with open(data_path / "tpcds-star-graph.json") as f:
        star = json.load(f)
    return snowflake, star

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--data_path', type=str, default='./data/tpcds', help='Path to the data directory containing CSV files.')
    parser.add_argument('--create_graphs', action='store_true', help='Flag to create schema graphs.')
    
    args = parser.parse_args()

    if args.create_graphs:
        create_schema_graphs()
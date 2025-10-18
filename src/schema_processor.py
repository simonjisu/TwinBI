import json
from collections import deque, defaultdict
from pathlib import Path
from typing import Callable, Iterator, List, Optional

import pandas as pd
from IPython.display import display
from ipycytoscape import CytoscapeWidget
import ipywidgets as W
from loguru import logger
from .hierarchy_duckdb import HierarchyTree, Node
from .unique_index import UniqueIndex

TPCDS_FACT_TABLES = {
    "store_sales",
    "store_returns",
    "catalog_sales",
    "catalog_returns",
    "web_sales",
    "web_returns",
    "inventory",
}
TUTORIAL_FACT_TABLES = {
    "fact_sales",
}


def _node_from_json_dict(data: dict) -> Node:
    return Node(
        name=data["name"].lower(),
        label=data.get("label"),
        stats=data.get("stats"),
        children=[_node_from_json_dict(child) for child in data.get("children", [])],
    )

def hierarchy_from_json(path: Path) -> HierarchyTree:
    payload = json.loads(Path(path).read_text())
    children = [_node_from_json_dict(child) for child in payload.get("children", [])]
    return HierarchyTree(table=payload["table"].lower(), children=children)


def load_data(data_path: Path):
    db_type = "tpcds" if "tpcds" in data_path.parts else "tutorial"
    try:
        with open(data_path / f"{db_type}-snowflake-graph.json") as f:
            snowflake = json.load(f)
    except FileNotFoundError:
        snowflake = {}
    try:
        with open(data_path / f"{db_type}-star-graph.json") as f:
            star = json.load(f)
    except FileNotFoundError:
        star = {}
    return snowflake, star


class SchemaExplorer:
    tpcds_facts = TPCDS_FACT_TABLES
    tutorial_facts = TUTORIAL_FACT_TABLES

    def __init__(self, data_path: Path | str):
        """data_path: Path to the database directory, e.g., ./data/tpcds or ./data/tutorial"""
        self.data_path = Path(data_path)
        self.db_type = "tpcds" if "tpcds" in self.data_path.parts else "tutorial"
        self.facts = self.tpcds_facts if self.db_type == "tpcds" else self.tutorial_facts
        self.snowflake, self.star = load_data(self.data_path)
        self.hierarchies = self._load_hierarchies(hierarchy_dir=self.data_path / "hierarchy")

    @classmethod
    def is_fact(cls, table_name: str, db_type: str) -> bool:
        if db_type == "tpcds":
            return table_name in cls.tpcds_facts
        elif db_type == "tutorial":
            return table_name in cls.tutorial_facts
        else:
            raise ValueError(f"Unknown db_type: {db_type}")

    def get_facts(self):
        return self.facts

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
    

def tpcds_extract_edges(tables_path: Path) -> tuple[dict[str, list], dict[str, dict[str, str]], dict[str, list[str]]]:
    db_type = 'tpcds'
    prefix2info: dict[str, dict[str, str]] = {}
    table_attributes: dict[str, list[str]] = {}

    for p in tables_path.glob('*.csv'):
        df = pd.read_csv(p)
        if df.empty or 'column' not in df:
            continue

        table_name = p.stem.split('_', 2)[-1].split('_Column_Definitions')[0]
        table_key = table_name.lower()
        first_col = str(df.loc[0, 'column']).split('_')[0]
        prefix2info[first_col] = {'table_name': table_key, 'path': str(p), 'is_fact': SchemaExplorer.is_fact(table_key, db_type)}

        if not SchemaExplorer.is_fact(table_key, db_type):
            attributes = []
            for raw_col in df['column'].dropna():
                col_name = str(raw_col).strip()
                if not col_name or col_name.lower().endswith('_sk'):
                    continue
                attributes.append(col_name)
            table_attributes[table_key] = attributes

    edges = []
    for p in tables_path.glob('*.csv'):
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

def tutorial_extract_edges(tables_path: Path) -> tuple[dict[str, list], None, dict[str, list[str]]]:
    db_type = 'tutorial'
    prefix2info = None
    table_attributes: dict[str, list[str]] = {}
    edges = []

    for p in tables_path.glob("*.csv"):
        df = pd.read_csv(p)
        src_table = p.stem.split("_", 2)[-1].split("_Column_Definitions")[0].lower()
        if not SchemaExplorer.is_fact(src_table, db_type):
            attributes = []
            for raw_col in df["column"].dropna():
                col_name = str(raw_col).strip()
                if not col_name or col_name.lower().endswith("_sk"):
                    continue
                attributes.append(col_name)
            table_attributes[src_table.lower()] = attributes
        
        if df.empty or "Foreign Key" not in df.columns or df["Foreign Key"].isnull().all():
            continue

        for _, row in df.iterrows():
            src_col = row["column"]
            fk_val = str(row["Foreign Key"]).strip() if not pd.isna(row["Foreign Key"]) else ""
            if not fk_val:
                continue

            # "Dim_Product.product_key" → ("Dim_Product", "product_key")
            if "." in fk_val:
                target_table, target_col = fk_val.split(".", 1)
            else:
                target_table, target_col = None, fk_val

            edges.append({
                "source_table": src_table,
                "source_col": src_col,
                "target_table": target_table.lower() if target_table else "",
                "target_pk": target_col
            })

    out_edges: dict[str, list] = defaultdict(list)
    for e in edges:
        out_edges[e["source_table"]].append(e)

    return out_edges, prefix2info, table_attributes


def build_subgraph(
        db_type: str,
        out_edges: dict[list], 
        fact_table: str, 
        table_attributes: dict[str, list[str]] | None = None, 
        schema_filter: Optional[list[str]] = None,
        schema_type: str = 'star'):
    nodes = {}  # id -> {'id': name, 'type': 'fact'|'dimension'}
    es = []     # edges for the subgraph

    def ensure_node(db_type: str, name: str, schema_filter: Optional[list[str]] = None):
        if schema_filter and name not in schema_filter:
            return
        if name not in nodes:
            node_type = 'fact' if SchemaExplorer.is_fact(name, db_type) else 'dimension'
            node = {'id': name, 'type': node_type}
            if node_type == 'dimension' and table_attributes is not None:
                node['attributes'] = table_attributes.get(name, [])
            nodes[name] = node

    ensure_node(db_type, fact_table)

    if schema_type == 'star':
        # 1-hop: fact → dimension
        for e in out_edges.get(fact_table, []):
            ensure_node(db_type, e['target_table'])
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
    db_type = 'tpcds' if 'tpcds' in data_path.parts else 'tutorial'
    logger.info(f"Creating schema graphs for {db_type} in {data_path}")
    with open(data_path / f'{db_type}-schema.json') as f:
        schema: dict[str, list[str]] = json.load(f)

    schema_types = ['star', 'snowflake'] if db_type == 'tpcds' else ['star']
    extract_edges_func: Callable = tpcds_extract_edges if db_type == 'tpcds' else tutorial_extract_edges
    fact_tables = TPCDS_FACT_TABLES if db_type == 'tpcds' else TUTORIAL_FACT_TABLES
    
    for schema_type in schema_types:
        out_edges, prefix2info, table_attributes = extract_edges_func(data_path / 'tables')
        graph = {}
        for fact in sorted(fact_tables):
            graph[fact] = build_subgraph(
                db_type, out_edges, fact, table_attributes,
                schema_filter=schema.get(fact, []),
                schema_type=schema_type)

        with open(data_path / f'{db_type}-{schema_type}-graph.json', 'w') as f:
            json.dump(graph, f, indent=2)

        if prefix2info:
            with open(data_path / f'{db_type}-prefix_info.json', 'w') as f:
                json.dump(prefix2info, f, indent=2)

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
    parser.add_argument('--db_type', type=str, help='Database type (tpcds or tutorial).')
    parser.add_argument('--create_graphs', action='store_true', help='Flag to create schema graphs.')
    parser.add_argument('--test', action='store_true', help='Run SchemaExplorer search tests.')
    args = parser.parse_args()


    # execution
    execution_path = Path().resolve()
    assert execution_path.stem == 'src', "Please run the script from the 'src' directory."
    data_path = execution_path.parent / 'data' / args.db_type

    if args.create_graphs:
        # uv run schema_processor.py --db_type tutorial --create_graphs
        # uv run schema_processor.py --db_type tpcds --create_graphs
        if not data_path.exists():
            data_path.mkdir(parents=True)
        create_schema_graphs(data_path)

    if args.test:
        run_schema_explorer_tests(data_path)

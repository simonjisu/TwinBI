# Tooling Overview

The `src/` directory collects Python utilities that prepare, introspect, and visualize the DuckDB datasets behind Agent4OLAP’s Cube service.

## Quick Commands

```bash
# Build hierarchy JSON + B+Tree indexes for the requested dataset
$ uv run hierarchy_duckdb.py --db_type "[tutorial|tpcds]"

# Generate schema graphs, star/snowflake JSON, and supporting metadata
$ uv run schema_processor.py --db_type "[tutorial|tpcds]" --create_graphs
```

## Schema Explorer

`schema_processor.SchemaExplorer` is the central API for navigating the generated metadata. It loads the star/snowflake graphs, hierarchy trees, and cached statistics from `data/<dataset>/` and exposes a set of search primitives that agents and notebooks can reuse.

- **Initialization**
  - Point the explorer at a dataset directory such as `Path("data/tutorial")`.
  - The constructor detects whether you are working with the `tutorial` or `tpcds` model and loads the matching fact table catalogue, star/snowflake graphs (`*-star-graph.json`, `*-snowflake-graph.json`), and hierarchy JSON files.
  - Hierarchies are parsed into `HierarchyTree`/`Node` objects so that labels, level order, and attribute statistics are all available in memory.

- **Schema inspection**
  - `get_facts()` lists the supported fact tables for the dataset.
  - `get_schema(schema_type, fact_table)` returns the nodes (fact + dimensions) that participate in the star or snowflake layout for that fact table, including the node type.
  - `get_attributes(schema_type, fact_table, dimension_table)` enumerates the attributes captured for a specific dimension, which is useful when building query builders or UI drop-downs.

- **Hierarchy-aware search**
  - `search_attribute(schema_type, fact_table, attribute_name)` traverses loaded hierarchies to find every path where the attribute appears. It returns the matching dimension, the ordered breadcrumb of hierarchy levels, and any cached statistics (count, distinct count, min/max, unique values).
  - `search_value(schema_type, fact_table, attribute_name, value)` reuses the attribute search results and inspects the per-attribute stats to confirm whether a value exists. For high-cardinality columns it transparently opens the corresponding `UniqueIndex` B+Tree built by `hierarchy_duckdb.py`.

- **Interactive notebooks**
  - The module ships with helper functions that return `ipywidgets`/`ipycytoscape` components for visually exploring the schema graphs inside Jupyter (see the class-level usage in `schema_processor.py`).
  - Displaying the returned widgets immediately renders the star/snowflake network, making it simple to verify joins or debug missing relationships.

- **Recommended workflow**
  1. Generate or refresh metadata with `hierarchy_duckdb.py` and `schema_processor.py`.
  2. Instantiate `SchemaExplorer` in a notebook or service:  
     ```python
     from pathlib import Path
     from schema_processor import SchemaExplorer

     explorer = SchemaExplorer(Path("data/tutorial"))
     explorer.get_schema("star", "fact_sales")
     explorer.search_value("star", "fact_sales", "state", "California")
     ```
  3. Use the explorer’s outputs to drive agent prompts, Cube query builders, or diagnostics.

Together, these utilities keep the Cube semantics, hierarchy stats, and agent-facing metadata synchronized so that higher-level components can focus on planning queries and presenting results.

# Agent4OLAP — Agent Architecture & Project Structure

## Overview
Agent4OLAP is an intelligent agent system for OLAP (Online Analytical Processing). This document outlines the current project layout, agent architecture, and development workflows with a focus on the `cube-project`, `data`, and `src` directories that power the end-to-end analytics experience.

## Project Structure

Note: Omitted any .gitignore-related files for clarity.

```
Agent4OLAP/
├── README.md                 # Project overview and setup
├── AGENTS.md                 # This file — architecture & structure
├── main.py                   # App entry point (CLI or service)
├── pyproject.toml            # Python project config (tooling/deps)
├── uv.lock                   # Locked dependency graph
├── cube-project/             # Cube.js backend (semantic layer + API)
│   ├── create_tpcds_data.py  # Utility to materialize a TPC-DS DuckDB database
│   ├── create_tutorial_data.py # Synthetic tutorial dataset generator
│   ├── cube_conf/            # Cube configuration presets (env specific)
│   ├── data/                 # DuckDB files served by Cube.js
│   ├── docker-compose-*.yml  # Local orchestration helpers
│   ├── package.json          # Cube.js server dependencies (`npm run dev`)
│   └── README.md             # Cube project usage notes
├── data/                     # Canonical data exports shared with agents
│   ├── tutorial/             # Tutorial dataset (DuckDB files + metadata)
│   │   ├── database/         # DuckDB database file(s) served to Cube
│   │   ├── hierarchy/        # Attribute trees powering graph visualizations
│   │   ├── index/            # Generated indexes for selective predicates
│   │   ├── tables/           # Materialized table snapshots
│   │   └── tutorial-star-graph.json
│   └── tpcds/                # TPC-DS dataset with richer star/snowflake graphs
│       ├── database/
│       ├── hierarchy/
│       ├── index/
│       ├── tables/
│       ├── tpcds-star-graph.json
│       └── tpcds-snowflake-graph.json
├── src/                      # Python tooling and agent-support utilities
│   ├── hierarchy_duckdb.py   # Builds OLAP hierarchies & stats from DuckDB
│   ├── schema_processor.py   # Generates schema metadata for agents/Cube
│   ├── unique_index.py       # B+ tree helper for large cardinality columns
│   ├── graph_vis.py          # Renders schema graphs via `display_graph`
│   └── README.md             # Usage examples for the Python helpers
├── lib/                      # Front-end & visualization assets (optional)
├── logs/                     # Runtime/application logs
└── ...                       # Additional notebooks and legacy directories
```

Legacy dbt artifacts under `tpcds/` remain in the repository for reference but are no longer part of the primary development workflow.

Each dataset directory under `data/` mirrors the same layout (database files, hierarchy definitions, indexes, table extracts, and star/snowflake graph JSON). The visualization helpers in `src/graph_vis.py` load these artifacts to drive the interactive `display_graph` experience for both `tutorial` and `tpcds` cubes.

## Agent Architecture

### Core Components

- Agent Framework: Base interfaces, lifecycle hooks (init/start/stop), error handling, and lightweight messaging.
- OLAP Engine: Implements cube operations (slice/dice, roll-up, drill-down), query planning, and aggregation logic that can execute against DuckDB-backed cubes.
- Data Management: Ingestion, validation, schema/catalog management, and storage connectors centered on the top-level `data/` directory.
- Cube Integration (`cube-project`): Hosts the Cube.js server and semantic layer definition exposed to agents over HTTP/REST.

### Agent Types

- Query Agent: Translates user intent into Cube-compatible OLAP queries; optimizes and formats results.
- Data Agent: Loads or generates raw data (via `cube-project` scripts), validates outputs, and materializes assets in `data/`.
- Analytics Agent: Runs advanced analytics over curated cube datasets and derives additional insights.
- Coordination Agent: Schedules tasks, manages inter-agent messaging, and monitors health of the Cube service.

## Agent ↔ Cube Workflow

- Ingest: Generate or refresh DuckDB sources using the scripts in `cube-project/` or the utilities in `src/`.
- Publish: Update Cube configuration under `cube-project/cube_conf/` and ensure schema metadata from `src/schema_processor.py` is synced.
- Serve: Install dependencies (`npm install`) and run `npm run dev` inside `cube-project/` to start the Cube.js service backed by the DuckDB files under `cube-project/data/`.
- Analyze: Agents issue Cube queries, leverage hierarchies generated in `data/<dataset>/hierarchy/`, and post-process results for downstream consumers.

## Development Guidelines

### Getting Started

- Python: Create/activate a virtual environment and install dependencies with `uv sync` or `pip install -e .` as defined in `pyproject.toml`.
- Node/Cube.js: In `cube-project/`, run `npm install` (first time) and `npm run dev` to launch the Cube server.
- Review the helper scripts in `src/` and the dataset generators in `cube-project/` to understand the data preparation pipeline.

### Cube.js Setup (DuckDB driver)

- Add the required environment variables (e.g., `UID`/`GID`) as documented in `cube-project/README.md`.
- Generate or copy DuckDB databases into `cube-project/data/`:
  - `uv run create_tutorial_data.py --output "./data/tutorial/sales.db"`
  - `uv run create_tpcds_data.py` (installs DuckDB TPC-DS extension and materializes `tpcds.db`)
- Launch the Cube server:
  - `npm run dev` (default), or use the provided Docker Compose definitions for containerized runs.

### Adding New Agents

- Implement a class inheriting the base agent interface and lifecycle.
- Document configuration, inputs/outputs, and failure modes, especially how the agent interacts with Cube or the `data/` assets.
- Add unit tests covering the agent’s responsibilities and ensure schema metadata stays in sync.

### Data Management

- Store canonical DuckDB files, indexes, and hierarchy metadata under `data/<dataset>/`.
- Use `src/schema_processor.py` and `src/hierarchy_duckdb.py` to update schema JSON files and hierarchy graphs when source data changes.
- Keep generated indexes (`data/<dataset>/index/`) and graphs current to avoid stale query plans.

### Code Organization

- `src/hierarchy_duckdb.py`: DuckDB introspection and hierarchy builder.
- `src/schema_processor.py`: Schema/metadata serializer consumed by agents and Cube.js.
- `src/unique_index.py`: B+ tree index materialization helper.
- `src/graph_vis.py`: Visualization utilities for schema graphs.

## Future Enhancements

- Multi-agent collaboration protocols and shared state across Cube queries.
- Real-time ingestion/streaming into DuckDB with automatic Cube refresh.
- Visualization hooks for OLAP results leveraging assets in `lib/`.
- Learned query optimization, caching, and pre-aggregation strategies.
- Distributed execution for large-scale Cube builds and data refreshes.

## Contributing

See `README.md` for setup and contribution guidelines. Keep changes focused and well-tested.

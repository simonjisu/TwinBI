# Agent4OLAP — Agent Architecture & Project Structure

## Overview
Agent4OLAP is an intelligent agent system for OLAP (Online Analytical Processing). This document defines the project layout, agent architecture, and development workflows, with special attention to the `tpcds` dbt project integration.

## Project Structure

Note: Omitted any .gitignore-related files for clarity.

```
Agent4OLAP/
├── README.md                 # Project overview and setup
├── AGENTS.md                 # This file — architecture & structure
├── pyproject.toml            # Python project config (tooling/deps)
├── uv.lock                   # Locked dependency graph
├── main.py                   # App entry point (CLI or service)
├── src/                      # Python source
│   ├── agents/               # Agent implementations
│   ├── olap/                 # OLAP-specific logic (cubes, query ops)
│   ├── utils/                # Shared helpers/utilities
│   └── schema_processor.py   # Schema processing helpers
├── tpcds/                    # dbt project directory
│   ├── dbt_project.yml       # dbt project config
│   ├── models/               # Transformations and marts
│   ├── seeds/                # CSV seeds (optional TPC-DS inputs)
│   ├── macros/               # Reusable SQL/Jinja macros
│   └── tests/                # dbt tests (generic + singular)
├── tpcds.db                  # Example DuckDB database (local)
├── data/                     # Data files (outside dbt seeds)
│   ├── raw/                  # Raw ingested data
│   ├── processed/            # Post-ingest/cleaned data
│   └── examples/             # Example datasets
└── logs/                     # Runtime/application logs
```

## Agent Architecture

### Core Components

- Agent Framework: Base interfaces, lifecycle hooks (init/start/stop), error handling, lightweight messaging.
- OLAP Engine: Cube operations (slice/dice, roll-up, drill-down), query planning/optimization, aggregations.
- Data Management: Ingestion, validation, schema/catalog management, storage connectors.
- dbt Integration (tpcds): Orchestrates `dbt seed/run/test`, exposes curated models to agents.

### Agent Types

- Query Agent: Translates user intent into OLAP queries; optimizes and formats results.
- Data Agent: Loads raw data, validates, triggers dbt builds, manages lifecycles.
- Analytics Agent: Runs advanced analytics on curated marts/cubes.
- Coordination Agent: Schedules tasks, manages inter-agent messaging, monitors health.

## Agent ↔ dbt Workflow

- Ingest: Place inputs in `data/raw/` or `tpcds/seeds/`.
- Build: Run dbt in `tpcds/` (`dbt seed`, `dbt run`, `dbt test`).
- Serve: Query curated relations in the target database (e.g., `tpcds.db`).
- Analyze: Feed OLAP/analytics over built models.

## Development Guidelines

### Getting Started

- Python: Create/activate a virtualenv and install deps per `pyproject.toml`.
- dbt: Install dbt with the appropriate adapter (e.g., `dbt-duckdb`).
- Review `tpcds/dbt_project.yml` and `models/` to understand the semantic layer.

### dbt Setup (DuckDB example)

- profiles.yml (typically `~/.dbt/profiles.yml`):

```yaml
tpcds:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: /absolute/path/to/Agent4OLAP/tpcds.db
      threads: 4
```

- Commands (from `tpcds/`):
- `dbt debug`
- `dbt deps`
- `dbt seed`  # if using seeds
- `dbt run`
- `dbt test`

### Adding New Agents

- Implement a class inheriting the base agent interface and lifecycle.
- Document configuration, inputs/outputs, and failure modes.
- Add unit tests covering the agent’s responsibilities.

### Data Management

- Use `data/raw/` for non-dbt raw inputs; `tpcds/seeds/` for dbt seeds.
- Write non-dbt processed artifacts to `data/processed/`.
- Keep small curated examples in `data/examples/`.

### Code Organization

- `src/agents/`: agents and orchestrators.
- `src/olap/`: cubes, aggregations, query helpers.
- `src/utils/`: shared utilities (I/O, logging, config).

## Future Enhancements

- Multi-agent collaboration protocols and shared state.
- Real-time ingestion/streaming.
- Visualization hooks for OLAP results.
- Learned query optimization and caching.
- Distributed execution for large-scale builds.

## Contributing

See `README.md` for setup and contribution guidelines. Keep changes focused and well-tested.


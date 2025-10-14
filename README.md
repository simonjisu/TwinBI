# Agent4OLAP

## How to start

### Prerequisites

```
python3.12
uv
duckdb
docker
docker-compose
```

### Setup

```bash
$ uv venv
$ source .venv/bin/activate
(Agent4OLAP) $ uv sync
(Agent4OLAP) $ cd cube-project
(Agent4OLAP) $ echo -e "UID=$UID\nGID=$GID\nCUBEJS_TESSERACT_SQL_PLANNER=true" > .env
(Agent4OLAP) $ uv run ./create_tpcds_data.py
(Agent4OLAP) $ docker-compose up -d
```



Agent for OLAP

```
uv run src/schema_processor.py --create_graphs
uv run src/hierarchy_duckdb.py
```


```
DBT_PROFILES_DIR=./.dbt dbt debug
DBT_PROFILES_DIR=./.dbt dbt ls --resource-type source
DBT_PROFILES_DIR=./.dbt dbt build
```

# TODO: 

* `SchemaExplorer` in `src/schema_processor.py`: need from/target searching
* Due to snowflake schema, need to create multiple view tables that joined with each others...
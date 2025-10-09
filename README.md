# Agent4OLAP

Agent for OLAP

```
uv run src/schema_processor.py --create_graphs
uv run src/hierarchy_duckdb.py
```


```
uv add dbt-core dbt-duckdb
npm install -g cubejs-cli
```

```
DBT_PROFILES_DIR=./.dbt dbt debug
DBT_PROFILES_DIR=./.dbt dbt ls --resource-type source
DBT_PROFILES_DIR=./.dbt dbt build
```


## Cube.js
```shell
cd cube-project
docker network ls # check the host
docker compose up -d
```

# TODO: 

* `SchemaExplorer` in `src/schema_processor.py`: need from/target searching
* Due to snowflake schema, need to create multiple view tables that joined with each others...
# Agent4OLAP

Agent for OLAP


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


## TODO


```python
con = duckdb.connect(database='./tpcds/tpcds.db')
con.execute('INSTALL tpcds;')
con.execute('LOAD tpcds;')
```
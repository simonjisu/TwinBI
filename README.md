# TwinBI 🐝

## How to start

### Prerequisites

```
python3.12
uv
duckdb
docker
docker-compose
```

### Setup - Create Network

```bash
$ docker network create agent4olap_net
```

### Setup - Sales DB + Cube.js

```bash
$ uv venv
$ source .venv/bin/activate
(Agent4OLAP) $ uv sync
(Agent4OLAP) $ echo -e "UID=$UID\nGID=$GID\nCUBEJS_TESSERACT_SQL_PLANNER=true" > .env
(Agent4OLAP) $ uv run src/create_db_data.py --type sales --rows 5000 --db_path "data/sales/database/sales.db"
```

### Setup - Applications

Clone and setup Superset:

```bash
(Agent4OLAP) $ git clone --depth=1 https://github.com/apache/superset.git superset
(Agent4OLAP) $ cd superset && git fetch --tags --depth=1
(Agent4OLAP) superset $ git checkout 6.0.0
(Agent4OLAP) superset $ set TAG=6.0.0-dev
```

Modify `superset_config.py`:

```python
# add the following lines to superset/docker/pythonpath_dev/superset_config.py
FEATURE_FLAGS = {"ALERT_REPORTS": True, "DRILL_BY": True, "EMBEDDED_SUPERSET": True}

TALISMAN_ENABLED = False
GUEST_TOKEN_JWT_AUDIENCE = "superset"
GUEST_TOKEN_HEADER_NAME = "X-GuestToken"
GUEST_ROLE_NAME= "Gamma"
GUEST_TOKEN_JWT_ALGO = "HS256"
GUEST_TOKEN_JWT_SECRET = "ifJCllwMr-7vPky1kysSn1qRWjgYVKt-SAZt2edE9je5fob5MKKp0yWZJ0o41h2nAjpyjAC6vH30g_qL4iCBEA"  # changable
GUEST_TOKEN_JWT_EXP_SECONDS = 3600  # 1 hour
OVERRIDE_HTTP_HEADERS = {
    "X-Frame-Options": "ALLOWALL",
}
```

Start superset:

```bash
(Agent4OLAP) superset $ cp ../docker-compose.superset.yml ./docker-compose.superset.yml
(Agent4OLAP) superset $ docker-compose -f ./docker-compose.superset.yml up -d
```

Create the sales.db database in superset:

```bash
(Agent4OLAP) $ uv run src/create_db_data.py --type sales --rows 5000 --db_path "data/sales/database/sales.db"
(Agent4OLAP) $ docker-compose -f ./docker-compose.sales.yml -d --build

# Create Cube schema graphs
(Agent4OLAP) $ uv run src/schema_processor.py --create_graphs
(Agent4OLAP) $ uv run src/hierarchy_duckdb.py --db_type sales
```

In the superset web UI (`http://localhost:8088`), create a new database connection to connect to the sales DB created above. 

```
Host: host.docker.internal
Port: 35432
Database name: sales
Username: admin
Password: admin
Name: Sales-DuckDB
```

Then create a dashboard, and enable embedding for the dashboard:

```
In the dashboard, click '...' --> Embed Dashboard --> Enable Dashboard Embedding --> check the uuid value
```

At the same time, using admin account to edit the role 'Gamma' to add all permissions.

```
can grant guest token SecurityRestApi
can read Log
can write Log
can get or create Dataset
can write Dataset
can warm up cache Dataset
can samples on Datasource
+ all roles related to [Sales-DuckDB]
```

Now, we need to set the following variables in `docker-compose.streamlit.yml`:

```bash
# .env file settings: in fastapi
OPENAI_API_KEY=your_openai_api_key
```

Run the FastAPI backend:

```bash
(Agent4OLAP) $ docker-compose -f ./docker-compose.fastapi.yml up -d --build
```

Finally, run the streamlit app:

```bash
(Agent4OLAP) $ docker-compose -f ./docker-compose.streamlit.yml up -d --build
```

```bash
# Run in one line
docker compose -f docker-compose.sales.yml -f docker-compose.streamlit.yml -f docker-compose.fastapi.yml up -d --build
```
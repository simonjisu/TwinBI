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
(TwinBI) $ uv sync
(TwinBI) $ echo -e "UID=$UID\nGID=$GID\nCUBEJS_TESSERACT_SQL_PLANNER=true" > .env
(TwinBI) $ uv run src/create_db_data.py --type sales --rows 5000 --db_path "data/sales/database/sales.db"
```

### Setup - Applications

Clone and setup Superset:

```bash
(TwinBI) $ git clone --depth=1 https://github.com/apache/superset.git superset
(TwinBI) $ cd superset && git fetch --tags --depth=1
(TwinBI) superset $ git checkout 6.0.0
(TwinBI) superset $ set TAG=6.0.0-dev
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
(TwinBI) superset $ cp ../docker-compose.superset.yml ./docker-compose.superset.yml
(TwinBI) superset $ docker-compose -f ./docker-compose.superset.yml up -d
```

Create the sales.db database in superset:

```bash
(TwinBI) $ uv run src/create_db_data.py --type sales --rows 5000 --db_path "data/sales/database/sales.db"
(TwinBI) $ docker-compose -f ./docker-compose.sales.yml -d --build

# Create Cube schema graphs
(TwinBI) $ uv run src/schema_processor.py --create_graphs
(TwinBI) $ uv run src/hierarchy_duckdb.py --db_type sales
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
(TwinBI) $ docker-compose -f ./docker-compose.fastapi.yml up -d --build
```

Finally, run the streamlit app:

```bash
(TwinBI) $ docker-compose -f ./docker-compose.streamlit.yml up -d --build
```

```bash
# Run in one line
docker compose -f docker-compose.sales.yml -f docker-compose.streamlit.yml -f docker-compose.fastapi.yml up -d --build
```

## How to?

### Apply superset-frontend changes

```bash
# build frontend assets
(TwinBI) superset/superset-frontend $ cd superset/superset-frontend
(TwinBI) superset/superset-frontend $ npm ci
(TwinBI) superset/superset-frontend $ npm run build

# then rebuild the Docker image and restart
(TwinBI) superset $ cd ..
(TwinBI) superset $ docker compose -f docker-compose.superset.yml up -d --build
```

### Reset logs in superset

```bash
docker exec -it superset_db psql -U superset -d superset
```

```sql
-- check the table is `logs` with \dt
TRUNCATE TABLE logs RESTART IDENTITY;
```


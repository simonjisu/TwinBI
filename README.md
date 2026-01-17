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

### Setup

```bash
(Agent4OLAP) $ git clone --depth=1 https://github.com/apache/superset.git superset
(Agent4OLAP) $ cd superset && git fetch --tags --depth=1
(Agent4OLAP) superset $ git checkout 6.0.0
(Agent4OLAP) superset $ set TAG=6.0.0-dev
```

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

```bash
(Agent4OLAP) superset $ cp ../docker-compose.superset.yml ./docker-compose.superset.yml
(Agent4OLAP) superset $ docker-compose -f ./docker-compose.superset.yml up -d
```

Create the sales.db database in superset:

```bash
(Agent4OLAP) $ uv run src/create_db_data.py --type sales --rows 5000 --db_path "data/sales/database/sales.db"
(Agent4OLAP) $ docker-compose -f ./docker-compose.sales.yml -d --build
```

In the superset web UI, create a new database connection to connect to the sales DB created above. 

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
can warmup cache Dataset
catalog access [Sales-DuckDB]

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

# Cube DB Test

```
PGPASSWORD=admin psql -h localhost -p 35432 -U admin sales
```

### After login to superset

Click the top-right `+` icon -> Settings -> Database -> + Database
```
Host: host.docker.internal
Port: 35432
Database name: cube (아무거나 OK)
Username: admin
Password: admin
```

### Create Schema Graphs

```bash
(Agent4OLAP) $ uv run src/schema_processor.py --create_graphs
(Agent4OLAP) $ uv run src/hierarchy_duckdb.py --db_type sales
```

# (Optional) Superset create new user

```bash
docker compose exec superset superset fab create-user \
  --username user1 \
  --firstname User \
  --lastname One \
  --email user1@example.com \
  --password mypassword \
  --role Gamma
```
--role에는 기본 제공 권한 중 하나 지정

* Admin
* Alpha (데이터 생성/편집 권한 O, 시스템 설정 X)
* Gamma (읽기만 가능)
* Public (로그인 없이 접근 가능한 권한)


## NLQ Dashboard (Streamlit + FastAPI)

Launch the backend and UI locally:

```bash
# terminal 1: FastAPI backend
uvicorn backend.main:app --reload --port 8000

# terminal 2: Streamlit UI
streamlit run ui/app.py
```

The UI expects the backend at `http://localhost:8000` by default; override with `export BACKEND_URL=http://<host>:<port>`.

debug

```bash
cd streamlit-app/superset_embed_component
npm install
npm run build
```

```
./get_guest_tokens.sh http://localhost:8088 admin admin 12
```

```
cd streamlit-app/superset_embed_component
npm install
npm run build
```

https://medium.com/@vishalsadriya1224/embedding-apache-superset-dashboards-in-ruby-on-rails-and-react-a-role-level-security-guide-697da01676af


## Archive


Copy the UUID to `SUPERSET_EMBED_UUID` in `docker-compose.streamlit.yml`.

The dashboard id can be found in the URL when viewing the dashboard, e.g., `http://localhost:8088/superset/dashboard/12/` -> dashboard id is `12`.


The dashboard uuid can be found by running the `get_superset_uuid.sh` script:

```bash
# come back to the Agent4OLAP root dir
(Agent4OLAP) $ chmod +x ./get_superset_uuid.sh
(Agent4OLAP) $ ./get_superset_uuid.sh http://localhost:8088 admin admin 12
# Superset: http://localhost:8088
# User: admin
# Dashboard ID: 12
# 
# [dashboard uuid]
```
copy the DASHBOARD UUID and set it to `SUPERSET_DASHBOARD_UUID` in `docker-compose.streamlit.yml`

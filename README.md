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

### Setup - Superset

```bash
(Agent4OLAP) $ git clone --depth=1  https://github.com/apache/superset.git
```

add these line to `superset/docker/.env-local` file

issue JWC token for guest access
```bash
(Agent4OLAP) $ openssl rand -base64 48
# [some random string]
```

```bash
(Agent4OLAP) $ cp superset_config_overrides.py superset/docker/pythonpath_dev/superset_config_overrides.py
```

```python
# add the following lines to superset/docker/pythonpath_dev/superset_config.py

FEATURE_FLAGS = {
    "ALERT_REPORTS": True,
    "DRILL_BY": True,
    "EMBEDDABLE_CHARTS": True,
    "EMBEDDED_SUPERSET": True,
    "DASHBOARD_CROSS_FILTERS": True,
}
GUEST_TOKEN_JWT_SECRET = os.environ["GUEST_TOKEN_JWT_SECRET"]
GUEST_TOKEN_JWT_ALGO = "HS256"

TALISMAN_ENABLED = True
TALISMAN_CONFIG = {
    "content_security_policy": {
        "frame-ancestors": ["http://localhost:8501", "http://127.0.0.1:8501"]
    }
}

# GUEST_TOKEN_HEADER_NAME = "X-GuestToken"
# GUEST_TOKEN_JWT_AUDIENCE = "superset"
X_FRAME_OPTIONS = "ALLOWALL"
# GUEST_ROLE_NAME = "Gamma"
```

```bash
(Agent4OLAP) $ chmod +x ./get_superset_uuid.sh
(Agent4OLAP) $ ./get_superset_uuid.sh http://localhost:8088 admin admin 12
# Superset: http://localhost:8088
# User: admin
# Dashboard ID: 12
# 
# [dashboard uuid]
```

copy the DASHBOARD UUID and set it to `SUPERSET_DASHBOARD_UUID` in `docker-compose.streamlit.yml`

In the superset web UI, create a new database connection to connect to the sales DB created above.

```
In the dashboard, click '...' --> Embed Dashboard --> Enable Dashboard Embedding --> check the uuid value
```

copy the UUID to `SUPERSET_EMBED_UUID` in `docker-compose.streamlit.yml`

Put the `http://localhost:8501,http://127.0.0.1:8501` in the `Settings > Allowed Domains (comma separated) ` field.

### Run - Docker Compose

```bash
(Agent4OLAP) $ docker-compose -f ./docker-compose.sales.yml -f ./docker-compose.streamlit.yml -f ./docker-compose.superset.yml up -d
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
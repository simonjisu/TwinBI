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
(Agent4OLAP) $ uv run ./create_tutorial_data.py
# create & start container
(Agent4OLAP) $ docker compose -f docker-compose-tutorial.yml up -d  
# stop & remove container
(Agent4OLAP) $ docker compose -f docker-compose-tutorial.yml down
```

### Setup - Superset

```bash
(Agent4OLAP) $ git clone --depth=1  https://github.com/apache/superset.git
(Agent4OLAP) $ cp cube-project/docker-compose-superset.yml superset/docker-compose-superset.yml
(Agent4OLAP) $ cd superset
# create & start container
(Agent4OLAP) $ docker compose -f docker-compose-superset.yml up -d
# stop & remove container
(Agent4OLAP) $ docker compose -f docker-compose-superset.yml down
```

# Cube Test

```
PGPASSWORD=1234 psql -h localhost -p 35432 -U user1 tutorial
```

# Superset 사용자 생성 방법

```bash
docker compose exec superset-light superset fab create-user \
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

### Create Schema Graphs

```
uv run src/schema_processor.py --create_graphs
cd src
uv run hierarchy_duckdb.py --db_type tutorial
```


### DBT Commands

```
DBT_PROFILES_DIR=./.dbt dbt debug
DBT_PROFILES_DIR=./.dbt dbt ls --resource-type source
DBT_PROFILES_DIR=./.dbt dbt build
```

## NLQ Dashboard (Streamlit + FastAPI)

Launch the backend and UI locally:

```bash
# terminal 1: FastAPI backend
uvicorn backend.main:app --reload --port 8000

# terminal 2: Streamlit UI
streamlit run ui/app.py
```

The UI expects the backend at `http://localhost:8000` by default; override with `export BACKEND_URL=http://<host>:<port>`.

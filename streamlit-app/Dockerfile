# ---- build the component frontend ----
FROM node:20-alpine AS frontend
WORKDIR /src/superset_embed_component/frontend

COPY superset_embed_component/frontend/package.json ./
COPY superset_embed_component/frontend/tsconfig.json ./
COPY superset_embed_component/frontend/vite.config.ts ./
COPY superset_embed_component/frontend/index.html ./
COPY superset_embed_component/frontend/src ./src

RUN npm install
RUN npm run build

# ---- python runtime ----
FROM python:3.11-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV SUPERSET_EMBED_COMPONENT_RELEASE=1

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# app + component wrapper
COPY streamlit_app.py /app/streamlit_app.py
COPY schema_cytoscape.py /app/schema_cytoscape.py
COPY superset_embed_component /app/superset_embed_component
COPY .streamlit /app/.streamlit

# (선택) 앱에서 필요하면 데이터도 포함
# COPY cube_data /app/cube_data

# copy built frontend into python package
COPY --from=frontend /src/superset_embed_component/frontend/build \
  /app/superset_embed_component/frontend/build

EXPOSE 8501
CMD ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]

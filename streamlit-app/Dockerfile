# ---- build the frontend (Vite/React) ----
FROM node:20-alpine AS frontend
WORKDIR /src

COPY superset_embed_component/frontend/package.json superset_embed_component/frontend/package.json
COPY superset_embed_component/frontend/tsconfig.json superset_embed_component/frontend/tsconfig.json
COPY superset_embed_component/frontend/vite.config.ts superset_embed_component/frontend/vite.config.ts
COPY superset_embed_component/frontend/index.html superset_embed_component/frontend/index.html
COPY superset_embed_component/frontend/src superset_embed_component/frontend/src

WORKDIR /src/superset_embed_component/frontend
RUN npm install
RUN npm run build

# ---- python runtime ----
FROM python:3.11-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# app + component python wrapper
COPY streamlit_app.py /app/streamlit_app.py
COPY superset_embed_component /app/superset_embed_component

# copy built frontend into python package
COPY --from=frontend /src/superset_embed_component/frontend/dist /app/superset_embed_component/frontend/dist

EXPOSE 8501
CMD ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]

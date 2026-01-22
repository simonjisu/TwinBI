FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

COPY fastapi/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY fastapi/fastapi_service /app/fastapi_service
COPY fastapi/chart_desc.json /app/chart_desc.json

CMD ["uvicorn", "fastapi_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

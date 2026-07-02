FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/recsys/apps/api-serving/src:/opt/recsys/apps/data-platform/src

WORKDIR /opt/recsys

RUN pip install --no-cache-dir \
    fastapi \
    "feast[redis]" \
    httpx \
    numpy \
    opentelemetry-api \
    opentelemetry-exporter-otlp \
    opentelemetry-instrumentation-fastapi \
    opentelemetry-sdk \
    pydantic \
    prometheus-client \
    redis \
    tritonclient[grpc] \
    uvicorn

COPY apps/api-serving /opt/recsys/apps/api-serving
COPY apps/data-platform/feature-store /opt/recsys/apps/data-platform/feature-store
COPY apps/data-platform/src/feature_store /opt/recsys/apps/data-platform/src/feature_store

CMD ["uvicorn", "inference_api:app", "--host", "0.0.0.0", "--port", "8080"]

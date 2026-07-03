ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_CONCURRENT_DOWNLOADS=8 \
    UV_CONCURRENT_BUILDS=8

WORKDIR /opt/recsys

RUN pip install --no-cache-dir uv \
    && python -m venv /opt/venv

RUN uv pip install --python /opt/venv/bin/python --no-cache \
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

FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/recsys/apps/api-serving/src:/opt/recsys/apps/data-platform/src

WORKDIR /opt/recsys

COPY --from=deps /opt/venv /opt/venv
COPY apps/api-serving/src /opt/recsys/apps/api-serving/src
COPY apps/data-platform/feature-store /opt/recsys/apps/data-platform/feature-store
COPY apps/data-platform/src/feature_store /opt/recsys/apps/data-platform/src/feature_store

CMD ["uvicorn", "inference_api:app", "--host", "0.0.0.0", "--port", "8080"]

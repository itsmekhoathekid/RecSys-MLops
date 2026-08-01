ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_CONCURRENT_DOWNLOADS=8 \
    UV_CONCURRENT_BUILDS=8

WORKDIR /opt/recsys/apps/api-serving

COPY apps/api-serving/pyproject.toml apps/api-serving/uv.lock ./

RUN pip install --no-cache-dir uv==0.10.9 \
    && UV_PROJECT_ENVIRONMENT=/opt/venv \
        uv sync --frozen --no-dev --no-install-project --no-cache

FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/recsys/apps/api-serving/src:/opt/recsys/apps/data-platform/src

WORKDIR /opt/recsys

RUN /usr/local/bin/pip uninstall -y wheel setuptools jaraco.context \
    && /usr/local/bin/pip uninstall -y pip
COPY --from=deps /opt/venv /opt/venv
COPY apps/api-serving/pyproject.toml apps/api-serving/uv.lock /opt/recsys/apps/api-serving/
COPY apps/api-serving/src /opt/recsys/apps/api-serving/src
COPY apps/data-platform/feature-store /opt/recsys/apps/data-platform/feature-store
COPY apps/data-platform/src/feature_store /opt/recsys/apps/data-platform/src/feature_store

CMD ["uvicorn", "inference_api:app", "--host", "0.0.0.0", "--port", "8080"]

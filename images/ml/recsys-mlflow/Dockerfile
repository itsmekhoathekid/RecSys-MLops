FROM python:3.11-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_CONCURRENT_DOWNLOADS=8 \
    UV_CONCURRENT_BUILDS=8

COPY apps/ml-system/pyproject.toml apps/ml-system/uv.lock /opt/recsys/apps/ml-system/
COPY apps/data-platform/pyproject.toml /opt/recsys/apps/data-platform/pyproject.toml
COPY packages/recsys-feature-store-runtime /opt/recsys/packages/recsys-feature-store-runtime
RUN pip install --no-cache-dir uv==0.12.1 \
    && python -m venv /opt/venv

RUN uv export \
        --project /opt/recsys/apps/ml-system \
        --frozen \
        --no-dev \
        --no-emit-project \
        --no-emit-local \
        --no-hashes \
        --format requirements-txt \
        --output-file /tmp/ml-constraints.txt
RUN uv pip install --python /opt/venv/bin/python --no-cache \
    --constraint /tmp/ml-constraints.txt \
    boto3==1.35.36 \
    jaraco.context==6.1.0 \
    mlflow \
    psycopg2-binary==2.9.12 \
    setuptools==81.0.0 \
    wheel==0.46.2
RUN /opt/venv/bin/python -m pip uninstall -y pip \
    && /opt/venv/bin/python -c "import mlflow"

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash \
    && ln -s /opt/venv/bin/mlflow /usr/local/bin/mlflow \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/pip uninstall -y wheel setuptools jaraco.context \
    && /usr/local/bin/pip uninstall -y pip

COPY --from=deps /opt/venv /opt/venv

CMD ["mlflow", "--help"]

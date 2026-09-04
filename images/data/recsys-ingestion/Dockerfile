ARG RECSYS_BASE_IMAGE=registry.example.invalid/recsys/recsys-base-python:required
FROM ${RECSYS_BASE_IMAGE} AS deps

WORKDIR /opt/recsys
COPY apps/data-platform/pyproject.toml apps/data-platform/uv.lock /opt/recsys/apps/data-platform/
RUN python -m venv /opt/venv
RUN uv export --project /opt/recsys/apps/data-platform --frozen --no-dev \
        --no-emit-project --no-emit-local --no-hashes \
        --format requirements-txt --output-file /tmp/constraints.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python \
        --constraint /tmp/constraints.txt \
        boto3==1.35.36 botocore==1.35.93 fsspec==2024.10.0 \
        numpy==2.4.6 pyarrow==25.0.0 pydantic==2.13.4 \
        "psycopg[binary]==3.3.4" psycopg-pool==3.3.1 \
        pyyaml==6.0.3 requests==2.34.2 s3fs==2024.10.0
RUN /opt/venv/bin/python -m pip uninstall -y pip

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys/apps/data-platform/src:/opt/recsys
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/pip uninstall -y pip setuptools wheel \
    && groupadd --gid 10001 recsys \
    && useradd --uid 10001 --gid recsys --no-create-home --shell /usr/sbin/nologin recsys
WORKDIR /opt/recsys
COPY --from=deps /opt/venv /opt/venv
COPY configs/data-platform/generator /opt/recsys/configs/data-platform/generator
COPY configs/data-platform/spark /opt/recsys/configs/data-platform/spark
COPY apps/data-platform/data-generator /opt/recsys/apps/data-platform/data-generator
COPY apps/data-platform/src/ingest /opt/recsys/apps/data-platform/src/ingest
COPY apps/data-platform/src/lakehouse /opt/recsys/apps/data-platform/src/lakehouse
COPY apps/data-platform/src/features /opt/recsys/apps/data-platform/src/features
COPY apps/data-platform/src/validate /opt/recsys/apps/data-platform/src/validate
USER 10001:10001
CMD ["python", "/opt/recsys/apps/data-platform/data-generator/src/cli.py", "--help"]

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
        boto3==1.35.36 botocore==1.35.93 \
        pydantic==2.13.4 requests==2.34.2 pyyaml==6.0.3
RUN /opt/venv/bin/python -m pip uninstall -y pip

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH} PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/pip uninstall -y pip setuptools wheel \
    && groupadd --gid 10001 recsys \
    && useradd --uid 10001 --gid recsys --no-create-home --shell /usr/sbin/nologin recsys
WORKDIR /opt/recsys
COPY --from=deps /opt/venv /opt/venv
COPY apps/data-platform/src/ingest /opt/recsys/apps/data-platform/src/ingest
COPY apps/data-platform/src/lakehouse /opt/recsys/apps/data-platform/src/lakehouse
COPY apps/data-platform/src/validate /opt/recsys/apps/data-platform/src/validate
USER 10001:10001
CMD ["python", "-m", "ingest.register_k8s_connectors", "--help"]

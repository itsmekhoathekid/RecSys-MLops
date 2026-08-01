ARG RECSYS_BASE_IMAGE=registry.example.invalid/recsys/recsys-base-python:required
FROM ${RECSYS_BASE_IMAGE} AS deps

WORKDIR /opt/recsys
COPY apps/data-platform/pyproject.toml apps/data-platform/uv.lock /opt/recsys/apps/data-platform/
ENV UV_LINK_MODE=copy \
    UV_CONCURRENT_DOWNLOADS=8 \
    UV_CONCURRENT_BUILDS=8

RUN python -m venv /opt/venv
RUN uv export \
        --project /opt/recsys/apps/data-platform \
        --frozen \
        --no-dev \
        --no-emit-project \
        --no-hashes \
        --format requirements-txt \
        --output-file /tmp/data-constraints.txt
RUN uv pip install --python /opt/venv/bin/python --no-cache \
        --constraint /tmp/data-constraints.txt \
        boto3==1.35.36 \
        botocore==1.35.93 \
        fsspec==2024.10.0 \
        numpy==2.4.6 \
        pyarrow==25.0.0 \
        pydantic==2.13.4 \
        "psycopg[binary]==3.3.4" \
        psycopg-pool==3.3.1 \
        pyyaml==6.0.3 \
        requests==2.34.2 \
        s3fs==2024.10.0 \
        tqdm==4.69.0 \
        setuptools==83.0.0 \
        wheel==0.46.2 \
        jaraco.context==6.1.0

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys/apps/data-platform/src:/opt/recsys

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl jq \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/pip uninstall -y wheel setuptools jaraco.context \
    && /usr/local/bin/pip uninstall -y pip

WORKDIR /opt/recsys
COPY --from=deps /opt/venv /opt/venv
COPY configs /opt/recsys/configs
COPY apps/data-platform/src /opt/recsys/apps/data-platform/src
COPY apps/data-platform/data-generator /opt/recsys/apps/data-platform/data-generator

CMD ["python", "/opt/recsys/apps/data-platform/data-generator/src/cli.py", "--help"]

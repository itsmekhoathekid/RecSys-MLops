ARG RECSYS_BASE_IMAGE=recsys-base-python:local
FROM ${RECSYS_BASE_IMAGE} AS deps

WORKDIR /opt/recsys
COPY pyproject.toml uv.lock* /opt/recsys/
ENV UV_LINK_MODE=copy \
    UV_CONCURRENT_DOWNLOADS=8 \
    UV_CONCURRENT_BUILDS=8

RUN python -m venv /opt/venv

RUN uv pip install --python /opt/venv/bin/python --no-cache pandas pyarrow pydantic pyyaml boto3 tqdm

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys

WORKDIR /opt/recsys
COPY --from=deps /opt/venv /opt/venv
COPY pyproject.toml uv.lock* /opt/recsys/
COPY configs /opt/recsys/configs
COPY apps/data-platform/data-generator /opt/recsys/apps/data-platform/data-generator

CMD ["python", "/opt/recsys/apps/data-platform/data-generator/src/cli.py", "--help"]

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
        kfp==2.16.1 \
        pandas==2.3.3 \
        pyarrow==25.0.0 \
        setuptools==81.0.0 \
        wheel==0.46.2 \
        jaraco.context==6.1.0
RUN /opt/venv/bin/python -m pip uninstall -y pip \
    && /opt/venv/bin/python -c "import kfp, pandas, pyarrow"

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl jq \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/pip uninstall -y wheel setuptools jaraco.context \
    && /usr/local/bin/pip uninstall -y pip

WORKDIR /opt/recsys
COPY --from=deps /opt/venv /opt/venv
COPY apps/data-platform/src/__init__.py /opt/recsys/apps/data-platform/src/__init__.py
COPY apps/data-platform/src/mlops /opt/recsys/apps/data-platform/src/mlops
COPY apps/data-platform/src/monitoring /opt/recsys/apps/data-platform/src/monitoring
COPY apps/data-platform/src/validate/offline_feature_drift.py /opt/recsys/apps/data-platform/src/validate/offline_feature_drift.py
COPY apps/data-platform/src/validate/__init__.py /opt/recsys/apps/data-platform/src/validate/__init__.py

CMD ["python", "-m", "mlops.trigger_kubeflow_retrain", "--help"]

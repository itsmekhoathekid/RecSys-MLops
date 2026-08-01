ARG RECSYS_BASE_IMAGE=registry.example.invalid/recsys/recsys-base-python:required
FROM ${RECSYS_BASE_IMAGE} AS deps

WORKDIR /opt/recsys
ENV UV_LINK_MODE=copy \
    UV_CONCURRENT_DOWNLOADS=8 \
    UV_CONCURRENT_BUILDS=8

RUN python -m venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python --no-cache \
        boto3==1.35.36 \
        botocore==1.35.36 \
        kfp==2.16.1 \
        pandas \
        pyarrow \
        setuptools==83.0.0 \
        wheel==0.46.2 \
        jaraco.context==6.1.0

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

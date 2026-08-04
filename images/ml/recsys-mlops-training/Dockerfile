ARG RECSYS_BASE_IMAGE=registry.example.invalid/recsys/recsys-base-python:required
FROM ${RECSYS_BASE_IMAGE} AS deps

WORKDIR /opt/recsys
COPY apps/ml-system/pyproject.toml apps/ml-system/uv.lock /opt/recsys/apps/ml-system/
COPY apps/data-platform/pyproject.toml /opt/recsys/apps/data-platform/pyproject.toml
COPY packages/recsys-feature-store-runtime /opt/recsys/packages/recsys-feature-store-runtime

ENV UV_LINK_MODE=copy \
    UV_CONCURRENT_DOWNLOADS=8 \
    UV_CONCURRENT_BUILDS=8

RUN python -m venv /opt/venv

RUN uv export \
        --project /opt/recsys/apps/ml-system \
        --frozen \
        --no-dev \
        --no-emit-project \
        --no-emit-local \
        --no-hashes \
        --format requirements-txt \
        --output-file /tmp/ml-constraints.txt

RUN torch_version="$(sed -n 's/^torch==\([^ ;]*\).*$/\1/p' /tmp/ml-constraints.txt | head -n 1)" \
    && test -n "${torch_version}" \
    && uv pip install --python /opt/venv/bin/python --no-cache \
        --constraint /tmp/ml-constraints.txt \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        --index-strategy unsafe-best-match \
        "torch==${torch_version}+cpu"

RUN uv pip install --python /opt/venv/bin/python --no-cache \
    --constraint /tmp/ml-constraints.txt \
    boto3==1.35.36 \
    "feast[redis]==0.64.0" \
    kfp==2.16.1 \
    kfp-kubernetes==2.16.1 \
    kubernetes==30.1.0 \
    mlflow==3.14.0 \
    numpy==2.4.6 \
    onnx==1.22.0 \
    onnxscript==0.7.1 \
    pandas==2.3.3 \
    "psycopg[binary]==3.3.4" \
    psycopg-pool==3.3.1 \
    pyarrow==24.0.0 \
    pyyaml==6.0.3 \
    "ray[default,train,tune]==2.56.1" \
    s3fs==2024.10.0 \
    scikit-learn==1.9.0 \
    tqdm==4.69.0 \
    setuptools==81.0.0 \
    wheel==0.46.2 \
    jaraco.context==6.1.0
RUN /opt/venv/bin/python -m pip uninstall -y pip \
    && /opt/venv/bin/python -c "import kfp, mlflow, ray, torch"

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/recsys/apps/ml-system/src:/opt/recsys/apps/data-platform/src:/opt/recsys/apps/data-platform/feature-store/src:/opt/recsys

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wget \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/pip uninstall -y wheel setuptools jaraco.context \
    && /usr/local/bin/pip uninstall -y pip

WORKDIR /opt/recsys
COPY --from=deps /opt/venv /opt/venv
COPY apps/ml-system/pyproject.toml apps/ml-system/uv.lock /opt/recsys/apps/ml-system/
COPY configs /opt/recsys/configs
COPY apps/ml-system/src /opt/recsys/apps/ml-system/src
COPY apps/data-platform/src /opt/recsys/apps/data-platform/src
COPY apps/data-platform/feature-store /opt/recsys/apps/data-platform/feature-store
COPY packages/recsys-feature-store-runtime /opt/recsys/packages/recsys-feature-store-runtime

CMD ["bash"]

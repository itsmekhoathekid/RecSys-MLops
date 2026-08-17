ARG RECSYS_BASE_IMAGE=registry.example.invalid/recsys/recsys-base-python:required
ARG RAG_MODEL=intfloat/multilingual-e5-small
ARG RAG_MODEL_REVISION=03415a4be176a1620747c692ed433219fabc3def
ARG RAG_MODEL_CHECKSUM=sha256:8da4c9ba0ad59f58e8566839425d7fd6339d31414d0ce5cba2d7d0afb75dd8b6

FROM python:3.11-slim AS rag-model
ARG RAG_MODEL
ARG RAG_MODEL_REVISION
RUN pip install --no-cache-dir huggingface-hub==0.36.0 onnx==1.18.0 onnxruntime==1.23.2
COPY scripts/models/export_e5_onnx.py /tmp/export_e5_onnx.py
RUN python /tmp/export_e5_onnx.py \
        --model "${RAG_MODEL}" \
        --revision "${RAG_MODEL_REVISION}" \
        --output /opt/recsys/models/multilingual-e5-small

FROM ${RECSYS_BASE_IMAGE} AS deps

WORKDIR /opt/recsys
COPY apps/data-platform/pyproject.toml apps/data-platform/uv.lock /opt/recsys/apps/data-platform/
COPY packages/recsys-feature-store-runtime /opt/recsys/packages/recsys-feature-store-runtime
COPY packages/recsys-rag-runtime /opt/recsys/packages/recsys-rag-runtime
ENV UV_LINK_MODE=copy \
    UV_CONCURRENT_DOWNLOADS=8 \
    UV_CONCURRENT_BUILDS=8

RUN python -m venv /opt/venv
RUN uv export \
        --project /opt/recsys/apps/data-platform \
        --frozen \
        --no-dev \
        --no-emit-project \
        --no-emit-local \
        --no-hashes \
        --format requirements-txt \
        --output-file /tmp/data-constraints.txt
RUN uv pip install --python /opt/venv/bin/python --no-cache \
        --constraint /tmp/data-constraints.txt \
        acryl-datahub==1.6.0.17 \
        boto3==1.35.36 \
        botocore==1.35.93 \
        fsspec==2024.10.0 \
        "feast[milvus]==0.64.0" \
        numpy==2.4.6 \
        onnxruntime==1.23.2 \
        pyarrow==25.0.0 \
        pydantic==2.13.4 \
        "psycopg[binary]==3.3.4" \
        psycopg-pool==3.3.1 \
        psycopg2-binary==2.9.12 \
        pyyaml==6.0.3 \
        requests==2.34.2 \
        pymilvus==2.6.12 \
        s3fs==2024.10.0 \
        tqdm==4.69.0 \
        transformers==4.57.6 \
        setuptools==80.9.0 \
        wheel==0.46.2 \
        jaraco.context==6.1.0
RUN /opt/venv/bin/python -m pip uninstall -y pip \
    && /opt/venv/bin/python -c "from datahub.api.entities.dataprocess.dataprocess_instance import DataProcessInstance; from datahub.emitter.rest_emitter import DatahubRestEmitter"

FROM python:3.11-slim AS runtime
ARG RAG_MODEL
ARG RAG_MODEL_REVISION
ARG RAG_MODEL_CHECKSUM
LABEL org.opencontainers.image.recsys.embedding.model="${RAG_MODEL}" \
      org.opencontainers.image.recsys.embedding.revision="${RAG_MODEL_REVISION}" \
      org.opencontainers.image.recsys.embedding.checksum="${RAG_MODEL_CHECKSUM}" \
      org.opencontainers.image.recsys.embedding.manifest="/opt/recsys/models/multilingual-e5-small/model_manifest.json"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys/apps/data-platform/src:/opt/recsys/packages/recsys-feature-store-runtime/src:/opt/recsys/packages/recsys-rag-runtime/src:/opt/recsys \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_OFFLINE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl jq \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/pip uninstall -y wheel setuptools jaraco.context \
    && /usr/local/bin/pip uninstall -y pip \
    && groupadd --gid 10001 recsys \
    && useradd --uid 10001 --gid recsys --no-create-home --shell /usr/sbin/nologin recsys

WORKDIR /opt/recsys
COPY --from=deps /opt/venv /opt/venv
COPY --from=rag-model /opt/recsys/models /opt/recsys/models
COPY configs /opt/recsys/configs
COPY apps/data-platform/src /opt/recsys/apps/data-platform/src
COPY packages/recsys-feature-store-runtime /opt/recsys/packages/recsys-feature-store-runtime
COPY packages/recsys-rag-runtime /opt/recsys/packages/recsys-rag-runtime
COPY apps/data-platform/feature-store/rag_feature_repo /opt/recsys/apps/data-platform/feature-store/rag_feature_repo
COPY apps/data-platform/data-generator /opt/recsys/apps/data-platform/data-generator

USER 10001:10001
CMD ["python", "/opt/recsys/apps/data-platform/data-generator/src/cli.py", "--help"]

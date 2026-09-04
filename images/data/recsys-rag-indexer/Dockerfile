ARG RECSYS_BASE_IMAGE=registry.example.invalid/recsys/recsys-base-python:required
ARG RAG_MODEL_IMAGE=registry.example.invalid/recsys/recsys-rag-model-e5:required
FROM ${RAG_MODEL_IMAGE} AS rag-model
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
        "feast[milvus]==0.64.0" numpy==2.4.6 onnxruntime==1.23.2 \
        pandas==2.3.3 pyarrow==25.0.0 pydantic==2.13.4 \
        "psycopg[binary]==3.3.4" psycopg-pool==3.3.1 pymilvus==2.6.12 \
        pyyaml==6.0.3 requests==2.34.2 s3fs==2024.10.0 \
        tqdm==4.69.0 transformers==4.57.6
RUN /opt/venv/bin/python -m pip uninstall -y pip

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys/apps/data-platform/feature-store/runtime/src:/opt/recsys/apps/data-platform/rag-runtime/src:/opt/recsys \
    TRANSFORMERS_OFFLINE=1 HF_HUB_OFFLINE=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/pip uninstall -y pip setuptools wheel \
    && groupadd --gid 10001 recsys \
    && useradd --uid 10001 --gid recsys --no-create-home --shell /usr/sbin/nologin recsys
WORKDIR /opt/recsys
COPY --from=deps /opt/venv /opt/venv
COPY --from=rag-model /models /opt/recsys/models
COPY configs/data-platform/rag /opt/recsys/configs/data-platform/rag
COPY apps/data-platform/src/rag_data /opt/recsys/apps/data-platform/src/rag_data
COPY apps/data-platform/src/validate /opt/recsys/apps/data-platform/src/validate
COPY apps/data-platform/feature-store/runtime /opt/recsys/apps/data-platform/feature-store/runtime
COPY apps/data-platform/rag-runtime /opt/recsys/apps/data-platform/rag-runtime
COPY apps/data-platform/feature-store/rag_feature_repo /opt/recsys/apps/data-platform/feature-store/rag_feature_repo
USER 10001:10001
CMD ["python", "-m", "rag_data.cli", "--help"]

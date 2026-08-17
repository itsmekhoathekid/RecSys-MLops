ARG PYTHON_VERSION=3.11
ARG RAG_MODEL=intfloat/multilingual-e5-small
ARG RAG_MODEL_REVISION=03415a4be176a1620747c692ed433219fabc3def
ARG RAG_MODEL_CHECKSUM=sha256:8da4c9ba0ad59f58e8566839425d7fd6339d31414d0ce5cba2d7d0afb75dd8b6

FROM python:${PYTHON_VERSION}-slim AS model
ARG RAG_MODEL
ARG RAG_MODEL_REVISION
RUN pip install --no-cache-dir huggingface-hub==0.36.0 onnx==1.18.0 onnxruntime==1.23.2
COPY scripts/models/export_e5_onnx.py /tmp/export_e5_onnx.py
RUN python /tmp/export_e5_onnx.py \
    --model "${RAG_MODEL}" \
    --revision "${RAG_MODEL_REVISION}" \
    --output /opt/recsys/models/multilingual-e5-small

FROM python:${PYTHON_VERSION}-slim AS deps
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /opt/recsys/apps/api-serving/rag-api
RUN pip install --no-cache-dir uv==0.12.1
COPY apps/api-serving/rag-api ./
COPY apps/api-serving/shared /opt/recsys/apps/api-serving/shared
COPY packages/recsys-rag-runtime /opt/recsys/packages/recsys-rag-runtime
RUN uv sync --frozen --no-dev --no-cache

FROM python:${PYTHON_VERSION}-slim AS runtime
ARG RAG_MODEL
ARG RAG_MODEL_REVISION
ARG RAG_MODEL_CHECKSUM
LABEL org.opencontainers.image.recsys.embedding.model="${RAG_MODEL}" \
      org.opencontainers.image.recsys.embedding.revision="${RAG_MODEL_REVISION}" \
      org.opencontainers.image.recsys.embedding.checksum="${RAG_MODEL_CHECKSUM}" \
      org.opencontainers.image.recsys.embedding.manifest="/opt/recsys/models/multilingual-e5-small/model_manifest.json"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_OFFLINE=1 \
    PATH=/opt/venv/bin:${PATH}
WORKDIR /opt/recsys
RUN groupadd --gid 10001 recsys \
    && useradd --uid 10001 --gid recsys --no-create-home --shell /usr/sbin/nologin recsys
COPY --from=deps /opt/venv /opt/venv
COPY --from=model /opt/recsys/models /opt/recsys/models
COPY apps/api-serving/rag-api /opt/recsys/apps/api-serving/rag-api
COPY apps/api-serving/shared /opt/recsys/apps/api-serving/shared
COPY packages/recsys-rag-runtime /opt/recsys/packages/recsys-rag-runtime
COPY apps/data-platform/feature-store/rag_feature_repo /opt/recsys/apps/data-platform/feature-store/rag_feature_repo
RUN pip check \
    && pip uninstall -y wheel setuptools \
    && pip uninstall -y pip \
    && chmod -R a-w /opt/recsys/models
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "recsys_rag_api.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

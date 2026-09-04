ARG RAG_MODEL=intfloat/multilingual-e5-small
ARG RAG_MODEL_REVISION=03415a4be176a1620747c692ed433219fabc3def
ARG RAG_MODEL_CHECKSUM=sha256:8da4c9ba0ad59f58e8566839425d7fd6339d31414d0ce5cba2d7d0afb75dd8b6

FROM python:3.11-slim AS exporter
ARG RAG_MODEL
ARG RAG_MODEL_REVISION
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install huggingface-hub==0.36.0 onnx==1.18.0 onnxruntime==1.23.2
COPY scripts/models/export_e5_onnx.py /tmp/export_e5_onnx.py
RUN python /tmp/export_e5_onnx.py \
        --model "${RAG_MODEL}" \
        --revision "${RAG_MODEL_REVISION}" \
        --output /models/multilingual-e5-small

FROM scratch
ARG RAG_MODEL
ARG RAG_MODEL_REVISION
ARG RAG_MODEL_CHECKSUM
LABEL org.opencontainers.image.recsys.embedding.model="${RAG_MODEL}" \
      org.opencontainers.image.recsys.embedding.revision="${RAG_MODEL_REVISION}" \
      org.opencontainers.image.recsys.embedding.checksum="${RAG_MODEL_CHECKSUM}"
COPY --from=exporter /models /models

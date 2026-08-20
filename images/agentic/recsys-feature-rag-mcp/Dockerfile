ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /opt/recsys/apps/agentic/recsys-feature-rag-mcp
RUN pip install --no-cache-dir uv==0.12.1
COPY apps/agentic/recsys-feature-rag-mcp ./
RUN uv sync --frozen --no-dev --no-cache

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH}
WORKDIR /opt/recsys
RUN groupadd --gid 10001 recsys \
    && useradd --uid 10001 --gid recsys --no-create-home --shell /usr/sbin/nologin recsys
COPY --from=deps /opt/venv /opt/venv
COPY apps/agentic/recsys-feature-rag-mcp /opt/recsys/apps/agentic/recsys-feature-rag-mcp
RUN pip check \
    && pip uninstall -y wheel setuptools pip
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "recsys_feature_rag_mcp.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

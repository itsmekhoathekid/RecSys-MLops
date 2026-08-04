ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /opt/recsys/apps/api-serving/inference-api

RUN pip install --no-cache-dir uv==0.12.1
COPY apps/api-serving/inference-api ./
COPY apps/api-serving/shared /opt/recsys/apps/api-serving/shared
RUN uv sync --frozen --no-dev --no-cache

FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH}

WORKDIR /opt/recsys

RUN groupadd --gid 10001 recsys \
    && useradd --uid 10001 --gid recsys --no-create-home --shell /usr/sbin/nologin recsys
COPY --from=deps /opt/venv /opt/venv
COPY apps/api-serving/inference-api /opt/recsys/apps/api-serving/inference-api
COPY apps/api-serving/shared /opt/recsys/apps/api-serving/shared
RUN pip check \
    && pip uninstall -y wheel setuptools jaraco.context \
    && pip uninstall -y pip

USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "recsys_inference_api.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

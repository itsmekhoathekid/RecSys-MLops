FROM python:3.11-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /opt/recsys-demo-api

RUN pip install --no-cache-dir uv
COPY apps/demo-web/backend/pyproject.toml apps/demo-web/backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/opt/recsys-demo-api
WORKDIR /opt/recsys-demo-api

RUN groupadd --gid 10001 recsys \
    && useradd --uid 10001 --gid recsys --no-create-home --shell /usr/sbin/nologin recsys
COPY --from=deps /opt/venv /opt/venv
COPY apps/demo-web/backend/app ./app
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

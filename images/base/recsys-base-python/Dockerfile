FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        curl \
        git \
        jq \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        uv==0.12.1 \
        setuptools==83.0.0 \
        wheel==0.46.2 \
        jaraco.context==6.1.0

WORKDIR /opt/recsys

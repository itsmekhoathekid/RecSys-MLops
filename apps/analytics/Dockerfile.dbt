FROM python:3.13-slim

ARG DBT_TRINO_VERSION=1.10.2
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DBT_PROFILES_DIR=/opt/recsys/apps/analytics/profiles

RUN pip install \
    "dbt-trino==${DBT_TRINO_VERSION}" \
    "setuptools==83.0.0" \
    "wheel==0.46.2" \
    "jaraco.context==6.1.0"
WORKDIR /opt/recsys/apps/analytics
COPY apps/analytics /opt/recsys/apps/analytics
RUN dbt deps

ENTRYPOINT ["dbt"]
CMD ["build"]

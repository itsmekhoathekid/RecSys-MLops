ARG AIRFLOW_VERSION=2.9.3
ARG AIRFLOW_PYTHON_VERSION=3.10

FROM apache/airflow:slim-${AIRFLOW_VERSION}-python${AIRFLOW_PYTHON_VERSION} AS deps

ARG AIRFLOW_VERSION
ARG AIRFLOW_PYTHON_VERSION
ARG AIRFLOW_CONSTRAINTS_URL=https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${AIRFLOW_PYTHON_VERSION}.txt

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

USER airflow
RUN pip install --upgrade \
    --constraint "${AIRFLOW_CONSTRAINTS_URL}" \
    "apache-airflow==${AIRFLOW_VERSION}" \
    apache-airflow-providers-docker \
    apache-airflow-providers-http \
    apache-airflow-providers-cncf-kubernetes \
    apache-airflow-providers-postgres \
    pyyaml \
    redis \
    requests \
    && pip install --upgrade \
    "Mako==1.3.12" \
    "PyJWT==2.13.0" \
    "aiohttp==3.13.3" \
    "cryptography==48.0.1" \
    "setuptools==78.1.1" \
    "urllib3==2.7.0" \
    "wheel==0.46.2" \
    && pip check

FROM apache/airflow:slim-${AIRFLOW_VERSION}-python${AIRFLOW_PYTHON_VERSION} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PATH=/home/airflow/.local/bin:${PATH}

USER root
WORKDIR /opt/recsys
RUN apt-get update \
    && apt-mark hold msodbcsql18 \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /home/airflow/.local
COPY --from=deps --chown=airflow:root /home/airflow/.local /home/airflow/.local
COPY --chown=airflow:root configs /opt/recsys/configs
COPY --chown=airflow:root apps/data-platform/src /opt/recsys/apps/data-platform/src
COPY --chown=airflow:root apps/data-platform/data-generator /opt/recsys/apps/data-platform/data-generator
COPY --chown=airflow:root apps/data-platform/feature-store /opt/recsys/apps/data-platform/feature-store
COPY --chown=airflow:root apps/analytics/orchestration/airflow/dags /opt/recsys/apps/data-platform/src/orchestration/airflow/dags
RUN chown -R airflow:root /home/airflow \
    && chmod -R g+rwX /home/airflow \
    && rm -f /home/airflow/.local/bin/uv /home/airflow/.local/bin/uvx \
    && rm -rf /home/airflow/.local/share/uv
USER airflow

ENV PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys

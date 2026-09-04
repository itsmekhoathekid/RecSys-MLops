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
    apache-airflow-providers-cncf-kubernetes==8.3.3 \
    apache-airflow-providers-postgres==5.11.2 \
    && pip install --upgrade \
    "Mako==1.3.12" \
    "PyJWT==2.13.0" \
    "aiohttp==3.14.3" \
    "cryptography==50.0.0" \
    "pyasn1==0.6.4" \
    "pyasn1-modules==0.4.2" \
    "setuptools==78.1.1" \
    "urllib3==2.7.0" \
    "wheel==0.46.2" \
    && pip check

FROM apache/airflow:slim-${AIRFLOW_VERSION}-python${AIRFLOW_PYTHON_VERSION} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PATH=/home/airflow/.local/bin:${PATH}

USER root
WORKDIR /opt/recsys
RUN rm -rf /home/airflow/.local
COPY --from=deps --chown=airflow:root /home/airflow/.local /home/airflow/.local
COPY --chown=airflow:root apps/data-platform/src/orchestration /opt/recsys/apps/data-platform/src/orchestration
COPY --chown=airflow:root apps/analytics/orchestration/airflow/dags /opt/recsys/apps/data-platform/src/orchestration/airflow/dags
RUN chown -R airflow:root /home/airflow \
    && chmod -R g+rwX /home/airflow \
    && rm -f /home/airflow/.local/bin/uv /home/airflow/.local/bin/uvx \
    && rm -rf /home/airflow/.local/share/uv
USER airflow

ENV PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys

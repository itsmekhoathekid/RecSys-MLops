ARG RECSYS_SPARK_RUNTIME_IMAGE=registry.example.invalid/recsys/recsys-spark-runtime:required
FROM ${RECSYS_SPARK_RUNTIME_IMAGE}
USER root
ENV UV_LINK_MODE=copy UV_CONCURRENT_DOWNLOADS=8 UV_CONCURRENT_BUILDS=8
COPY apps/data-platform/pyproject.toml apps/data-platform/uv.lock /opt/recsys/apps/data-platform/
COPY apps/data-platform/feature-store/runtime /opt/recsys/apps/data-platform/feature-store/runtime
RUN python3 -m venv /opt/venv \
    && uv export --project /opt/recsys/apps/data-platform --frozen --no-dev \
        --no-emit-project --no-emit-local --no-hashes \
        --format requirements-txt --output-file /tmp/constraints.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python --constraint /tmp/constraints.txt \
        boto3==1.35.36 fsspec==2024.10.0 "feast[redis]==0.64.0" \
        numpy==2.2.6 pandas==2.3.3 "psycopg[binary]==3.3.4" \
        psycopg-pool==3.3.1 psycopg2-binary==2.9.12 pyarrow==25.0.0 \
        pydantic==2.13.4 pyyaml==6.0.3 redis==7.4.1 requests==2.34.2 \
        s3fs==2024.10.0 sqlalchemy==1.4.54 \
    && /opt/venv/bin/python -m pip uninstall -y pip
COPY configs/data-platform /opt/recsys/configs/data-platform
COPY apps/data-platform/src /opt/recsys/apps/data-platform/src
COPY apps/data-platform/data-generator /opt/recsys/apps/data-platform/data-generator
COPY apps/data-platform/feature-store /opt/recsys/apps/data-platform/feature-store
COPY apps/data-platform/feature-store/runtime /opt/recsys/apps/data-platform/feature-store/runtime
ENV PATH=/opt/venv/bin:${PATH} PYSPARK_PYTHON=/opt/venv/bin/python \
    PYSPARK_DRIVER_PYTHON=/opt/venv/bin/python \
    PYTHONPATH=/opt/spark/python:/opt/spark/python/lib/pyspark.zip:/opt/spark/python/lib/py4j-0.10.9.7-src.zip:/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys/apps/data-platform/src:/opt/recsys/apps/data-platform/feature-store/runtime/src:/opt/recsys
USER spark

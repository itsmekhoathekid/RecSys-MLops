ARG RECSYS_SPARK_RUNTIME_IMAGE=registry.example.invalid/recsys/recsys-spark-runtime:required
FROM ${RECSYS_SPARK_RUNTIME_IMAGE}
USER root
ENV UV_LINK_MODE=copy
COPY apps/data-platform/pyproject.toml apps/data-platform/uv.lock /opt/recsys/apps/data-platform/
RUN python3 -m venv /opt/venv \
    && uv export --project /opt/recsys/apps/data-platform --frozen --no-dev \
        --no-emit-project --no-emit-local --no-hashes \
        --format requirements-txt --output-file /tmp/constraints.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python --constraint /tmp/constraints.txt \
        boto3==1.35.36 fsspec==2024.10.0 pandas==2.3.3 pyarrow==25.0.0 \
        pydantic==2.13.4 pyyaml==6.0.3 requests==2.34.2 s3fs==2024.10.0 \
    && /opt/venv/bin/python -m pip uninstall -y pip
COPY configs/data-platform /opt/recsys/configs/data-platform
COPY apps/analytics/src /opt/recsys/apps/analytics/src
COPY apps/data-platform/src/validate /opt/recsys/validate
ENV PATH=/opt/venv/bin:${PATH} PYSPARK_PYTHON=/opt/venv/bin/python \
    PYSPARK_DRIVER_PYTHON=/opt/venv/bin/python \
    PYTHONPATH=/opt/spark/python:/opt/spark/python/lib/pyspark.zip:/opt/spark/python/lib/py4j-0.10.9.7-src.zip:/opt/recsys/apps/analytics/src:/opt/recsys
USER spark

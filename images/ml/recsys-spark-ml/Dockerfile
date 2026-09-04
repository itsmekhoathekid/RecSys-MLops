ARG RECSYS_SPARK_RUNTIME_IMAGE=registry.example.invalid/recsys/recsys-spark-runtime:required
FROM ${RECSYS_SPARK_RUNTIME_IMAGE}
USER root
ENV UV_LINK_MODE=copy UV_CONCURRENT_DOWNLOADS=8 UV_CONCURRENT_BUILDS=8
COPY apps/ml-system/pyproject.toml apps/ml-system/uv.lock /opt/recsys/apps/ml-system/
COPY apps/data-platform/feature-store/runtime /opt/recsys/apps/data-platform/feature-store/runtime
RUN python3 -m venv /opt/venv \
    && uv export --project /opt/recsys/apps/ml-system --frozen --no-dev \
        --no-emit-project --no-emit-local --no-hashes \
        --format requirements-txt --output-file /tmp/constraints.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python --constraint /tmp/constraints.txt \
        boto3==1.35.36 "feast[redis]==0.64.0" numpy==2.2.6 pandas==2.3.3 \
        "psycopg[binary]==3.3.4" psycopg-pool==3.3.1 pyarrow==24.0.0 \
        psycopg2-binary==2.9.12 sqlalchemy==1.4.54 \
    && uv pip check --python /opt/venv/bin/python \
    && /opt/venv/bin/python -m pip uninstall -y pip
COPY apps/ml-system/src/cli/prepare_bst_training_data.py /opt/recsys/apps/ml-system/src/cli/prepare_bst_training_data.py
COPY apps/ml-system/src/cli/create_hudi_savepoint.py /opt/recsys/apps/ml-system/src/cli/create_hudi_savepoint.py
COPY apps/ml-system/src/lineage/dataset_versioning.py /opt/recsys/apps/ml-system/src/lineage/dataset_versioning.py
COPY apps/data-platform/feature-store /opt/recsys/apps/data-platform/feature-store
COPY apps/data-platform/feature-store/runtime /opt/recsys/apps/data-platform/feature-store/runtime
ENV PATH=/opt/venv/bin:${PATH} PYSPARK_PYTHON=/opt/venv/bin/python \
    PYSPARK_DRIVER_PYTHON=/opt/venv/bin/python \
    PYTHONPATH=/opt/spark/python:/opt/spark/python/lib/pyspark.zip:/opt/spark/python/lib/py4j-0.10.9.7-src.zip:/opt/recsys/apps/ml-system/src:/opt/recsys/apps/data-platform/src:/opt/recsys/apps/data-platform/feature-store/runtime/src:/opt/recsys
USER spark

FROM spark:3.5.8-scala2.12-java11-ubuntu AS jar-downloader

USER root
ARG DOWNLOAD_JOBS=4
ARG POSTGRES_JDBC_VERSION=42.7.7
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /tmp/recsys-jars \
    && printf '%s %s\n' \
        https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-3.5_2.12/1.7.1/iceberg-spark-runtime-3.5_2.12-1.7.1.jar /tmp/recsys-jars/iceberg-spark-runtime-3.5_2.12-1.7.1.jar \
        https://repo1.maven.org/maven2/org/apache/hudi/hudi-spark3.5-bundle_2.12/1.0.2/hudi-spark3.5-bundle_2.12-1.0.2.jar /tmp/recsys-jars/hudi-spark3.5-bundle_2.12-1.0.2.jar \
        https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/3.3.4/hadoop-aws-3.3.4.jar /tmp/recsys-jars/hadoop-aws-3.3.4.jar \
        https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/1.12.262/aws-java-sdk-bundle-1.12.262.jar /tmp/recsys-jars/aws-java-sdk-bundle-1.12.262.jar \
        https://repo1.maven.org/maven2/org/postgresql/postgresql/${POSTGRES_JDBC_VERSION}/postgresql-${POSTGRES_JDBC_VERSION}.jar /tmp/recsys-jars/postgresql-${POSTGRES_JDBC_VERSION}.jar \
    | xargs -n 2 -P "${DOWNLOAD_JOBS}" sh -c 'curl -fsSL "$1" -o "$2"' sh

FROM spark:3.5.8-scala2.12-java11-ubuntu AS python-deps

USER root
ENV UV_LINK_MODE=copy \
    UV_CONCURRENT_DOWNLOADS=8 \
    UV_CONCURRENT_BUILDS=8

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip python3.10-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir uv==0.12.1 \
    && python3 -m venv /opt/venv

COPY apps/ml-system/pyproject.toml apps/ml-system/uv.lock /opt/recsys/apps/ml-system/
COPY apps/data-platform/pyproject.toml apps/data-platform/uv.lock /opt/recsys/apps/data-platform/

# Spark owns PySpark and its JARs. Distributed Ray training remains in the
# dedicated training image. This image installs the locked ML/data runtime
# needed by Spark jobs and replaces PyPI's CUDA torch closure with the CPU wheel.
RUN uv export \
        --project /opt/recsys/apps/ml-system \
        --frozen \
        --no-dev \
        --no-emit-project \
        --no-emit-local \
        --no-hashes \
        --format requirements-txt \
        --output-file /tmp/ml-constraints.txt \
    && torch_version="$(sed -n 's/^torch==\([^ ;]*\).*$/\1/p' /tmp/ml-constraints.txt | head -n 1)" \
    && test -n "${torch_version}" \
    && uv pip install \
        --python /opt/venv/bin/python \
        --no-cache \
        --constraint /tmp/ml-constraints.txt \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        --index-strategy unsafe-best-match \
        "torch==${torch_version}+cpu" \
    && uv pip install \
        --python /opt/venv/bin/python \
        --no-cache \
        --constraint /tmp/ml-constraints.txt \
        acryl-datahub==1.6.0.17 \
        boto3==1.35.36 \
        cryptography==48.0.1 \
        evidently==0.7.21 \
        "feast[redis]==0.64.0" \
        flask==3.1.3 \
        fsspec==2024.10.0 \
        kfp==2.16.1 \
        kfp-kubernetes==2.16.1 \
        kubernetes==30.1.0 \
        jaraco.context==6.1.0 \
        mlflow==3.14.0 \
        numpy==2.2.6 \
        onnx==1.22.0 \
        onnxscript==0.7.1 \
        openlineage-python==1.52.0 \
        pandas==2.3.3 \
        pendulum==3.2.0 \
        "psycopg[binary]==3.3.4" \
        psycopg-pool==3.3.1 \
        psycopg2-binary==2.9.12 \
        pyarrow==24.0.0 \
        pyyaml==6.0.3 \
        redis==7.4.1 \
        requests==2.34.2 \
        s3fs==2024.10.0 \
        scikit-learn==1.7.2 \
        sqlalchemy==1.4.54 \
        tqdm==4.69.0 \
        werkzeug==3.1.8 \
        setuptools==81.0.0 \
        wheel==0.46.2 \
    && uv pip check --python /opt/venv/bin/python

FROM spark:3.5.8-scala2.12-java11-ubuntu AS runtime

WORKDIR /opt/recsys
USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl python3-pip python3.10-venv \
    && rm -rf /var/lib/apt/lists/*

COPY --from=jar-downloader /tmp/recsys-jars/*.jar /opt/spark/jars/
COPY --from=python-deps /opt/venv /opt/venv
COPY configs /opt/recsys/configs
COPY apps/data-platform/src /opt/recsys/apps/data-platform/src
COPY apps/data-platform/data-generator /opt/recsys/apps/data-platform/data-generator
COPY apps/data-platform/feature-store /opt/recsys/apps/data-platform/feature-store
COPY apps/ml-system/src /opt/recsys/apps/ml-system/src
COPY apps/analytics/src /opt/recsys/apps/analytics/src

ENV PATH=/opt/venv/bin:${PATH} \
    PYSPARK_PYTHON=/opt/venv/bin/python \
    PYSPARK_DRIVER_PYTHON=/opt/venv/bin/python \
    AWS_REGION=us-east-1 \
    SPARK_EXTRA_CLASSPATH=/opt/spark/jars/* \
    PYTHONPATH=/opt/spark/python:/opt/spark/python/lib/pyspark.zip:/opt/spark/python/lib/py4j-0.10.9.7-src.zip:/opt/recsys/apps/analytics/src:/opt/recsys/apps/ml-system/src:/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys/apps/data-platform/src:/opt/recsys

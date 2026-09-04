FROM spark:3.5.8-scala2.12-java11-ubuntu AS jar-downloader

USER root
ARG DOWNLOAD_JOBS=4
ARG HUDI_VERSION=1.2.0
ARG NETTY_VERSION=4.1.136.Final
ARG POSTGRES_JDBC_VERSION=42.7.12
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /tmp/recsys-jars \
    && printf '%s %s\n' \
        https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-3.5_2.12/1.7.1/iceberg-spark-runtime-3.5_2.12-1.7.1.jar /tmp/recsys-jars/iceberg-spark-runtime-3.5_2.12-1.7.1.jar \
        https://repo1.maven.org/maven2/org/apache/hudi/hudi-spark3.5-bundle_2.12/${HUDI_VERSION}/hudi-spark3.5-bundle_2.12-${HUDI_VERSION}.jar /tmp/recsys-jars/hudi-spark3.5-bundle_2.12-${HUDI_VERSION}.jar \
        https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/3.3.4/hadoop-aws-3.3.4.jar /tmp/recsys-jars/hadoop-aws-3.3.4.jar \
        https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/1.12.262/aws-java-sdk-bundle-1.12.262.jar /tmp/recsys-jars/aws-java-sdk-bundle-1.12.262.jar \
        https://repo1.maven.org/maven2/io/netty/netty-codec-http2/${NETTY_VERSION}/netty-codec-http2-${NETTY_VERSION}.jar /tmp/recsys-jars/netty-codec-http2-${NETTY_VERSION}.jar \
        https://repo1.maven.org/maven2/org/postgresql/postgresql/${POSTGRES_JDBC_VERSION}/postgresql-${POSTGRES_JDBC_VERSION}.jar /tmp/recsys-jars/postgresql-${POSTGRES_JDBC_VERSION}.jar \
    | xargs -n 2 -P "${DOWNLOAD_JOBS}" sh -c 'curl -fsSL "$1" -o "$2"' sh

FROM spark:3.5.8-scala2.12-java11-ubuntu
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl python3-pip python3.10-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir uv==0.12.1 \
    && rm /opt/spark/jars/netty-codec-http2-4.1.96.Final.jar
COPY --from=jar-downloader /tmp/recsys-jars/*.jar /opt/spark/jars/
ENV AWS_REGION=us-east-1 SPARK_EXTRA_CLASSPATH=/opt/spark/jars/*
WORKDIR /opt/recsys

ARG CONFLUENT_PLATFORM_VERSION=8.2.2-1-ubi9
ARG DEBEZIUM_POSTGRES_VERSION=3.4.3.Final
ARG DEBEZIUM_POSTGRES_SHA256=7a4f5899fd31977ff1f1b7166619ed81c3df243cc3fbc29ebc7a89b5f0d61ebd
ARG NETTY_VERSION=4.1.136.Final

FROM alpine:3.23 AS plugins

ARG DEBEZIUM_POSTGRES_VERSION
ARG DEBEZIUM_POSTGRES_SHA256
ARG NETTY_VERSION

RUN apk upgrade --no-cache \
    && apk add --no-cache ca-certificates curl \
    && plugin_archive="/tmp/debezium-connector-postgres.tar.gz" \
    && plugin_root="/tmp/confluent-hub-components" \
    && mkdir -p "${plugin_root}" \
    && curl -fsSL \
      "https://repo1.maven.org/maven2/io/debezium/debezium-connector-postgres/${DEBEZIUM_POSTGRES_VERSION}/debezium-connector-postgres-${DEBEZIUM_POSTGRES_VERSION}-plugin.tar.gz" \
      -o "${plugin_archive}" \
    && echo "${DEBEZIUM_POSTGRES_SHA256}  ${plugin_archive}" | sha256sum -c - \
    && tar -xzf "${plugin_archive}" -C "${plugin_root}" \
    && test -f "${plugin_root}/debezium-connector-postgres/debezium-connector-postgres-${DEBEZIUM_POSTGRES_VERSION}.jar" \
    && curl -fsSL \
      "https://repo1.maven.org/maven2/io/netty/netty-codec-http2/${NETTY_VERSION}/netty-codec-http2-${NETTY_VERSION}.jar" \
      -o "/tmp/netty-codec-http2-${NETTY_VERSION}.jar"

FROM confluentinc/cp-kafka-connect:${CONFLUENT_PLATFORM_VERSION} AS runtime

ARG NETTY_VERSION
USER root
RUN microdnf upgrade -y \
    && microdnf clean all \
    && rm /usr/share/java/kafka-serde-tools/netty-codec-http2-4.1.133.Final.jar
COPY --from=plugins /tmp/confluent-hub-components /usr/share/confluent-hub-components
COPY --from=plugins /tmp/netty-codec-http2-${NETTY_VERSION}.jar /usr/share/java/kafka-serde-tools/
RUN test -f /usr/share/java/kafka-serde-tools/netty-codec-http2-${NETTY_VERSION}.jar \
    && test ! -e /usr/share/java/kafka-serde-tools/netty-codec-http2-4.1.133.Final.jar

USER appuser

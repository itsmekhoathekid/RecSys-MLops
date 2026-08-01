ARG CONFLUENT_PLATFORM_VERSION=8.2.2-1-ubi9
ARG DEBEZIUM_POSTGRES_VERSION=3.4.3.Final
ARG DEBEZIUM_POSTGRES_SHA256=7a4f5899fd31977ff1f1b7166619ed81c3df243cc3fbc29ebc7a89b5f0d61ebd

FROM alpine:3.23 AS plugins

ARG DEBEZIUM_POSTGRES_VERSION
ARG DEBEZIUM_POSTGRES_SHA256

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
    && test -f "${plugin_root}/debezium-connector-postgres/debezium-connector-postgres-${DEBEZIUM_POSTGRES_VERSION}.jar"

FROM confluentinc/cp-kafka-connect:${CONFLUENT_PLATFORM_VERSION} AS runtime

USER root
RUN microdnf upgrade -y \
    && microdnf clean all
COPY --from=plugins /tmp/confluent-hub-components /usr/share/confluent-hub-components

USER appuser

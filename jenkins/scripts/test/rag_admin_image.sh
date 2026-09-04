#!/usr/bin/env bash
set -euo pipefail

image="${1:?usage: rag_admin_image.sh IMAGE}"

docker run --rm --entrypoint python "${image}" -c '
import psycopg2
from feast.infra.registry.sql import SqlRegistry

assert psycopg2.__version__
assert SqlRegistry.__name__ == "SqlRegistry"
'

printf 'RAG admin image smoke passed: %s\n' "${image}"

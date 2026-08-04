#!/usr/bin/env bash
set -euo pipefail

image="${1:?image reference is required}"
service="${2:?service name is required}"

case "${service}" in
  recsys-online-feature-api)
    docker run --rm "${image}" python -c '
import importlib.util
from recsys_online_feature_api.app import app
assert app.title == "RecSys Online Feature API"
assert importlib.util.find_spec("tritonclient") is None
'
    ;;
  recsys-inference-api)
    docker run --rm "${image}" python -c '
import importlib.util
from recsys_inference_api.app import app
assert app.title == "RecSys Inference API"
for module in ("feast", "redis", "psycopg"):
    assert importlib.util.find_spec(module) is None, module
'
    ;;
  *)
    printf 'unsupported serving image: %s\n' "${service}" >&2
    exit 2
    ;;
esac

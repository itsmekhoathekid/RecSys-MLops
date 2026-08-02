#!/usr/bin/env bash
set -euo pipefail

image="${1:?unified Spark image is required}"
platform="${DOCKER_PLATFORM:-linux/amd64}"

docker run --rm \
  --platform "${platform}" \
  --entrypoint bash \
  "${image}" -lc '
set -euo pipefail

python -c "
from importlib.metadata import distributions

import generator_config
import ingest.batch_lakehouse_ingestion
import features.spark.dp2_silver_gold_entrypoint
import features.spark.dp3_offline_feature_entrypoint
import cli.prepare_bst_training_data
import cli.create_hudi_savepoint
import pyspark
import training.train
import torch
from sync_silver import AnalyticsSyncConfig, spark_catalog_conf

config = AnalyticsSyncConfig.from_env()
catalog = spark_catalog_conf(config)
assert catalog[f\"spark.sql.catalog.{config.target_catalog}.type\"] == \"jdbc\"
assert torch.__version__.endswith(\"+cpu\")
assert not torch.cuda.is_available()
runtime_packages = {
    dist.metadata[\"Name\"].lower()
    for dist in distributions()
    if dist.metadata[\"Name\"]
}
gpu_packages = {
    name
    for name in runtime_packages
    if name.startswith((\"cuda-\", \"nvidia-\")) or name == \"triton\"
}
assert not gpu_packages, sorted(gpu_packages)
assert \"pyspark\" not in runtime_packages
assert \"ray\" not in runtime_packages
assert pyspark.__version__ == \"3.5.8\"
"

python /opt/recsys/apps/data-platform/src/features/spark/dp3_offline_feature_entrypoint.py \
  --help >/dev/null
python /opt/recsys/apps/ml-system/src/cli/prepare_bst_training_data.py \
  --help >/dev/null

for jar in \
  iceberg-spark-runtime-3.5_2.12-1.7.1.jar \
  hudi-spark3.5-bundle_2.12-1.2.0.jar \
  hadoop-aws-3.3.4.jar \
  aws-java-sdk-bundle-1.12.262.jar \
  netty-codec-http2-4.1.136.Final.jar \
  postgresql-42.7.12.jar
do
  test -f "/opt/spark/jars/${jar}"
done

test ! -e /opt/spark/jars/netty-codec-http2-4.1.96.Final.jar
test ! -e /opt/venv/bin/pip

test "${PYSPARK_PYTHON}" = /opt/venv/bin/python
test "${PYSPARK_DRIVER_PYTHON}" = /opt/venv/bin/python
test -f /opt/recsys/configs/data-platform/generator/e2e-2k.yaml
test -f /opt/recsys/configs/data-platform/spark/dp2.yaml
test -f /opt/recsys/configs/data-platform/spark/dp3.yaml
test -f /opt/recsys/configs/ml-system/training/bst.yaml
test ! -e /opt/recsys/apps/ml-system/src/cli/run_feature_engineering.py
'

printf 'unified Spark image smoke passed: %s\n' "${image}"

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
import generator_config
import features.spark.dp3_offline_feature_entrypoint
import cli.prepare_bst_training_data
import cli.create_hudi_savepoint
from sync_silver import AnalyticsSyncConfig, spark_catalog_conf

config = AnalyticsSyncConfig.from_env()
catalog = spark_catalog_conf(config)
assert catalog[f\"spark.sql.catalog.{config.target_catalog}.type\"] == \"jdbc\"
"

python /opt/recsys/apps/data-platform/src/features/spark/dp3_offline_feature_entrypoint.py \
  --help >/dev/null
python /opt/recsys/apps/ml-system/src/cli/prepare_bst_training_data.py \
  --help >/dev/null

for jar in \
  iceberg-spark-runtime-3.5_2.12-1.7.1.jar \
  hudi-spark3.5-bundle_2.12-1.0.2.jar \
  hadoop-aws-3.3.4.jar \
  aws-java-sdk-bundle-1.12.262.jar \
  postgresql-42.7.7.jar
do
  test -f "/opt/spark/jars/${jar}"
done

test "${PYSPARK_PYTHON}" = /opt/venv/bin/python
test "${PYSPARK_DRIVER_PYTHON}" = /opt/venv/bin/python
test ! -e /opt/recsys/apps/ml-system/src/cli/run_feature_engineering.py
'

printf 'unified Spark image smoke passed: %s\n' "${image}"

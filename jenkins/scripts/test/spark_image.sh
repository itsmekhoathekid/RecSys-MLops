#!/usr/bin/env bash
set -euo pipefail

image="${1:?Spark image is required}"
profile="${2:?Spark profile is required: data, analytics, or ml}"
platform="${DOCKER_PLATFORM:-linux/amd64}"

docker run --rm --platform "${platform}" --entrypoint bash "${image}" -lc "
set -euo pipefail
for jar in \
  iceberg-spark-runtime-3.5_2.12-1.7.1.jar \
  hudi-spark3.5-bundle_2.12-1.2.0.jar \
  hadoop-aws-3.3.4.jar \
  aws-java-sdk-bundle-1.12.262.jar \
  netty-codec-http2-4.1.136.Final.jar \
  postgresql-42.7.12.jar
do
  test -f \"/opt/spark/jars/\${jar}\"
done
test ! -e /opt/spark/jars/netty-codec-http2-4.1.96.Final.jar
test ! -e /opt/venv/bin/pip
case '${profile}' in
  data)
    python -c 'import generator_config; import features.spark.dp2_silver_gold_entrypoint; import features.spark.dp3_offline_feature_entrypoint; import pyspark; assert pyspark.__version__ == \"3.5.8\"'
    test ! -e /opt/recsys/apps/ml-system
    test ! -e /opt/recsys/apps/analytics
    ;;
  analytics)
    python -c 'from sync_silver import AnalyticsSyncConfig, spark_catalog_conf; config=AnalyticsSyncConfig.from_env(); assert spark_catalog_conf(config)'
    test ! -e /opt/recsys/apps/ml-system
    test ! -e /opt/recsys/apps/data-platform/src
    ;;
  ml)
    python -c 'import cli.prepare_bst_training_data; import cli.create_hudi_savepoint; import pandas; import pyarrow'
    test ! -e /opt/recsys/apps/ml-system/src/training
    test ! -e /opt/venv/bin/ray
    test ! -e /opt/venv/bin/mlflow
    test ! -e /opt/recsys/apps/analytics
    test ! -e /opt/recsys/apps/data-platform/data-generator
    ;;
  *) exit 2 ;;
esac
"

printf 'Spark %s image smoke passed: %s\n' "${profile}" "${image}"

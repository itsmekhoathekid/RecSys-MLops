# DataHub SDK lineage cutover

This migration removes legacy OpenLineage DataJobs only after the replacement
Airflow-plugin and SDK DataJobs have lineage plus run evidence. It never targets
Datasets, assertions, contracts, or the reused DP1/DP2/DP3 DataFlows.

Run from the repository root with the data-platform source on `PYTHONPATH`:

```bash
export PYTHONPATH="$PWD/apps/data-platform/src:$PWD"
export DATAHUB_GMS_URL="http://datahub-datahub-gms.datahub.svc.cluster.local:8080"

# Preview and write the recoverable manifest. This is the default mode.
python ops/migrations/datahub-sdk-lineage-cutover/cutover.py

# Apply only after DP1, DP2, DP3, CDC, and both Flink jobs have new run evidence.
python ops/migrations/datahub-sdk-lineage-cutover/cutover.py \
  --apply --confirm-cutover

# Restore every entity recorded by the manifest.
python ops/migrations/datahub-sdk-lineage-cutover/cutover.py --restore
```

If GMS authentication is enabled, also set `DATAHUB_GMS_TOKEN` or
`DATAHUB_TOKEN`. Keep the generated manifest with the deployment evidence until
the cutover has been accepted.

## Runtime split and kill switch

- Airflow `2.9.3` contains `acryl-datahub-airflow-plugin==1.6.0` and its compatible SDK only.
- The image applies the exact-version `patch_datahub_plugin_no_openlineage.py` compatibility patch. Plugin `1.6.0` imports optional extractor modules eagerly even when extractors are disabled; the patch lazy-loads them so declared iolets work without installing an OpenLineage package. The build fails closed if the upstream source shape changes.
- Spark, Flink, feature-store, and ingestion environments retain `acryl-datahub==1.6.0.17`; OpenLineage is not installed.
- Airflow-created pods receive `RUNTIME_LINEAGE_ENABLED=false`. Set the same value to suppress direct SDK emission while diagnosing CDC/Flink, but remember that doing so intentionally creates an observability gap for those non-Airflow jobs.
- Scheduler and webserver must both expose `AIRFLOW_CONN_DATAHUB_REST_DEFAULT` and identical `AIRFLOW__DATAHUB__*` settings.

## Acceptance evidence before apply

1. Run DP1, DP2, and DP3 and verify their new `task_id` DataJobs have successful process instances and the expected table edges.
2. Register CDC and run a bounded Flink job; verify their flows use `kafka-connect` and `flink`, not `airflow`.
3. Inject one controlled failure and verify a `FAILURE` process instance with no false successful run.
4. Save fresh screenshots and the dry-run manifest. Historical OpenLineage screenshots are not cutover evidence.
5. Review the manifest: it may contain legacy process instances/jobs and the obsolete CDC/Flink Airflow flows, but never Datasets, assertions, contracts, or the reused DP1-DP3 Airflow flows.

## Troubleshooting

- If DAG import fails, run `airflow dags list-import-errors`, `pip check`, and `airflow plugins`; confirm the listener is importable from the pinned Airflow image.
- If runs appear without edges, inspect the DAG task's `inlets`/`outlets`, verify `materialize_iolets=true`, and compare each URN with `governance_catalog.py`.
- If duplicate Airflow runs appear, verify `RUNTIME_LINEAGE_ENABLED=false` reached Kubernetes pods, Spark drivers/executors, and analytics sync.
- If CDC/Flink emission fails, validate `DATAHUB_GMS_URL`, token, timeout/retry settings, and strict mode. Non-strict mode logs and continues; strict mode raises.
- Restore is idempotent: `--restore` emits `Status.removed=false` for every entity in the saved manifest.

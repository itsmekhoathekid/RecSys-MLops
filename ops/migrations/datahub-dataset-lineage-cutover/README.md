# DataHub static dataset-lineage cutover

This operational migration soft-deletes the old Airflow, Kafka Connect, Flink,
legacy assertion, removed-dataset contract, CDC, and streaming catalog entities
after the replacement 44-dataset/40-edge/44-assertion/44-contract catalog has
been verified. Contracts already attached to the current datasets are reused
and are never cleanup targets. The default mode is a dry run and writes a
recoverable manifest. The historical `recsys_rag_item_reconciliation` flow is
also removed because reconciliation now runs through `recsys_rag_item_index`.

```bash
export PYTHONPATH="$PWD/apps/data-platform/src:$PWD"
export DATAHUB_GMS_URL="http://datahub-datahub-gms.datahub.svc.cluster.local:8080"

python ops/migrations/datahub-dataset-lineage-cutover/cutover.py
python ops/migrations/datahub-dataset-lineage-cutover/cutover.py --apply --confirm-cutover
python ops/migrations/datahub-dataset-lineage-cutover/cutover.py --restore
```

Keep `.ci-deploy/datahub-dataset-lineage-cutover.json` with the Jenkins build
artifacts until the cutover has been accepted. Restore returns every record to
the `removed` state captured before apply.

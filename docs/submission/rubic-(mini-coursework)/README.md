# Mini-Coursework Proof Index

This folder contains the proof documents for the mini-coursework rubric in `docs/xlsx/Coursework Tracking (Public).xlsx`, sheet `rubic (mini-coursework)`.

| Rubric area | Proof document | What to capture/check |
|---|---|---|
| README and high-level deployable diagram | [README.md](../../../README.md) | Business domain, repo structure, table of contents, deployable-unit diagrams. |
| Docker and Docker Compose | [docker.md](docker.md) | Dockerfile optimization notes, Cloud Build/compose commands, image proof. |
| Data generator | [data_generator.md](data_generator.md) | Skew, high cardinality, schema evolution, duplicates, burst, late arrival, config, stored raw data. |
| Processing jobs | [processing_jobs.md](processing_jobs.md) | Spark baseline/optimized, Flink baseline/optimized, Spark UI/Flink UI, Airflow integration. |
| Data storage optimization | [data_storage.md](data_storage.md) | DP1 Bronze and DP2 Silver Iceberg compaction, clustering, write properties, manifests, and before/after evidence. |
| Data pipeline orchestration | [data_pipeline_orchestration.md](data_pipeline_orchestration.md) | Six operational Airflow DAGs, DP1/DP2/DP3 step-by-step flow, source references, commands, and task logs. |
| Data governance | [data_governance.md](data_governance.md) | DataHub DP1/DP2/DP3 lineage, validation, data contracts. |
| Schema design | [schema_design.md](schema_design.md) | Bronze/silver/gold tables, SCD2 dimension, feature table timestamp columns, naming convention. |
| Novel ideas | [novel_ideas.md](novel_ideas.md) | DataHub governance as product catalog and drift-to-retrain loop. |

## One-Shot Runtime Checks

Use these commands before capturing screenshots:

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

kubectl get pods -n recsys-dataflow
kubectl get pods -n datahub
kubectl get deploy -n recsys-dataflow
kubectl get svc -n recsys-dataflow
```

Useful UI port-forwards:

```bash
kubectl port-forward -n recsys-dataflow svc/airflow-webserver 8080:8080
kubectl port-forward -n recsys-dataflow svc/flink-jobmanager 8082:8081
kubectl port-forward -n datahub svc/datahub-datahub-frontend 9002:9002
```

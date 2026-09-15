# RAG five-stage Airflow deployment — 2026-09-06

The new DAG is deployed on GKE through `recsys-airflow` Helm revision 15. The Airflow UI's serialized graph contains exactly the five documented tasks. Run `manual__rag_five_stages_20260906T063100` finished **success** at **2026-09-06 07:46:43 UTC / 14:46:43 Asia/Ho_Chi_Minh**, with all five tasks successful.

## Deployment

- Namespace: `recsys-dataflow`
- Image: `asia-southeast1-docker.pkg.dev/recsys-mlops-506406/recsys/recsys-airflow@sha256:221b1f10eabbb603fd28fb205c606c53d85583eb80cd00a0d3fa5ac2032f77a0`
- Previous Helm revision: 14. The new image derives from the deployed Airflow image with only `recsys_rag_item_index.py` replaced.
- Scheduler and webserver rollout completed. The deployed DAG checksum matches the workspace.
- `airflow.ragItemSourceRunId=auto` replaces the stale shared default whose canonical manifest is missing. DAG run configuration still supports selecting an explicit source.

## Verified run

- Airflow run: `manual__rag_five_stages_20260906T063100`
- Logical date: `2026-09-06T06:31:09Z`
- Pipeline run: `rag-20260906T063109`
- Resolved source: `rag-source-quota-fallback96-20260823`

| Task | Final status | Successful attempt duration |
|---|---|---|
| `semantic_chunk_items` | success | 362.3 s |
| `embed_item_chunks` | success | 1874.8 s |
| `incremental_upsert_index` | success | 26.5 s |
| `validate_and_publish_index` | success | 43.6 s |
| `publish_datahub_validation` | success | 16.9 s |

The first four tasks succeeded on their first attempt. DataHub publication succeeded on attempt 5 after its infrastructure dependencies were recovered; only that task was cleared for retry. Airflow resets the DagRun start timestamp during clearing, so the final run start timestamp is not the start of the original chunking task.

Independent artifact verification passed: 96 canonical products, 576 complete chunks, 576 complete normalized 384-dimensional vectors, zero artifact failures, a published green-slot index with successful retrieval smoke, and an active pointer referencing this pipeline run. The DataHub publisher returned `published: true`, `success: 6`, `failure: 0`, `error: 0`, and no report read errors.

## Infrastructure recovery

GMS, MySQL, OpenSearch, Kafka, and ZooKeeper were all at zero replicas before recovery. Their desired replicas are now one and all are Ready; the existing PVCs were reused. DataHub frontend remains at zero replicas. No node pool capacity was added, and Kafka Connect, Flink, and realtime producers remain stopped.

With user approval, GMS/MySQL/OpenSearch CPU requests were set to 50m per application and 25m per Istio sidecar. Memory and application CPU limits were retained. The workloads received the existing ML-node toleration. GMS probes use `/config`, following the repository recovery script. GMS temporarily used Recreate while it had no Ready endpoint to unblock a stalled rollout, then returned to RollingUpdate after recovery.

Kafka initially could not start because its stored cluster ID differed from the newly initialized ZooKeeper metadata. Read-only inventory found 21 existing topics and 124 partitions, including their original topic IDs. Before any metadata repair, Kafka was stopped and both Kafka and ZooKeeper data were backed up to:

`s3://recsys-lakehouse/ops-recovery/kafka/20260906/rag-five-stages/kafka-zookeeper-before-recovery.tar.gz`

Backup SHA-256: `49ad8c63e80310f53cb623f3a47dc8ede743fee93cb6fabb27245819b8228331` (3,029,712 bytes).

A version-checked ZooKeeper transaction restored the original cluster ID and all 21 topic assignments from on-disk topic IDs and partition IDs, using the [Apache Kafka 3.5 TopicZNode format](https://github.com/apache/kafka/blob/3.5.0/core/src/main/scala/kafka/zk/ZkData.scala). Broker logs were retained. Kafka subsequently loaded persisted consumer offsets and passed a broker API check. Topic descriptions verified all 21 original topic IDs and 124 partitions.

ZooKeeper had no surviving topic configuration overrides. Compaction was configured for `__consumer_offsets` and the three Kafka Connect internal topics; other topics use the existing broker defaults. The backup and recovery plan are retained for audit and further recovery if needed.

## Evidence

- [Deployment metadata](evidence/rag-five-stages-2026-09-06/deployment.json)
- [Helm upgrade result](evidence/rag-five-stages-2026-09-06/helm-upgrade.log)
- [Serialized five-task graph](evidence/rag-five-stages-2026-09-06/serialized-graph.json)
- [Final Airflow task states](evidence/rag-five-stages-2026-09-06/run-status.json)
- [Independent artifact verification](evidence/rag-five-stages-2026-09-06/artifact-verification.log)
- [Successful DataHub publication](evidence/rag-five-stages-2026-09-06/datahub-publication.log)
- [Kafka backup verification](evidence/rag-five-stages-2026-09-06/kafka-backup.json)
- [Kafka metadata recovery](evidence/rag-five-stages-2026-09-06/kafka-metadata-recovery.json)
- [Kafka topic ID verification](evidence/rag-five-stages-2026-09-06/kafka-topic-verification.json)

Local checks: 13 focused tests and Ruff passed; the graphify code graph was updated after code/config changes.

# Data Governance: Static Dataset Lineage

The RecSys platform uses DataHub as a catalog for batch datasets and logical
table-level lineage. It deliberately does not use DataHub as a scheduler run,
CDC, Flink, or streaming execution tracker.

## Repository design

The governance implementation has four small responsibilities:

- [`governance_catalog.py`](../../../apps/data-platform/src/metadata/governance_catalog.py)
  declares five Data Products, 44 datasets, schemas, keys, tags, and 40 exact
  upstream relationships. It contains no network code.
- [`governance_schemas.py`](../../../apps/data-platform/src/metadata/governance_schemas.py)
  contains field-level Bronze, Silver, feature, RAG, and analytics schemas.
- [`datahub_client.py`](../../../apps/data-platform/src/metadata/datahub_client.py)
  adapts the catalog to the high-level `DataHubClient`. Domain and Data Product
  operations use the graph client only where the high-level SDK has no complete
  equivalent.
- [`sync_datahub_catalog.py`](../../../apps/data-platform/src/metadata/sync_datahub_catalog.py)
  provides local verification, production synchronization, remote verification,
  and Pushgateway metrics.

Spark, Flink, Airflow, Kafka Connect, analytics, and RAG runtime code do not
import this package. Pipeline availability therefore does not depend on DataHub.

## Governed graph

| Product | Datasets and lineage |
|---|---|
| DP1 | Ten Bronze roots |
| DP2 | Exact Bronze-to-Silver mappings from the Spark transformations |
| DP3 | Silver-to-Iceberg features, matching PostgreSQL exports, and Feast Redis materialization |
| RAG_ITEMS | Documents → chunks → embeddings → Milvus blue/green → active pointer |
| ANALYTICS | Six Silver and two Bronze inputs → matching analytics staging tables |

CDC source PostgreSQL datasets, Kafka topics, Flink jobs, Airflow jobs, process
instances, custom assertions, and Data Contracts are intentionally absent.
Local data validations still fail their Airflow tasks when checks fail, but they
do not publish assertion results to DataHub.

## Synchronization and verification

CI validates the catalog without contacting DataHub:

```bash
PYTHONPATH=apps/data-platform/src \
python -c 'from metadata.governance_catalog import catalog_products, validate_catalog; print(validate_catalog(catalog_products()))'
```

The expected summary is five Data Products, 44 datasets, and 40 direct lineage
edges. Jenkins deploys the immutable data-ingestion image as a one-shot catalog
Job and uses `--strict`, so an SDK or remote-verification failure fails the
release action. The post-deploy check runs the same CLI with `--verify-only`,
which reads DataHub without upserting the catalog.

Every dataset upsert contains its complete desired upstream set. This reconciles
removed edges instead of accumulating stale lineage through additive API calls.

## Operational cutover

The reversible cleanup is documented in
[`datahub-dataset-lineage-cutover`](../../../ops/migrations/datahub-dataset-lineage-cutover/README.md).
It defaults to dry-run, archives the manifest, verifies the replacement catalog,
and then soft-deletes legacy jobs, process instances, CDC/streaming assets,
assertions, and contracts only after explicit approval. Restore replays the
pre-cutover `removed` state recorded for every entity.

## Observability

The DataHub dashboard reports catalog-sync success, timestamp, dataset count,
Data Product presence, and static lineage-edge count. There is no job-count
metric because this repository no longer manages DataHub jobs.

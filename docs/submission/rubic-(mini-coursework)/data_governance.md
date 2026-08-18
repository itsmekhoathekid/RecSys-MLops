# Data Governance: Static Lineage, Assertions, and Contracts

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
  adapts datasets and tags to the high-level `DataHubClient`, uses the official
  graph helpers for externally evaluated CUSTOM assertions, and uses the
  DataHub GraphQL mutation for Data Contracts.
- [`sync_datahub_catalog.py`](../../../apps/data-platform/src/metadata/sync_datahub_catalog.py)
  provides local verification, production synchronization, remote verification,
  and Pushgateway metrics.
- [`report_io.py`](../../../apps/data-platform/src/validate/report_io.py) persists
  versioned local validation evidence, while
  [`publish_datahub_validation.py`](../../../apps/data-platform/src/metadata/publish_datahub_validation.py)
  publishes only the resulting SUCCESS, FAILURE, or ERROR state.

Spark, Flink, Kafka Connect, analytics transformations, and RAG transformations
do not import the DataHub SDK. A separate `all_done` Airflow pod uses the
data-ingestion image to publish validation evidence after each local gate.

## Governed graph

| Product | Datasets and lineage |
|---|---|
| DP1 | Ten Bronze roots |
| DP2 | Exact Bronze-to-Silver mappings from the Spark transformations |
| DP3 | Silver-to-Iceberg features, matching PostgreSQL exports, and Feast Redis materialization |
| RAG_ITEMS | Documents → chunks → embeddings → Milvus blue/green → active pointer |
| ANALYTICS | Six Silver and two Bronze inputs → matching analytics staging tables |

CDC source PostgreSQL datasets, Kafka topics, Flink jobs, Airflow jobs, and
process instances are intentionally absent. Each of the 44 catalog datasets has
one dataset-level CUSTOM assertion and one active Data Contract whose
`dataQuality` section contains exactly that assertion. Airflow publishes the
latest result after executing the local checks; Jenkins never invents pipeline
results.

## Synchronization and verification

CI validates the catalog without contacting DataHub:

```bash
PYTHONPATH=apps/data-platform/src \
python -c 'from metadata.governance_catalog import catalog_products, validate_catalog; print(validate_catalog(catalog_products()))'
```

The expected summary is five Data Products, 44 datasets, 40 direct lineage
edges, 44 CUSTOM assertions, and 44 Data Contracts. Jenkins deploys the
immutable data-ingestion image as a one-shot catalog Job and uses `--strict`, so
an SDK, GraphQL, or remote-verification failure fails the release action. The
post-deploy check runs the same CLI with `--verify-only`; after the data DAGs
have completed, `--verify-only --require-results --strict` additionally proves
all 44 assertions have evaluation evidence.

Every dataset upsert contains its complete desired upstream set. This reconciles
removed edges instead of accumulating stale lineage through additive API calls.

## Operational cutover

The reversible cleanup is documented in
[`datahub-dataset-lineage-cutover`](../../../ops/migrations/datahub-dataset-lineage-cutover/README.md).
It defaults to dry-run, archives the manifest, verifies the replacement catalog,
and then soft-deletes legacy jobs, process instances, CDC/streaming assets, old
assertions, and contracts belonging only to removed datasets after explicit
approval. Contracts attached to the current 44 datasets are reused and
reactivated. Restore replays the
pre-cutover `removed` state recorded for every entity.

## Observability

The DataHub dashboard reports catalog-sync success, timestamp, dataset count,
Data Product presence, static lineage-edge count, assertion count, contract
count, and assertions with results. There is no job-count metric because this
repository no longer manages DataHub jobs.

## UI evidence

For each of DP1, DP2, and DP3, the evidence capture must open a representative
dataset and show its upstream/downstream lineage together with the Data Contract
and latest CUSTOM assertion result:

- `docs/pngs/dp1_datahub_contract.png`
- `docs/pngs/dp2_datahub_contract.png`
- `docs/pngs/dp3_datahub_contract.png`

RAG reconciliation is no longer a separate DAG. Operators trigger
`recsys_rag_item_index` with `params.mode=reconcile`, so incremental and rebuild
executions share the same validation, promotion, and assertion publication path.

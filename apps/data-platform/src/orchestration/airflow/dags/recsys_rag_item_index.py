"""Airflow DAG for incremental or reconciled RAG item index publication.

The DataHub Airflow plugin owns runtime lineage for these pods, so SDK emission is
disabled by ``pod_task``. ``params.mode`` selects safe native upsert or explicit
reconciliation; the publisher can still escalate incremental to reconcile when it
detects stale-delete risk.
"""

from __future__ import annotations

from orchestration.airflow.spark_utils import (
    DAG,
    DATA_INGESTION_IMAGE,
    datetime,
    env_schedule,
    pod_task,
)
from metadata.governance_catalog import (
    RAG_ACTIVE_POINTER_URN,
    RAG_GOLD_EMBEDDINGS_URN,
    RAG_MILVUS_URNS,
    RAG_RAW_DOCUMENTS_URN,
    RAG_SILVER_CHUNKS_URN,
)


CONFIG = "configs/data-platform/rag/pipeline.yaml"
SOURCE_RUN = "{{ params.source_run_id }}"
PIPELINE_RUN = "{{ params.pipeline_run_id }}"


if DAG is not None:
    with DAG(
        dag_id="recsys_rag_item_index",
        start_date=datetime(2026, 1, 1),
        schedule=env_schedule("RAG_ITEM_DAG_SCHEDULE", "manual"),
        catchup=False,
        max_active_runs=1,
        params={
            "source_run_id": "required-canonical-run-id",
            "pipeline_run_id": "required-pipeline-run-id",
            "mode": "incremental",
        },
        tags=["recsys", "rag-items", "feast", "milvus"],
    ) as recsys_rag_item_index:
        semantic_chunk_items = pod_task(
            "semantic_chunk_items",
            DATA_INGESTION_IMAGE,
            f"python -m rag_data.cli chunk-items --config {CONFIG} --source-run-id '{SOURCE_RUN}' --run-id '{PIPELINE_RUN}'",
            inlets=(RAG_RAW_DOCUMENTS_URN,),
            outlets=(RAG_SILVER_CHUNKS_URN,),
        )
        embed_item_chunks = pod_task(
            "embed_item_chunks",
            DATA_INGESTION_IMAGE,
            f"python -m rag_data.cli embed-chunks --config {CONFIG} --run-id '{PIPELINE_RUN}'",
            inlets=(RAG_SILVER_CHUNKS_URN,),
            outlets=(RAG_GOLD_EMBEDDINGS_URN,),
        )
        publish_index = pod_task(
            "incremental_upsert_index",
            DATA_INGESTION_IMAGE,
            f"python -m rag_data.cli publish-index --config {CONFIG} --run-id '{PIPELINE_RUN}' --mode '{{{{ params.mode }}}}'",
            inlets=(RAG_GOLD_EMBEDDINGS_URN,),
            # The physical target is pointer-dependent. Declaring both possible
            # slots keeps static lineage complete without guessing at DAG parse time.
            outlets=RAG_MILVUS_URNS.values(),
        )
        validate_and_publish_index = pod_task(
            "validate_and_publish_index",
            DATA_INGESTION_IMAGE,
            f"python -m rag_data.cli validate-index --config {CONFIG} --run-id '{PIPELINE_RUN}' --promote",
            inlets=RAG_MILVUS_URNS.values(),
            outlets=(RAG_ACTIVE_POINTER_URN,),
        )

        (
            semantic_chunk_items
            >> embed_item_chunks
            >> publish_index
            >> validate_and_publish_index
        )

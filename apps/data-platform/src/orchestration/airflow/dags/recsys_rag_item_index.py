"""Airflow DAG for incremental or reconciled RAG item index publication."""

from __future__ import annotations

import os

from orchestration.airflow.spark_utils import (
    DAG,
    DATA_INGESTION_IMAGE,
    datetime,
    env_schedule,
    pod_task,
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
        is_paused_upon_creation=False,
        params={
            "source_run_id": os.getenv(
                "RAG_ITEM_SOURCE_RUN_ID", "required-canonical-run-id"
            ),
            "pipeline_run_id": os.getenv(
                "RAG_ITEM_PIPELINE_RUN_ID", "required-pipeline-run-id"
            ),
            "mode": "incremental",
        },
        tags=["recsys", "rag-items", "feast", "milvus"],
    ) as recsys_rag_item_index:
        semantic_chunk_items = pod_task(
            "semantic_chunk_items",
            DATA_INGESTION_IMAGE,
            f"python -m rag_data.cli chunk-items --config {CONFIG} --source-run-id '{SOURCE_RUN}' --run-id '{PIPELINE_RUN}'",
        )
        embed_item_chunks = pod_task(
            "embed_item_chunks",
            DATA_INGESTION_IMAGE,
            f"python -m rag_data.cli embed-chunks --config {CONFIG} --run-id '{PIPELINE_RUN}'",
        )
        publish_index = pod_task(
            "incremental_upsert_index",
            DATA_INGESTION_IMAGE,
            f"python -m rag_data.cli publish-index --config {CONFIG} --run-id '{PIPELINE_RUN}' --mode '{{{{ params.mode }}}}'",
        )
        validate_and_publish_index = pod_task(
            "validate_and_publish_index",
            DATA_INGESTION_IMAGE,
            f"python -m rag_data.cli validate-index --config {CONFIG} --run-id '{PIPELINE_RUN}' --promote",
        )

        (
            semantic_chunk_items
            >> embed_item_chunks
            >> publish_index
            >> validate_and_publish_index
        )

"""Airflow DAG for incremental or reconciled RAG item index publication."""

from __future__ import annotations

import os

from orchestration.airflow.spark_utils import (
    DAG,
    DATAHUB_OPS_IMAGE,
    RAG_INDEXER_IMAGE,
    datetime,
    datahub_validation_command,
    env_schedule,
    pod_task,
)


CONFIG = "configs/data-platform/rag/pipeline.yaml"
SOURCE_RUN = "{{ ti.xcom_pull(task_ids='resolve_source') }}"
PIPELINE_RUN = "rag-{{ ts_nodash }}"
REPORT_URI = "s3://recsys-lakehouse/governance-validation/RAG_ITEMS/{{ ts_nodash }}/rag-index.json"
RAG_DATASET_KEYS = (
    "rag.raw_documents",
    "rag.silver_chunks",
    "rag.gold_embeddings",
    "rag.milvus.blue",
    "rag.milvus.green",
    "rag.active_pointer",
)


if DAG is not None:
    with DAG(
        dag_id="recsys_rag_item_index",
        start_date=datetime(2026, 1, 1, tz="Asia/Ho_Chi_Minh"),
        schedule=env_schedule("RAG_ITEM_DAG_SCHEDULE", "30 2 * * *"),
        catchup=False,
        max_active_runs=1,
        is_paused_upon_creation=False,
        params={
            "source_run_id": os.getenv(
                "RAG_ITEM_SOURCE_RUN_ID", "auto"
            ),
            "mode": "incremental",
        },
        tags=["recsys", "rag-items", "feast", "milvus"],
    ) as recsys_rag_item_index:
        resolve_source = pod_task(
            "resolve_source",
            RAG_INDEXER_IMAGE,
            f"python -m rag_data.cli resolve-source --config {CONFIG} "
            "--source-run-id '{{ params.source_run_id }}' "
            "--xcom-output /airflow/xcom/return.json",
            do_xcom_push=True,
        )
        semantic_chunk_items = pod_task(
            "semantic_chunk_items",
            RAG_INDEXER_IMAGE,
            f"python -m rag_data.cli chunk-items --config {CONFIG} --source-run-id '{SOURCE_RUN}' --run-id '{PIPELINE_RUN}'",
        )
        embed_item_chunks = pod_task(
            "embed_item_chunks",
            RAG_INDEXER_IMAGE,
            f"python -m rag_data.cli embed-chunks --config {CONFIG} --run-id '{PIPELINE_RUN}'",
        )
        publish_index = pod_task(
            "incremental_upsert_index",
            RAG_INDEXER_IMAGE,
            f"python -m rag_data.cli publish-index --config {CONFIG} --run-id '{PIPELINE_RUN}' --mode '{{{{ params.mode }}}}'",
        )
        validate_and_publish_index = pod_task(
            "validate_and_publish_index",
            RAG_INDEXER_IMAGE,
            f"python -m rag_data.cli validate-index --config {CONFIG} --run-id '{PIPELINE_RUN}' "
            f"--promote --report-uri '{REPORT_URI}'",
        )
        verify_active_index = pod_task(
            "verify_active_index",
            RAG_INDEXER_IMAGE,
            f"python -m rag_data.cli verify-active-index --config {CONFIG} --run-id '{PIPELINE_RUN}' || "
            f"{{ python -m rag_data.cli rollback-index --config {CONFIG} --run-id '{PIPELINE_RUN}'; exit 1; }}",
        )
        publish_datahub_validation = pod_task(
            "publish_datahub_validation",
            DATAHUB_OPS_IMAGE,
            datahub_validation_command("RAG_ITEMS", (REPORT_URI,), RAG_DATASET_KEYS),
            trigger_rule="all_success",
            retries=2,
        )

        (
            resolve_source
            >> semantic_chunk_items
            >> embed_item_chunks
            >> publish_index
            >> validate_and_publish_index
            >> verify_active_index
            >> publish_datahub_validation
        )

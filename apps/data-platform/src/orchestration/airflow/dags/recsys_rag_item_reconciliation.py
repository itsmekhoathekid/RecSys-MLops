"""Manual reconciliation DAG alias for the RAG index blue/green rebuild path."""

from __future__ import annotations

from orchestration.airflow.spark_utils import DAG, DATA_INGESTION_IMAGE, datetime, pod_task
from metadata.governance_catalog import (
    RAG_ACTIVE_POINTER_URN,
    RAG_GOLD_EMBEDDINGS_URN,
    RAG_MILVUS_URNS,
)


if DAG is not None:
    with DAG(
        dag_id="recsys_rag_item_reconciliation",
        start_date=datetime(2026, 1, 1),
        schedule=None,
        catchup=False,
        max_active_runs=1,
        params={"pipeline_run_id": "required-pipeline-run-id"},
        tags=["recsys", "rag-items", "reconciliation", "milvus"],
    ) as recsys_rag_item_reconciliation:
        reconcile_vector_index = pod_task(
            "reconcile_vector_index",
            DATA_INGESTION_IMAGE,
            "python -m rag_data.cli publish-index --config configs/data-platform/rag/pipeline.yaml "
            "--run-id '{{ params.pipeline_run_id }}' --mode reconcile",
            inlets=(RAG_GOLD_EMBEDDINGS_URN,),
            outlets=RAG_MILVUS_URNS.values(),
        )
        validate_and_publish_index = pod_task(
            "validate_and_publish_index",
            DATA_INGESTION_IMAGE,
            "python -m rag_data.cli validate-index --config configs/data-platform/rag/pipeline.yaml "
            "--run-id '{{ params.pipeline_run_id }}' --promote",
            inlets=RAG_MILVUS_URNS.values(),
            outlets=(RAG_ACTIVE_POINTER_URN,),
        )
        reconcile_vector_index >> validate_and_publish_index

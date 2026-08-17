"""Public CLI for canonical generation, chunking, embedding, and index lifecycle.

Commands are idempotent through run-scoped manifests and checkpoints. Direct
invocations emit strict DataHub runtime lineage; Airflow pods disable SDK
emission because their inlets/outlets already describe the same run.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Sequence

import boto3
import yaml

from rag_data.catalog_mapping import CatalogMapping
from rag_data.artifact_storage import RagArtifactStore
from rag_data.embedding import OnnxE5Encoder, sha256_file
from rag_data.feast_publisher import FeastMilvusPublisher
from rag_data.generator import ItemMetadataGenerator, PostgresProductSource
from rag_data.index_lifecycle import (
    publish_index,
    rollback_active_pointer,
    validate_and_promote_index,
)
from rag_data.orcarouter_client import OrcaRouterClient
from rag_data.pipeline import chunk_canonical_items, embed_item_chunks
from rag_data.semantic_chunker import ChunkerConfig
from rag_data.storage import CompletedRunError, MinioRunStorage
from metadata.governance_catalog import (
    RAG_ACTIVE_POINTER_URN,
    RAG_GOLD_EMBEDDINGS_URN,
    RAG_MILVUS_URNS,
    RAG_RAW_DOCUMENTS_URN,
    RAG_SILVER_CHUNKS_URN,
    RAG_SOURCE_PRODUCTS_URN,
)
from metadata.runtime_lineage import RuntimeLineageRecorder


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a YAML mapping or reject a malformed pipeline configuration."""

    with Path(path).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError("RAG item metadata config must be a YAML mapping")
    return config


def build_parser() -> argparse.ArgumentParser:
    """Build the stable command-line contract used by Airflow and Helm Jobs."""

    parser = argparse.ArgumentParser(
        description="Generate canonical RAG item documents"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate-items", help="Generate item metadata")
    generate.add_argument("--config", required=True)
    generate.add_argument("--run-id", required=True)
    generate.add_argument(
        "--limit", type=int, default=0, help="0 means all active products"
    )
    generate.add_argument("--item-id", type=int, action="append", dest="item_ids")
    generate.add_argument("--checkpoint-every", type=int, default=10)
    generate.add_argument("--workers", type=int, default=1)
    generate.add_argument("--force", action="store_true")

    chunk = subparsers.add_parser(
        "chunk-items", help="Create semantic chunks from a complete canonical run"
    )
    chunk.add_argument("--config", required=True)
    chunk.add_argument("--source-run-id", required=True)
    chunk.add_argument("--run-id", required=True)
    chunk.add_argument("--force", action="store_true")
    chunk.add_argument("--checkpoint-every", type=int, default=10)
    chunk.add_argument(
        "--item-limit",
        type=int,
        default=0,
        help="0 means all canonical items; CD smoke uses an isolated run with 3",
    )

    embed = subparsers.add_parser(
        "embed-chunks", help="Encode a complete silver chunk artifact"
    )
    embed.add_argument("--config", required=True)
    embed.add_argument("--run-id", required=True)
    embed.add_argument("--force", action="store_true")
    embed.add_argument("--checkpoint-every", type=int, default=10)

    publish = subparsers.add_parser(
        "publish-index", help="Write an incremental or reconciled Milvus candidate"
    )
    publish.add_argument("--config", required=True)
    publish.add_argument("--run-id", required=True)
    publish.add_argument("--mode", choices=("incremental", "reconcile"), required=True)

    validate = subparsers.add_parser(
        "validate-index", help="Validate exact candidate contents and optionally promote"
    )
    validate.add_argument("--config", required=True)
    validate.add_argument("--run-id", required=True)
    validate.add_argument("--promote", action="store_true")
    validate.add_argument("--expected-item-count", type=int, default=0)
    rollback = subparsers.add_parser(
        "rollback-index", help="CAS-restore the previous validated active pointer"
    )
    rollback.add_argument("--config", required=True)
    rollback.add_argument("--run-id", required=True)
    return parser


def _s3_client() -> Any:
    endpoint = os.getenv(
        "MINIO_ENDPOINT",
        os.getenv("DATA_PLATFORM_MINIO_ENDPOINT", "http://data-platform-minio:9000"),
    )
    access_key = os.getenv("MINIO_ROOT_USER", os.getenv("AWS_ACCESS_KEY_ID", "minio"))
    secret_key = os.getenv(
        "MINIO_ROOT_PASSWORD", os.getenv("AWS_SECRET_ACCESS_KEY", "minio123")
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )


def generate_items(args: argparse.Namespace) -> int:
    """Generate or resume canonical documents and return a shell status code."""

    if args.limit < 0:
        raise ValueError("--limit must be 0 or greater")
    config = load_config(args.config)
    generation = config["generation"]
    api_key = os.getenv("ORCAROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("ORCAROUTER_API_KEY is required")

    client = OrcaRouterClient(
        api_key=api_key,
        model=generation["model"],
        base_url=generation["base_url"],
        temperature=float(generation["temperature"]),
        max_tokens=int(generation["max_tokens"]),
        max_attempts=int(generation["max_attempts"]),
        timeout_seconds=float(generation["timeout_seconds"]),
    )
    storage = MinioRunStorage(
        client=_s3_client(),
        bucket=os.getenv("LAKE_BUCKET", config["storage"]["bucket"]),
        run_id=args.run_id,
    )
    runner = ItemMetadataGenerator(
        source=PostgresProductSource(),
        content_generator=client,
        mapping=CatalogMapping.from_config(config["catalog"]),
        storage=storage,
        checkpoint_every=args.checkpoint_every,
        workers=args.workers,
    )
    try:
        state = runner.run(
            item_ids=args.item_ids,
            limit=args.limit,
            force=args.force,
        )
    except CompletedRunError as exc:
        print(json.dumps({"status": "skipped", "reason": str(exc)}, ensure_ascii=False))
        return 0

    summary = {
        "run_id": args.run_id,
        "status": state.manifest.status,
        "generated_count": state.manifest.generated_count,
        "failed_count": state.manifest.failed_count,
        "output_uri": f"s3://{storage.bucket}/{storage.prefix}/",
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if state.manifest.status == "complete" else 1


def _artifact_store(config: dict[str, Any]) -> RagArtifactStore:
    """Build the shared S3 artifact adapter without logging credentials."""

    return RagArtifactStore(
        client=_s3_client(),
        bucket=os.getenv("LAKE_BUCKET", config["storage"]["bucket"]),
    )


def _encoder(config: dict[str, Any]) -> tuple[OnnxE5Encoder, str]:
    """Load the image-baked ONNX encoder and verify its configured checksum."""

    embedding = config["embedding"]
    model_dir = Path(os.getenv("RAG_MODEL_DIR", embedding["model_dir"]))
    model_path = model_dir / embedding.get("onnx_file", "model_quantized.onnx")
    checksum = sha256_file(model_path)
    configured_checksum = embedding.get("checksum")
    if configured_checksum and checksum != configured_checksum:
        raise RuntimeError(
            f"Packaged embedding model differs from pinned config: {checksum} != {configured_checksum}"
        )
    manifest_path = model_dir / "model_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("sha256")
        if expected and checksum != expected:
            raise RuntimeError(
                f"Packaged embedding model checksum mismatch: {checksum} != {expected}"
            )
    return (
        OnnxE5Encoder(
            model_dir,
            dimension=int(embedding["dimension"]),
            max_tokens=int(config["chunking"]["max_tokens"]),
        ),
        checksum,
    )


def chunk_items(args: argparse.Namespace) -> int:
    """CLI handler for idempotent canonical-to-silver chunk generation."""

    config = load_config(args.config)
    encoder, checksum = _encoder(config)
    values = config["chunking"]
    manifest = chunk_canonical_items(
        store=_artifact_store(config),
        encoder=encoder,
        source_run_id=args.source_run_id,
        run_id=args.run_id,
        config=ChunkerConfig(
            target_tokens=int(values["target_tokens"]),
            min_tokens=int(values["min_tokens"]),
            max_tokens=int(values["max_tokens"]),
            overlap_tokens=int(values["overlap_tokens"]),
            breakpoint_percentile=float(values["breakpoint_percentile"]),
            version=values["version"],
        ),
        embedding_model=config["embedding"]["model"],
        embedding_revision=config["embedding"]["revision"],
        model_checksum=checksum,
        force=args.force,
        checkpoint_every=args.checkpoint_every,
        item_limit=args.item_limit,
    )
    print(manifest.model_dump_json())
    return 0


def embed_chunks(args: argparse.Namespace) -> int:
    """CLI handler for silver-to-gold normalized ONNX embedding."""

    config = load_config(args.config)
    encoder, _ = _encoder(config)
    manifest = embed_item_chunks(
        store=_artifact_store(config),
        encoder=encoder,
        run_id=args.run_id,
        force=args.force,
        checkpoint_every=args.checkpoint_every,
    )
    print(manifest.model_dump_json())
    return 0


def _publisher(config: dict[str, Any]) -> FeastMilvusPublisher:
    """Create a Feast/Milvus adapter from secret-backed environment values."""

    milvus = config["milvus"]
    host = os.getenv("MILVUS_HOST", milvus["host"])
    port = int(os.getenv("MILVUS_PORT", milvus["port"]))
    return FeastMilvusPublisher(
        repo_path=os.getenv("RAG_FEAST_REPO", config["feast"]["repo_path"]),
        milvus_uri=f"{host}:{port}",
        milvus_token=(
            f"{os.getenv('MILVUS_USERNAME', '')}:{os.getenv('MILVUS_PASSWORD', '')}"
            if os.getenv("MILVUS_USERNAME") and os.getenv("MILVUS_PASSWORD")
            else ""
        ),
    )


def publish_item_index(args: argparse.Namespace) -> int:
    """CLI handler that writes a candidate but never promotes it implicitly."""

    config = load_config(args.config)
    manifest = publish_index(
        store=_artifact_store(config),
        publisher=_publisher(config),
        run_id=args.run_id,
        requested_mode=args.mode,
    )
    print(manifest.model_dump_json())
    return 0


def validate_item_index(args: argparse.Namespace) -> int:
    """CLI handler for candidate validation and explicit CAS promotion."""

    config = load_config(args.config)
    manifest = validate_and_promote_index(
        store=_artifact_store(config),
        publisher=_publisher(config),
        run_id=args.run_id,
        promote=args.promote,
        expected_item_count=args.expected_item_count,
    )
    print(manifest.model_dump_json())
    return 0


def rollback_item_index(args: argparse.Namespace) -> int:
    """CLI handler for conditional active-pointer rollback."""

    config = load_config(args.config)
    pointer = rollback_active_pointer(store=_artifact_store(config))
    print(pointer.model_dump_json())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one CLI command while recording runtime DataHub lineage."""

    args = build_parser().parse_args(argv)
    milvus = set(RAG_MILVUS_URNS.values())
    command_contract = {
        "generate-items": (
            "generate_item_documents",
            {RAG_SOURCE_PRODUCTS_URN},
            {RAG_RAW_DOCUMENTS_URN},
            generate_items,
        ),
        "chunk-items": (
            "semantic_chunk_items",
            {RAG_RAW_DOCUMENTS_URN},
            {RAG_SILVER_CHUNKS_URN},
            chunk_items,
        ),
        "embed-chunks": (
            "embed_item_chunks",
            {RAG_SILVER_CHUNKS_URN},
            {RAG_GOLD_EMBEDDINGS_URN},
            embed_chunks,
        ),
        "publish-index": (
            "reconcile_vector_index"
            if getattr(args, "mode", None) == "reconcile"
            else "incremental_upsert_index",
            {RAG_GOLD_EMBEDDINGS_URN},
            milvus,
            publish_item_index,
        ),
        "validate-index": (
            "validate_and_publish_index",
            milvus,
            {RAG_ACTIVE_POINTER_URN},
            validate_item_index,
        ),
        "rollback-index": (
            "validate_and_publish_index",
            {RAG_ACTIVE_POINTER_URN},
            {RAG_ACTIVE_POINTER_URN},
            rollback_item_index,
        ),
    }
    try:
        job_id, inputs, outputs, handler = command_contract[args.command]
    except KeyError as exc:
        raise ValueError(f"Unsupported command: {args.command}") from exc
    # Airflow pods set RUNTIME_LINEAGE_ENABLED=false because inlets/outlets are
    # emitted by its plugin. Direct CLI and Helm jobs retain strict SDK lineage.
    with RuntimeLineageRecorder(
        "RAG_ITEMS",
        job_id,
        inputs=inputs,
        outputs=outputs,
        run_id=getattr(args, "run_id", "unknown"),
        properties={
            "sourceRunId": str(getattr(args, "source_run_id", "")),
            "mode": str(getattr(args, "mode", "")),
        },
    ) as lineage:
        result = handler(args)
        if result:
            lineage.fail(f"CLI exited with status {result}")
        return result


if __name__ == "__main__":
    raise SystemExit(main())

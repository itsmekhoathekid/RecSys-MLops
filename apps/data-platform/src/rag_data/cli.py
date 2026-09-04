"""Public CLI for canonical generation, chunking, embedding, and index lifecycle."""

from __future__ import annotations

import argparse
import json
import math
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
from validate.report_io import (
    check,
    dataset_result,
    validation_report,
    write_validation_report,
)


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

    resolve = subparsers.add_parser(
        "resolve-source", help="Resolve an explicit or latest complete canonical run"
    )
    resolve.add_argument("--config", required=True)
    resolve.add_argument("--source-run-id", default="auto")
    resolve.add_argument("--xcom-output", default="")

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
        "validate-index",
        help="Validate exact candidate contents and optionally promote",
    )
    validate.add_argument("--config", required=True)
    validate.add_argument("--run-id", required=True)
    validate.add_argument("--promote", action="store_true")
    validate.add_argument("--expected-item-count", type=int, default=0)
    validate.add_argument("--report-uri", default="")
    rollback = subparsers.add_parser(
        "rollback-index", help="CAS-restore the previous validated active pointer"
    )
    rollback.add_argument("--config", required=True)
    rollback.add_argument("--run-id", required=True)
    verify = subparsers.add_parser(
        "verify-active-index", help="Smoke the active pointer after promotion"
    )
    verify.add_argument("--config", required=True)
    verify.add_argument("--run-id", required=True)
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


def resolve_source(args: argparse.Namespace) -> int:
    """Resolve the Airflow input without allowing an incomplete source run."""

    config = load_config(args.config)
    store = _artifact_store(config)
    if args.source_run_id and args.source_run_id != "auto":
        store.load_canonical_items(args.source_run_id)
        run_id = args.source_run_id
    else:
        run_id = store.latest_complete_source_run()
    if args.xcom_output:
        output = Path(args.xcom_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(run_id) + "\n", encoding="utf-8")
    print(run_id)
    return 0


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
    store = _artifact_store(config)
    publisher = _publisher(config)
    try:
        manifest = validate_and_promote_index(
            store=store,
            publisher=publisher,
            run_id=args.run_id,
            promote=args.promote,
            expected_item_count=args.expected_item_count,
        )
        if args.report_uri:
            gold_manifest, embeddings = store.load_embeddings(args.run_id)
            _, chunks = store.load_chunks(args.run_id)
            raw_manifest, documents = store.load_canonical_items(
                gold_manifest.source_run_id
            )
            dimensions_ok = all(
                len(record.embedding) == gold_manifest.embedding_dimension
                and all(math.isfinite(value) for value in record.embedding)
                for record in embeddings
            )
            datasets = {
                "rag.raw_documents": dataset_result(
                    [
                        check(
                            "manifest_count",
                            "SUCCESS"
                            if len(documents) == raw_manifest.generated_count
                            else "FAILURE",
                            raw_manifest.generated_count,
                            len(documents),
                        )
                    ]
                ),
                "rag.silver_chunks": dataset_result(
                    [
                        check(
                            "record_count",
                            "SUCCESS" if chunks else "FAILURE",
                            "> 0",
                            len(chunks),
                        ),
                        check(
                            "chunk_ids_unique",
                            "SUCCESS"
                            if len({item.chunk_id for item in chunks}) == len(chunks)
                            else "FAILURE",
                            len(chunks),
                            len({item.chunk_id for item in chunks}),
                        ),
                    ]
                ),
                "rag.gold_embeddings": dataset_result(
                    [
                        check(
                            "record_count",
                            "SUCCESS" if embeddings else "FAILURE",
                            "> 0",
                            len(embeddings),
                        ),
                        check(
                            "embedding_dimension_and_finite",
                            "SUCCESS" if dimensions_ok else "FAILURE",
                            gold_manifest.embedding_dimension,
                            dimensions_ok,
                        ),
                    ]
                ),
            }
            expected_ids = {record.chunk_id for record in embeddings}
            for slot in ("blue", "green"):
                try:
                    count = publisher.collection_count(slot)
                    ids = publisher.collection_ids(slot)
                    checks = [
                        check("collection_readable", "SUCCESS", ">= 0", count),
                    ]
                    if slot == manifest.slot:
                        checks.append(
                            check(
                                "candidate_ids",
                                "SUCCESS"
                                if count == len(expected_ids) and ids == expected_ids
                                else "FAILURE",
                                len(expected_ids),
                                count,
                            )
                        )
                except Exception as exc:
                    checks = [
                        check("collection_readable", "ERROR", "readable", str(exc))
                    ]
                datasets[f"rag.milvus.{slot}"] = dataset_result(checks)
            pointer = store.load_active_pointer()
            datasets["rag.active_pointer"] = dataset_result(
                [
                    check(
                        "active_pointer",
                        "SUCCESS"
                        if pointer and pointer.pointer.pipeline_run_id == args.run_id
                        else "FAILURE",
                        args.run_id,
                        pointer.pointer.pipeline_run_id if pointer else None,
                    )
                ]
            )
            write_validation_report(
                validation_report(
                    "RAG_ITEMS",
                    args.run_id,
                    datasets,
                    report_uri=args.report_uri,
                ),
                args.report_uri,
            )
    except Exception as exc:
        if args.report_uri:
            keys = (
                "rag.raw_documents",
                "rag.silver_chunks",
                "rag.gold_embeddings",
                "rag.milvus.blue",
                "rag.milvus.green",
                "rag.active_pointer",
            )
            datasets = {
                key: dataset_result(
                    [check("validation_execution", "ERROR", "completed", str(exc))]
                )
                for key in keys
            }
            write_validation_report(
                validation_report(
                    "RAG_ITEMS", args.run_id, datasets, report_uri=args.report_uri
                ),
                args.report_uri,
            )
        raise
    print(manifest.model_dump_json())
    return 0


def rollback_item_index(args: argparse.Namespace) -> int:
    """CLI handler for conditional active-pointer rollback."""

    config = load_config(args.config)
    pointer = rollback_active_pointer(store=_artifact_store(config))
    print(pointer.model_dump_json())
    return 0


def verify_active_index(args: argparse.Namespace) -> int:
    """Verify the newly promoted pointer and its physical retrieval path."""

    config = load_config(args.config)
    store = _artifact_store(config)
    active = store.load_active_pointer()
    if active is None or active.pointer.pipeline_run_id != args.run_id:
        raise RuntimeError("Active RAG pointer does not reference the promoted run")
    _, records = store.load_embeddings(args.run_id)
    if not records or not _publisher(config).smoke_search(
        active.pointer.active_slot, records[0].embedding
    ):
        raise RuntimeError("Active RAG index retrieval smoke failed")
    print(active.pointer.model_dump_json())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one idempotent RAG pipeline command."""

    args = build_parser().parse_args(argv)
    handlers = {
        "generate-items": generate_items,
        "resolve-source": resolve_source,
        "chunk-items": chunk_items,
        "embed-chunks": embed_chunks,
        "publish-index": publish_item_index,
        "validate-index": validate_item_index,
        "rollback-index": rollback_item_index,
        "verify-active-index": verify_active_index,
    }
    try:
        handler = handlers[args.command]
    except KeyError as exc:
        raise ValueError(f"Unsupported command: {args.command}") from exc
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

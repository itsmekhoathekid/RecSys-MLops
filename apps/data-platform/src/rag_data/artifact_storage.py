"""S3/MinIO persistence for silver, gold, and active-index artifacts.

Writes are idempotent for a pipeline run: checkpoints replace the run-scoped
Parquet object and manifest, while a completed manifest protects the run unless
force is explicit. The global active pointer uses an ETag compare-and-swap so two
publishers cannot silently promote different Milvus slots.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any, Iterable, TypeVar

import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError
from pydantic import BaseModel

from rag_data.contracts import CanonicalItemDocument, RunManifest
from rag_data.pipeline_contracts import (
    ActiveIndexPointer,
    ArtifactManifest,
    EmbeddedItemChunk,
    IndexManifest,
    ItemChunk,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


class UpstreamNotCompleteError(RuntimeError):
    """Raised when a command attempts to consume a non-complete manifest."""


class PointerConflictError(RuntimeError):
    """Raised when the active pointer ETag changed during promotion."""


@dataclass(frozen=True)
class VersionedPointer:
    """An active pointer paired with the object version used for CAS."""

    pointer: ActiveIndexPointer
    etag: str


def _json_bytes(model: BaseModel) -> bytes:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _records_to_parquet(records: Iterable[BaseModel]) -> bytes:
    rows = [record.model_dump(mode="json") for record in records]
    table = pa.Table.from_pylist(rows)
    output = io.BytesIO()
    pq.write_table(table, output, compression="zstd")
    return output.getvalue()


class RagArtifactStore:
    """Read and write RAG lake artifacts through an S3-compatible client."""

    def __init__(self, *, client: Any, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def _get(self, key: str) -> tuple[bytes, str] | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NoSuchBucket"}:
                return None
            raise
        return response["Body"].read(), str(response.get("ETag", "")).strip('"')

    def _put(self, key: str, body: bytes, content_type: str, **kwargs: Any) -> str:
        response = self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            **kwargs,
        )
        return str(response.get("ETag", "")).strip('"')

    @staticmethod
    def raw_prefix(source_run_id: str) -> str:
        """Return the canonical-document prefix for a source run."""

        return f"raw/{source_run_id}/rag_item_documents"

    @staticmethod
    def silver_prefix(run_id: str) -> str:
        """Return the semantic-chunk prefix for a pipeline run."""

        return f"silver/{run_id}/rag_item_chunks"

    @staticmethod
    def gold_prefix(run_id: str) -> str:
        """Return the embedding prefix for a pipeline run."""

        return f"gold/{run_id}/rag_item_embeddings"

    @staticmethod
    def active_pointer_key() -> str:
        """Return the singleton blue/green pointer object key."""

        return "gold/rag_item_embeddings/_active/pointer.json"

    def load_canonical_items(
        self, source_run_id: str
    ) -> tuple[RunManifest, list[CanonicalItemDocument]]:
        """Load canonical JSONL only when its source manifest is complete."""

        prefix = self.raw_prefix(source_run_id)
        manifest_value = self._get(f"{prefix}/manifest.json")
        if manifest_value is None:
            raise FileNotFoundError(f"Missing canonical manifest for {source_run_id}")
        manifest = RunManifest.model_validate_json(manifest_value[0])
        if manifest.status != "complete":
            raise UpstreamNotCompleteError(
                f"Canonical run {source_run_id!r} is {manifest.status!r}, not complete"
            )
        item_value = self._get(f"{prefix}/items.jsonl")
        if item_value is None:
            raise FileNotFoundError(f"Missing canonical items for {source_run_id}")
        items = [
            CanonicalItemDocument.model_validate_json(line)
            for line in item_value[0].decode("utf-8").splitlines()
            if line.strip()
        ]
        if len(items) != manifest.generated_count:
            raise ValueError("Canonical item count does not match complete manifest")
        if len({item.item_id for item in items}) != len(items):
            raise ValueError("Canonical document contains duplicate item IDs")
        return manifest, items

    def load_manifest(self, run_id: str, *, zone: str) -> ArtifactManifest | None:
        """Read a silver or gold run manifest, returning None when absent."""

        prefix = self.silver_prefix(run_id) if zone == "silver" else self.gold_prefix(run_id)
        value = self._get(f"{prefix}/manifest.json")
        return ArtifactManifest.model_validate_json(value[0]) if value else None

    def write_chunks(
        self,
        run_id: str,
        chunks: list[ItemChunk],
        manifest: ArtifactManifest,
        failures: list[dict[str, Any]] | None = None,
    ) -> None:
        """Checkpoint silver chunks and status; repeated writes replace this run only."""

        prefix = self.silver_prefix(run_id)
        self._put(
            f"{prefix}/chunks.parquet",
            _records_to_parquet(chunks),
            "application/vnd.apache.parquet",
        )
        self._write_failures(f"{prefix}/failures.jsonl", failures or [])
        self._put(f"{prefix}/manifest.json", _json_bytes(manifest), "application/json")

    def write_embeddings(
        self,
        run_id: str,
        records: list[EmbeddedItemChunk],
        manifest: ArtifactManifest,
        failures: list[dict[str, Any]] | None = None,
    ) -> None:
        """Checkpoint gold embeddings and status; no global pointer is changed."""

        prefix = self.gold_prefix(run_id)
        self._put(
            f"{prefix}/embeddings.parquet",
            _records_to_parquet(records),
            "application/vnd.apache.parquet",
        )
        self._write_failures(f"{prefix}/failures.jsonl", failures or [])
        self._put(f"{prefix}/manifest.json", _json_bytes(manifest), "application/json")

    def _write_failures(self, key: str, failures: list[dict[str, Any]]) -> None:
        lines = [
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
            for value in failures
        ]
        self._put(
            key,
            ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"),
            "application/x-ndjson",
        )

    def _load_parquet(self, key: str, model: type[ModelT]) -> list[ModelT]:
        value = self._get(key)
        if value is None:
            raise FileNotFoundError(f"Missing artifact: s3://{self.bucket}/{key}")
        rows = pq.read_table(io.BytesIO(value[0])).to_pylist()
        return [model.model_validate(row) for row in rows]

    def load_chunks(self, run_id: str) -> tuple[ArtifactManifest, list[ItemChunk]]:
        """Load a complete silver artifact and validate its recorded count."""

        manifest = self.load_manifest(run_id, zone="silver")
        if manifest is None or manifest.status != "complete":
            raise UpstreamNotCompleteError(f"Silver run {run_id!r} is not complete")
        chunks = self._load_parquet(
            f"{self.silver_prefix(run_id)}/chunks.parquet", ItemChunk
        )
        if len(chunks) != manifest.record_count:
            raise ValueError("Silver Parquet count does not match manifest")
        return manifest, chunks

    def load_partial_chunks(self, run_id: str) -> list[ItemChunk]:
        """Load any checkpointed silver records, returning an empty list if absent."""

        key = f"{self.silver_prefix(run_id)}/chunks.parquet"
        if self._get(key) is None:
            return []
        return self._load_parquet(key, ItemChunk)

    def load_embeddings(
        self, run_id: str
    ) -> tuple[ArtifactManifest, list[EmbeddedItemChunk]]:
        """Load a complete gold artifact and validate its recorded count."""

        manifest = self.load_manifest(run_id, zone="gold")
        if manifest is None or manifest.status != "complete":
            raise UpstreamNotCompleteError(f"Gold run {run_id!r} is not complete")
        records = self._load_parquet(
            f"{self.gold_prefix(run_id)}/embeddings.parquet", EmbeddedItemChunk
        )
        if len(records) != manifest.record_count:
            raise ValueError("Gold Parquet count does not match manifest")
        return manifest, records

    def load_partial_embeddings(self, run_id: str) -> list[EmbeddedItemChunk]:
        """Load any checkpointed gold records, returning an empty list if absent."""

        key = f"{self.gold_prefix(run_id)}/embeddings.parquet"
        if self._get(key) is None:
            return []
        return self._load_parquet(key, EmbeddedItemChunk)

    def write_index_manifest(self, run_id: str, manifest: IndexManifest) -> None:
        """Persist candidate validation evidence beside the gold artifact."""

        self._put(
            f"{self.gold_prefix(run_id)}/index_manifest.json",
            _json_bytes(manifest),
            "application/json",
        )

    def load_index_manifest(self, run_id: str) -> IndexManifest:
        """Load publication state for a candidate or incremental run."""

        value = self._get(f"{self.gold_prefix(run_id)}/index_manifest.json")
        if value is None:
            raise FileNotFoundError(f"Missing index manifest for {run_id!r}")
        return IndexManifest.model_validate_json(value[0])

    def load_active_pointer(self) -> VersionedPointer | None:
        """Load the current pointer and ETag used for conditional promotion."""

        value = self._get(self.active_pointer_key())
        if value is None:
            return None
        return VersionedPointer(
            pointer=ActiveIndexPointer.model_validate_json(value[0]), etag=value[1]
        )

    def compare_and_swap_pointer(
        self, pointer: ActiveIndexPointer, *, expected_etag: str | None
    ) -> str:
        """Publish an active pointer iff the observed MinIO object is unchanged.

        Side effects:
            Writes the singleton active pointer.
        Raises:
            PointerConflictError: A competing promotion won the ETag race.
        """

        kwargs: dict[str, Any] = {"IfNoneMatch": "*"} if expected_etag is None else {"IfMatch": expected_etag}
        try:
            # This compare-and-swap is the atomic commit for blue/green promotion;
            # index writes alone never make a candidate visible to retrieval.
            return self._put(
                self.active_pointer_key(),
                _json_bytes(pointer),
                "application/json",
                **kwargs,
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"PreconditionFailed", "412", "ConditionalRequestConflict"}:
                raise PointerConflictError(
                    "Active pointer changed during promotion; reload and revalidate"
                ) from exc
            raise

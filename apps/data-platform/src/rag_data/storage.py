"""Checkpoint and resume canonical item generation in the MinIO raw zone.

Every checkpoint rewrites deterministic JSONL plus a manifest beneath one run
prefix. A completed run is immutable by default and incompatible resume
contracts fail before any generated item is changed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from botocore.exceptions import ClientError

from rag_data.contracts import CanonicalItemDocument, FailureRecord, RunManifest


RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CompletedRunError(RuntimeError):
    """Raised when an immutable complete run is started without force."""

    pass


class IncompatibleRunError(RuntimeError):
    """Raised when resume would mix model, prompt, or mapping contracts."""

    pass


@dataclass
class RunState:
    """In-memory canonical items, failures, and the current raw manifest."""

    items: dict[int, CanonicalItemDocument]
    failures: dict[int, FailureRecord]
    manifest: RunManifest | None

    @property
    def completed_item_ids(self) -> set[int]:
        """Return IDs already materialized successfully for resume skipping."""

        return set(self.items)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _json_line(model: Any) -> str:
    return json.dumps(
        model.model_dump(mode="python"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
        allow_nan=False,
    )


class MinioRunStorage:
    """Persist one canonical generation run through an S3-compatible client."""

    dataset_name = "rag_item_documents"

    def __init__(self, *, client: Any, bucket: str, run_id: str) -> None:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError(
                "run_id must be 1-128 characters containing only letters, digits, '.', '_' or '-'"
            )
        self.client = client
        self.bucket = bucket
        self.run_id = run_id
        self.prefix = f"raw/{run_id}/{self.dataset_name}"

    def key(self, filename: str) -> str:
        """Resolve a filename beneath this run's isolated raw-zone prefix."""

        return f"{self.prefix}/{filename}"

    def _read_text(self, filename: str) -> str | None:
        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=self.key(filename)
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NoSuchBucket"}:
                return None
            raise
        return response["Body"].read().decode("utf-8")

    def _put_text(self, filename: str, content: str, content_type: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.key(filename),
            Body=content.encode("utf-8"),
            ContentType=content_type,
        )

    def load(self) -> RunState:
        """Load and validate existing JSONL and manifest checkpoint objects."""

        items: dict[int, CanonicalItemDocument] = {}
        for line in (self._read_text("items.jsonl") or "").splitlines():
            if line.strip():
                item = CanonicalItemDocument.model_validate_json(line)
                items[item.item_id] = item

        failures: dict[int, FailureRecord] = {}
        for line in (self._read_text("failures.jsonl") or "").splitlines():
            if line.strip():
                failure = FailureRecord.model_validate_json(line)
                failures[failure.item_id] = failure

        manifest_text = self._read_text("manifest.json")
        manifest = (
            RunManifest.model_validate_json(manifest_text) if manifest_text else None
        )
        return RunState(items=items, failures=failures, manifest=manifest)

    def start(self, *, manifest: RunManifest, force: bool = False) -> RunState:
        """Start or resume a compatible run and immediately checkpoint running state."""

        state = self.load()
        if state.manifest and state.manifest.status == "complete" and not force:
            raise CompletedRunError(
                f"Run {self.run_id!r} is already complete; pass --force to overwrite it"
            )
        if force:
            state = RunState(items={}, failures={}, manifest=None)
        elif state.manifest:
            existing_contract = (
                state.manifest.model,
                state.manifest.prompt_version,
                state.manifest.catalog_mapping_version,
            )
            requested_contract = (
                manifest.model,
                manifest.prompt_version,
                manifest.catalog_mapping_version,
            )
            if existing_contract != requested_contract:
                raise IncompatibleRunError(
                    f"Run {self.run_id!r} was created with a different model, prompt, "
                    "or catalog mapping; pass --force to overwrite it"
                )
            manifest = state.manifest
        finish_reason_counts = dict(manifest.finish_reason_counts)
        if state.items and not finish_reason_counts:
            finish_reason_counts["unknown"] = len(state.items)
        state.manifest = manifest.refreshed(
            status="running",
            generated_count=len(state.items),
            failed_count=len(state.failures),
            finish_reason_counts=finish_reason_counts,
        )
        self.checkpoint(state)
        return state

    def checkpoint(self, state: RunState) -> None:
        """Atomically replace each run-scoped artifact with sorted stable content."""

        if state.manifest is None:
            raise ValueError("state.manifest is required before checkpoint")
        item_lines = [
            _json_line(state.items[item_id]) for item_id in sorted(state.items)
        ]
        failure_lines = [
            _json_line(state.failures[item_id]) for item_id in sorted(state.failures)
        ]
        self._put_text(
            "items.jsonl",
            "\n".join(item_lines) + ("\n" if item_lines else ""),
            "application/x-ndjson",
        )
        self._put_text(
            "failures.jsonl",
            "\n".join(failure_lines) + ("\n" if failure_lines else ""),
            "application/x-ndjson",
        )
        self._put_text(
            "manifest.json",
            json.dumps(
                state.manifest.model_dump(mode="python"),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                default=_json_default,
            )
            + "\n",
            "application/json",
        )

    def add_items(
        self, state: RunState, items: Iterable[CanonicalItemDocument]
    ) -> None:
        """Upsert successful items into memory and clear older failures."""

        for item in items:
            state.items[item.item_id] = item
            state.failures.pop(item.item_id, None)

    def add_failures(self, state: RunState, failures: Iterable[FailureRecord]) -> None:
        """Record failures unless the same item already succeeded in this run."""

        for failure in failures:
            if failure.item_id not in state.items:
                state.failures[failure.item_id] = failure

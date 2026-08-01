from __future__ import annotations

import pytest

from cli.create_hudi_savepoint import _savepoint_identity, create_savepoint


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def collect(self):
        return self.rows


class FakeSpark:
    def __init__(self, savepoints=()):
        self.savepoints = set(savepoints)
        self.queries: list[str] = []

    def sql(self, query: str):
        self.queries.append(query)
        if query.startswith("CALL show_savepoints"):
            return _Rows([(instant,) for instant in sorted(self.savepoints)])
        if query.startswith("CALL create_savepoint"):
            self.savepoints.add("001")
            return _Rows([(True,)])
        return _Rows([])


class RacingSpark(FakeSpark):
    def sql(self, query: str):
        if query.startswith("CALL create_savepoint"):
            self.queries.append(query)
            self.savepoints.add("001")
            raise RuntimeError("savepoint already exists")
        return super().sql(query)


def _metadata():
    return {
        "hudi": {
            "table": {
                "name": "recsys_features.ml.bst_samples_native_v2",
                "path": "s3a://warehouse/recsys_features/ml/bst_samples_native_v2",
                "hudi_instant": "001",
            }
        }
    }


def test_create_savepoint_creates_and_verifies_requested_instant():
    spark = FakeSpark()

    result = create_savepoint(_metadata(), spark=spark)

    assert result["hudi_instant"] == "001"
    assert result["savepoint_created"] is True
    assert result["already_existed"] is False
    assert sum(query.startswith("CALL create_savepoint") for query in spark.queries) == 1
    assert sum(query.startswith("CALL show_savepoints") for query in spark.queries) == 2


def test_create_savepoint_is_idempotent():
    spark = FakeSpark(savepoints={"001"})

    result = create_savepoint(_metadata(), spark=spark)

    assert result["savepoint_created"] is False
    assert result["already_existed"] is True
    assert not any(query.startswith("CALL create_savepoint") for query in spark.queries)


def test_create_savepoint_treats_concurrent_creation_as_success():
    spark = RacingSpark()

    result = create_savepoint(_metadata(), spark=spark)

    assert result["savepoint_created"] is False
    assert result["already_existed"] is True


def test_savepoint_identity_supports_legacy_commit_time_but_requires_path():
    metadata = {
        "splits": {
            "train": {
                "table": "legacy",
                "table_path": "s3a://warehouse/legacy",
                "commit_time": "009",
            }
        }
    }

    assert _savepoint_identity(metadata) == (
        "legacy",
        "s3a://warehouse/legacy",
        "009",
    )

    with pytest.raises(ValueError, match="table path"):
        _savepoint_identity({"splits": {"train": {"commit_time": "009"}}})

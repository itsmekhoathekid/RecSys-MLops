from __future__ import annotations

from lineage.mlflow_dataset_lineage import dataset_versions, log_dataset_lineage


class FakeData:
    def from_pandas(self, frame, source, name):
        return {"frame": frame, "source": source, "name": name}


class FakeMLflow:
    data = FakeData()

    def __init__(self):
        self.params = {}
        self.inputs = []
        self.dicts = {}

    def log_param(self, key, value):
        self.params[key] = value

    def log_input(self, dataset, context):
        self.inputs.append((context, dataset["name"]))

    def log_dict(self, payload, path):
        self.dicts[path] = payload


def test_mlflow_dataset_lineage_logs_all_contexts():
    metadata = {
        "dataset_run_id": "run-1",
        "feature_service_name": "bst_ranking_v1",
        "feast_registry_path": (
            "postgresql://feature-postgres:5432/feature_store"
            "?schema=feature_store&project=recsys"
        ),
        "entity_input_path": "/labels",
        "schema_hash": "hash",
        "processing_code_version": "abc123",
        "split_strategy": "temporal",
        "versioning_latency_ms": {"total": 22.0},
        "splits": {
            split: {
                "table": "recsys_features.ml.bst_samples_native_v2",
                "table_path": "s3a://warehouse/recsys_features/ml/bst_samples_native_v2",
                "hudi_instant": "001",
                "row_count": row_count,
                "jsonl_path": f"/split/{split}.jsonl",
            }
            for split, row_count in {"train": 3, "val": 1, "test": 1}.items()
        },
    }
    fake = FakeMLflow()

    log_dataset_lineage(
        fake,
        metadata,
        {"train": "training", "val": "validation", "test": ["testing", "evaluation"]},
    )

    assert fake.params["feast_feature_service"] == "bst_ranking_v1"
    assert fake.params["dataset.versioning_latency_ms.total"] == 22.0
    assert fake.params["dataset.training.hudi_table"] == "recsys_features.ml.bst_samples_native_v2"
    assert fake.params["dataset.evaluation.hudi_instant"] == "001"
    assert fake.params["dataset.evaluation.hudi_commit_time"] == "001"
    assert set(fake.inputs) == {
        ("training", "bst_train_samples"),
        ("validation", "bst_val_samples"),
        ("testing", "bst_test_samples"),
        ("evaluation", "bst_test_samples"),
    }
    assert dataset_versions(metadata)["test"]["hudi_instant"] == "001"


def test_dataset_versions_falls_back_to_legacy_instant_fields():
    commit_metadata = {
        "splits": {
            "train": {
                "table": "legacy",
                "commit_time": "002",
            }
        }
    }
    snapshot_metadata = {
        "splits": {
            "train": {
                "table": "legacy",
                "snapshot_id": "003",
            }
        }
    }

    assert dataset_versions(commit_metadata)["train"]["hudi_instant"] == "002"
    assert dataset_versions(snapshot_metadata)["train"]["hudi_instant"] == "003"

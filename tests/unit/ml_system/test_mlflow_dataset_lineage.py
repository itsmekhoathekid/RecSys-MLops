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
        "feast_registry_path": "/repo/data/registry.db",
        "entity_input_path": "/labels",
        "schema_hash": "hash",
        "processing_code_version": "abc123",
        "split_strategy": "temporal",
        "versioning_latency_ms": {"total": 22.0},
        "splits": {
            "train": {"table": "recsys.ml.bst_training_samples", "snapshot_id": "001", "commit_time": "001", "tag": "bst_training_run_1", "row_count": 3, "jsonl_path": "/split/train.jsonl"},
            "val": {"table": "recsys.ml.bst_training_samples", "snapshot_id": "001", "commit_time": "001", "tag": "bst_training_run_1", "row_count": 1, "jsonl_path": "/split/val.jsonl"},
            "test": {"table": "recsys.ml.bst_evaluation_samples", "snapshot_id": "002", "commit_time": "002", "tag": "bst_evaluation_run_1", "row_count": 1, "jsonl_path": "/split/test.jsonl"},
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
    assert fake.params["dataset.training.hudi_table"] == "recsys.ml.bst_training_samples"
    assert fake.params["dataset.evaluation.hudi_commit_time"] == "002"
    assert set(fake.inputs) == {
        ("training", "bst_train_samples"),
        ("validation", "bst_val_samples"),
        ("testing", "bst_test_samples"),
        ("evaluation", "bst_test_samples"),
    }
    assert dataset_versions(metadata)["test"]["tag"] == "bst_evaluation_run_1"

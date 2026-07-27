from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "apps/ml-system/src/kubeflow/delete_pipeline_version.py"
SPEC = importlib.util.spec_from_file_location("delete_pipeline_version", PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class NotFound(Exception):
    status = 404


class FakeClient:
    def __init__(self):
        self.deleted: list[tuple[str, str]] = []

    def delete_pipeline_version(self, resource_id: str):
        self.deleted.append(("version", resource_id))

    def get_pipeline_version(self, resource_id: str):
        raise NotFound(resource_id)

    def delete_pipeline(self, resource_id: str):
        self.deleted.append(("pipeline", resource_id))

    def get_pipeline(self, resource_id: str):
        raise NotFound(resource_id)


def test_kfp_version_compensation_deletes_and_verifies_absence():
    client = FakeClient()
    module.delete_uploaded_resource(
        client,
        {
            "action": "uploaded_pipeline_version",
            "pipeline_version_id": "version-1",
        },
    )
    assert client.deleted == [("version", "version-1")]


def test_kfp_pipeline_compensation_deletes_new_pipeline():
    client = FakeClient()
    module.delete_uploaded_resource(
        client,
        {"action": "uploaded_pipeline", "pipeline_id": "pipeline-1"},
    )
    assert client.deleted == [("pipeline", "pipeline-1")]

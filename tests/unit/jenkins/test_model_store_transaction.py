from __future__ import annotations

import json

from jenkins.python.model_cd import storage


class ClientError(Exception):
    def __init__(self, code: str):
        self.response = {"Error": {"Code": code}}


class FakeClient:
    class exceptions:
        ClientError = ClientError

    def __init__(self):
        self.objects = {("models", "latest.json"): ("v1", '"old"')}
        self.versioned = {"models"}

    def get_bucket_versioning(self, *, Bucket):
        return {"Status": "Enabled" if Bucket in self.versioned else "Suspended"}

    def head_object(self, *, Bucket, Key):
        try:
            version, etag = self.objects[(Bucket, Key)]
        except KeyError as error:
            raise ClientError("404") from error
        return {"VersionId": version, "ETag": etag}

    def copy_object(self, *, Bucket, Key, CopySource):
        assert CopySource["VersionId"] == "v1"
        self.objects[(Bucket, Key)] = ("restored", '"old"')

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def test_model_store_restore_uses_previous_version_and_removes_new_objects(
    tmp_path, monkeypatch
):
    client = FakeClient()
    state_path = tmp_path / "model-store.json"
    state_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "bucket": "models",
                        "key": "latest.json",
                        "existed": True,
                        "versionId": "v1",
                        "etag": '"old"',
                    },
                    {
                        "bucket": "models",
                        "key": "new.bin",
                        "existed": False,
                        "versionId": "",
                        "etag": "",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    client.objects[("models", "latest.json")] = ("v2", '"new"')
    client.objects[("models", "new.bin")] = ("v1", '"new-object"')
    monkeypatch.setattr(storage, "s3_client", lambda: client)

    storage.restore(str(state_path))

    assert client.objects[("models", "latest.json")][1] == '"old"'
    assert ("models", "new.bin") not in client.objects
    assert json.loads(state_path.read_text(encoding="utf-8"))["restored"] is True


def test_model_store_snapshot_fails_closed_without_bucket_versioning(tmp_path):
    client = FakeClient()
    client.versioned.clear()
    try:
        storage.snapshot_object(
            client,
            "models",
            "latest.json",
            str(tmp_path / "state.json"),
        )
    except RuntimeError as error:
        assert "versioning must be Enabled" in str(error)
    else:
        raise AssertionError("snapshot must reject a non-versioned bucket")

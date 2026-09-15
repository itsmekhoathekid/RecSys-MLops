from __future__ import annotations

import json
import re
from copy import deepcopy
from .release import digest

from jenkins.python.model_cd.storage import parse_s3_uri, require_versioning, s3_client


class StateStore:
    """One authoritative, versioned object. Conflicts never trigger blind retries."""

    def __init__(self, uri: str, client=None):
        self.client = client or s3_client()
        self.bucket, self.key = parse_s3_uri(uri)
        require_versioning(self.client, self.bucket)
        members = self.client.meta.service_model.operation_model(
            "PutObject"
        ).input_shape.members
        if not {"IfMatch", "IfNoneMatch"} <= members.keys():
            raise RuntimeError(
                "LLM CD requires boto3/botocore with conditional PutObject support"
            )

    def read(self):
        result = self.client.get_object(Bucket=self.bucket, Key=self.key)
        return json.loads(result["Body"].read()), result["ETag"]

    def write(self, value: dict, etag: str | None):
        result = self.client.put_object(
            Bucket=self.bucket,
            Key=self.key,
            Body=json.dumps(value, sort_keys=True, allow_nan=False).encode(),
            ContentType="application/json",
            **({"IfMatch": etag} if etag else {"IfNoneMatch": "*"}),
        )
        return result["ETag"]

    def archive(self, state):
        """Create-only terminal evidence; failure blocks starting the next run."""
        if state.get("phase") not in {"COMPLETED", "ROLLED_BACK"}:
            raise ValueError("only verified terminal experiments may be archived")
        eid = state.get("experiment_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", eid):
            raise ValueError("invalid archive experiment ID")
        snapshot = deepcopy(state)
        # Avoid recursively embedding the entire archive index in every snapshot.
        snapshot.pop("history", None)
        checksum = digest(snapshot)
        key = self.key.rsplit("/", 1)[0] + "/experiments/" + eid + "/" + checksum + ".json"
        body = json.dumps(snapshot, sort_keys=True, allow_nan=False).encode()
        from botocore.exceptions import ClientError
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=body,
                                   ContentType="application/json", IfNoneMatch="*")
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            if self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read() != body:
                raise ValueError("immutable experiment archive collision")
        return {"experiment_id": eid, "key": key, "checksum": checksum, "phase": state["phase"]}

    def read_archive(self, reference):
        prefix = self.key.rsplit("/", 1)[0] + "/experiments/"
        if not reference["key"].startswith(prefix):
            raise ValueError("archive outside scope")
        result = json.loads(self.client.get_object(Bucket=self.bucket, Key=reference["key"])["Body"].read())
        if digest(result) != reference["checksum"] or result.get("experiment_id") != reference["experiment_id"]:
            raise ValueError("archive evidence integrity mismatch")
        return result

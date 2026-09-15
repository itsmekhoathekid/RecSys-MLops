"""Create-only Recommendation llm_only candidate from the live champion."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import re
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from jenkins.python.model_cd.storage import parse_s3_uri

from .provision import forward, secret
from .release import digest, managed_backend_binding, release, validate_experiment


def candidate(champion, llm, adapter_image, namespace="kagent"):
    champion = release(champion)
    if champion.get("scope") == "workflow":
        raise ValueError("Recommendation candidate requires a non-workflow champion")
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", adapter_image):
        raise ValueError("candidate adapter image must be digest pinned")
    llm_id = digest(llm)
    binding = managed_backend_binding(
        champion["binding"], llm_id, namespace, adapter_image=adapter_image
    )
    value = release(
        {
            "config": deepcopy(champion["config"]),
            "llm": deepcopy(llm),
            # Prompt, tools, A2A card and stock Go runtime are byte-for-byte
            # frozen. Coordinator/Context do not participate in this release.
            "agent": deepcopy(champion["agent"]),
            "binding": binding,
        }
    )
    validate_experiment(champion, value, "llm_only")
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--catalog",
        default="configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m-tools-v3.json",
    )
    parser.add_argument("--adapter-image", required=True)
    args = parser.parse_args()
    cd = secret("ci", "recsys-llm-ab-cd")
    if cd is None:
        raise ValueError("Recommendation CD secret is missing")
    bucket, state_key = parse_s3_uri(cd["AB_STATE_URI"])
    with forward("experiment-tracking", "minio", 9000) as endpoint:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=cd["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=cd["AWS_SECRET_ACCESS_KEY"],
            region_name=cd.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
        state = json.loads(client.get_object(Bucket=bucket, Key=state_key)["Body"].read())
        if state.get("phase") not in {"IDLE", "COMPLETED", "ROLLED_BACK"}:
            raise ValueError("unfinished Recommendation experiment")
        value = candidate(
            state["champion"], json.loads(Path(args.catalog).read_text()), args.adapter_image
        )
        key = "recommendation/releases/" + value["release_id"] + ".json"
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        try:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            existing = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            if existing != body:
                raise ValueError("immutable candidate object conflict") from exc
        print(
            json.dumps(
                {
                    "candidate_uri": f"s3://{bucket}/{key}",
                    "release_id": value["release_id"],
                    "config_id": value["config_id"],
                    "llm_version_id": value["llm_version_id"],
                    "prompt_checksum": digest(value["agent"]),
                    "baseline_release_id": state["champion"]["release_id"],
                    "scope": "recommendation",
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()

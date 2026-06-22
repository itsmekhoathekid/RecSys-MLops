from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("RECSYS_LIVE_E2E") != "1",
    reason="set RECSYS_LIVE_E2E=1 to run live KServe/FastAPI E2E",
)


def run(command: list[str]) -> str:
    return subprocess.check_output(command, text=True)


def parse_first_json_object(output: str) -> dict:
    decoder = json.JSONDecoder()
    body, _ = decoder.raw_decode(output.strip())
    return body


def test_live_kserve_fastapi_recommendation_flow():
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl is not installed")
    run(
        [
            "kubectl",
            "wait",
            "--for=condition=Ready",
            "inferenceservice/recsys-bst-triton",
            "-n",
            "kserve-triton-inference",
            "--timeout=300s",
        ]
    )
    run(
        [
            "kubectl",
            "wait",
            "--for=condition=Available",
            "deployment/recsys-api-serving",
            "-n",
            "api-serving",
            "--timeout=180s",
        ]
    )
    payload = json.dumps({"user_id": 1, "candidate_item_ids": [1, 2, 3], "top_k": 3})
    response = run(
        [
            "kubectl",
            "run",
            "recsys-serving-e2e",
            "-n",
            "api-serving",
            "--rm",
            "-i",
            "--restart=Never",
            "--image=curlimages/curl:8.10.1",
            "--",
            "curl",
            "-fsS",
            "-X",
            "POST",
            "http://recsys-api-serving/recommendations",
            "-H",
            "Content-Type: application/json",
            "-d",
            payload,
        ]
    )
    body = parse_first_json_object(response)
    assert body["model_version"]
    assert body["items"]
    scores = [item["score"] for item in body["items"]]
    assert scores == sorted(scores, reverse=True)

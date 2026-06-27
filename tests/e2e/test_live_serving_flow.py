from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("RECSYS_LIVE_E2E") != "1",
    reason="set RECSYS_LIVE_E2E=1 to run live KServe/FastAPI E2E",
)


def run(command: list[str]) -> str:
    return subprocess.check_output(command, text=True)


def wait_for_port(port: int, timeout_seconds: int = 120) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(1)
    raise TimeoutError(f"Timed out waiting for 127.0.0.1:{port}")


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


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
    payload = {"user_id": 1, "candidate_item_ids": [1, 2, 3], "top_k": 3}
    port = int(os.getenv("RECSYS_E2E_FASTAPI_PORT", "18088"))
    port_forward = subprocess.Popen(
        [
            "kubectl",
            "-n",
            "api-serving",
            "port-forward",
            "svc/recsys-api-serving",
            f"{port}:80",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_port(port)
        body = post_json(f"http://127.0.0.1:{port}/recommendations", payload)
    finally:
        port_forward.terminate()
        try:
            port_forward.wait(timeout=5)
        except subprocess.TimeoutExpired:
            port_forward.kill()
    assert body["model_version"]
    assert body["items"]
    scores = [item["score"] for item in body["items"]]
    assert scores == sorted(scores, reverse=True)

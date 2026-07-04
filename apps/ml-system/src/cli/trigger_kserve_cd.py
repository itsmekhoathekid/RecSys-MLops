from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_JENKINS_URL = "http://recsys-jenkins.ci.svc.cluster.local:8080"
DEFAULT_JOB_NAME = "RecSys-KServe-Model-CD"
DEFAULT_STATUS_PATH = "/workspace/recsys/data_platform/output/ml/serving/kserve_cd_status.json"


def load_manifest(path: str) -> dict[str, Any]:
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Promotion manifest was not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def model_score(manifest: dict[str, Any], metric_name: str = "") -> float:
    if "metric_value" in manifest:
        return float(manifest["metric_value"])
    metric_key = metric_name or str(manifest.get("metric_name", ""))
    metrics = manifest.get("metrics") or {}
    if metric_key and metric_key in metrics:
        return float(metrics[metric_key])
    if metric_key and metric_key.replace("_", "/") in metrics:
        return float(metrics[metric_key.replace("_", "/")])
    raise KeyError("Promotion manifest does not contain metric_value or a matching metrics entry.")


def basic_auth_header(user: str, token: str) -> dict[str, str]:
    if not user or not token:
        return {}
    raw = f"{user}:{token}".encode("utf-8")
    return {"Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}"}


def request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, str], str]:
    req = urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, dict(response.headers), response.read().decode("utf-8")


def jenkins_crumb(base_url: str, auth_headers: dict[str, str]) -> dict[str, str]:
    try:
        _, _, body = request(
            f"{base_url.rstrip('/')}/crumbIssuer/api/json",
            headers=dict(auth_headers),
            timeout=15,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 403}:
            return {}
        raise
    payload = json.loads(body)
    crumb_field = payload.get("crumbRequestField")
    crumb = payload.get("crumb")
    if not crumb_field or not crumb:
        return {}
    return {str(crumb_field): str(crumb)}


def trigger_jenkins_cd(
    *,
    jenkins_url: str,
    job_name: str,
    params: dict[str, str],
    user: str,
    token: str,
    wait: bool,
    poll_interval_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    base_url = jenkins_url.rstrip("/")
    auth_headers = basic_auth_header(user, token)
    headers = {
        **auth_headers,
        **jenkins_crumb(base_url, auth_headers),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    encoded_params = urllib.parse.urlencode(params).encode("utf-8")
    encoded_job = "/".join(urllib.parse.quote(part, safe="") for part in job_name.split("/"))
    status, response_headers, _ = request(
        f"{base_url}/job/{encoded_job}/buildWithParameters",
        headers=headers,
        data=encoded_params,
        timeout=30,
    )
    queue_url = response_headers.get("Location", "")
    result: dict[str, Any] = {
        "triggered": True,
        "jenkins_url": base_url,
        "job_name": job_name,
        "http_status": status,
        "queue_url": queue_url,
        "parameters": params,
    }
    if not wait:
        return result

    deadline = time.monotonic() + timeout_seconds
    executable_url = ""
    while time.monotonic() < deadline:
        if queue_url:
            _, _, queue_body = request(f"{queue_url.rstrip('/')}/api/json", headers=auth_headers, timeout=15)
            queue_payload = json.loads(queue_body)
            executable_url = queue_payload.get("executable", {}).get("url", "")
            if executable_url:
                result["build_url"] = executable_url
                break
        time.sleep(poll_interval_seconds)
    if not executable_url:
        raise TimeoutError(f"Timed out waiting for Jenkins job {job_name} to leave the queue.")

    while time.monotonic() < deadline:
        _, _, build_body = request(f"{executable_url.rstrip('/')}/api/json", headers=auth_headers, timeout=15)
        build_payload = json.loads(build_body)
        build_result = build_payload.get("result")
        result["build_number"] = build_payload.get("number")
        result["building"] = bool(build_payload.get("building"))
        result["build_result"] = build_result
        if build_result:
            if build_result != "SUCCESS":
                raise RuntimeError(f"Jenkins KServe CD job finished with result={build_result}.")
            return result
        time.sleep(poll_interval_seconds)
    raise TimeoutError(f"Timed out waiting for Jenkins KServe CD job {job_name} to finish.")


def write_status(path: str, payload: dict[str, Any]) -> None:
    status_path = Path(path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trigger Jenkins KServe CD after a promoted BST model passes a score gate.")
    parser.add_argument("--manifest-path", required=True)
    parser.add_argument("--score-threshold", type=float, default=0.0)
    parser.add_argument("--metric-name", default="")
    parser.add_argument("--jenkins-url", default=os.getenv("JENKINS_URL", DEFAULT_JENKINS_URL))
    parser.add_argument("--job-name", default=os.getenv("KSERVE_CD_JOB_NAME", DEFAULT_JOB_NAME))
    parser.add_argument("--jenkins-user", default=os.getenv("JENKINS_USER") or os.getenv("JENKINS_USERNAME", ""))
    parser.add_argument("--jenkins-token", default=os.getenv("JENKINS_TOKEN") or os.getenv("JENKINS_PASSWORD", ""))
    parser.add_argument("--status-path", default=DEFAULT_STATUS_PATH)
    parser.add_argument("--poll-interval-seconds", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--wait", dest="wait", action="store_true", default=True)
    parser.add_argument("--no-wait", dest="wait", action="store_false")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = load_manifest(args.manifest_path)
    score = model_score(manifest, args.metric_name)
    metric_name = args.metric_name or manifest.get("metric_name", "metric_value")
    manifest_uri = manifest.get("promotion_manifest_uri")
    if not manifest_uri:
        raise KeyError("Promotion manifest does not contain promotion_manifest_uri.")

    base_status = {
        "model_name": manifest.get("model_name", ""),
        "model_version": manifest.get("model_version", ""),
        "metric_name": metric_name,
        "metric_value": score,
        "score_threshold": args.score_threshold,
        "promotion_manifest_uri": manifest_uri,
    }
    if score < args.score_threshold:
        status = {**base_status, "triggered": False, "reason": "score_below_threshold"}
        write_status(args.status_path, status)
        print(json.dumps(status, sort_keys=True))
        return 0

    build_status = trigger_jenkins_cd(
        jenkins_url=args.jenkins_url,
        job_name=args.job_name,
        params={
            "PROMOTION_MANIFEST_URI": str(manifest_uri),
            "MODEL_VERSION": str(manifest.get("model_version", "")),
            "METRIC_NAME": str(metric_name),
            "METRIC_VALUE": str(score),
            "TRIGGER_SOURCE": "kubeflow-ray-training",
        },
        user=args.jenkins_user,
        token=args.jenkins_token,
        wait=args.wait,
        poll_interval_seconds=args.poll_interval_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    status = {**base_status, **build_status}
    write_status(args.status_path, status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

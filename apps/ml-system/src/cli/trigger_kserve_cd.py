from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_JENKINS_URL = "http://recsys-jenkins.ci.svc.cluster.local:8080"
DEFAULT_JOB_NAME = "RecSys-KServe-Model-CD"
DEFAULT_STATUS_PATH = "/workspace/recsys/data_platform/output/ml/serving/kserve_cd_status.json"
DEFAULT_MODEL_BUCKET = "recsys-model-store"
DEFAULT_MODEL_PREFIX = "triton/bst"
DEFAULT_PROMOTION_KEY = "promotions/bst/latest.json"
DEFAULT_REGISTERED_MODEL_NAME = "recsys_bst_ranker"


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


def stable_manifest_uri() -> str:
    configured = os.getenv("STABLE_MANIFEST_URI", "").strip()
    if configured:
        return configured
    bucket = os.getenv("MODEL_STORE_BUCKET", DEFAULT_MODEL_BUCKET)
    key = os.getenv("PROMOTION_MANIFEST_KEY", DEFAULT_PROMOTION_KEY).strip("/")
    return f"s3://{bucket}/{key}"


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError(f"Expected s3://bucket/key URI, got {uri}")
    return parsed.netloc, parsed.path.strip("/")


def model_store_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.getenv("MODEL_STORE_ENDPOINT")
        or os.getenv("MLFLOW_S3_ENDPOINT_URL")
        or os.getenv("MINIO_ENDPOINT"),
        aws_access_key_id=os.getenv("MINIO_ROOT_USER") or os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("MINIO_ROOT_PASSWORD") or os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )


def _is_missing_object_error(exc: Exception) -> bool:
    response = getattr(exc, "response", {}) or {}
    code = str((response.get("Error", {}) or {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}


def manifest_exists(uri: str) -> bool:
    if not uri.startswith("s3://"):
        return Path(uri).exists()
    bucket, key = parse_s3_uri(uri)
    try:
        model_store_client().head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _is_missing_object_error(exc):
            return False
        raise
    return True


def publish_stable_manifest(manifest: dict[str, Any], uri: str) -> dict[str, Any]:
    stable = dict(manifest)
    stable["promotion_manifest_uri"] = uri
    bucket = os.getenv("MODEL_STORE_BUCKET", DEFAULT_MODEL_BUCKET)
    prefix = os.getenv("MODEL_STORE_PREFIX", DEFAULT_MODEL_PREFIX).strip("/")
    stable["serving_storage_uri"] = f"s3://{bucket}/{prefix}/latest"
    payload = json.dumps(stable, indent=2, sort_keys=True).encode("utf-8")
    if uri.startswith("s3://"):
        bucket, key = parse_s3_uri(uri)
        model_store_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="application/json",
        )
    else:
        target = Path(uri)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return stable


def registry_coordinates(manifest: dict[str, Any]) -> tuple[str, str]:
    name = str(manifest.get("mlflow_registered_model_name") or DEFAULT_REGISTERED_MODEL_NAME)
    version = str(manifest.get("mlflow_registered_model_version") or "")
    if not version:
        error = str(manifest.get("mlflow_model_registry_error") or "")
        suffix = f": {error}" if error else ""
        raise RuntimeError(f"Promotion manifest is missing its MLflow registry version{suffix}")
    return name, version


def set_registry_rollout_state(
    manifest: dict[str, Any],
    *,
    rollout_status: str,
    alias: str = "",
) -> tuple[str, str]:
    tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "").strip()
    if not tracking_uri:
        raise RuntimeError("MLFLOW_TRACKING_URI is required for the model rollout handoff")
    from mlflow.tracking import MlflowClient

    name, version = registry_coordinates(manifest)
    client = MlflowClient(tracking_uri=tracking_uri)
    client.set_model_version_tag(name, version, "rollout_status", rollout_status)
    if alias:
        client.set_registered_model_alias(name, alias, version)
    return name, version


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
    opener: urllib.request.OpenerDirector | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, str], str]:
    req = urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data is not None else "GET")
    open_url = opener.open if opener else urllib.request.urlopen
    with open_url(req, timeout=timeout) as response:
        return response.status, dict(response.headers), response.read().decode("utf-8")


def jenkins_crumb(
    base_url: str,
    auth_headers: dict[str, str],
    *,
    opener: urllib.request.OpenerDirector | None = None,
) -> dict[str, str]:
    try:
        _, _, body = request(
            f"{base_url.rstrip('/')}/crumbIssuer/api/json",
            headers=dict(auth_headers),
            opener=opener,
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
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    auth_headers = basic_auth_header(user, token)
    headers = {
        **auth_headers,
        **jenkins_crumb(base_url, auth_headers, opener=opener),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    encoded_params = urllib.parse.urlencode(params).encode("utf-8")
    encoded_job = "/".join(urllib.parse.quote(part, safe="") for part in job_name.split("/"))
    status, response_headers, _ = request(
        f"{base_url}/job/{encoded_job}/buildWithParameters",
        headers=headers,
        data=encoded_params,
        opener=opener,
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
            _, _, queue_body = request(
                f"{queue_url.rstrip('/')}/api/json",
                headers=auth_headers,
                opener=opener,
                timeout=15,
            )
            queue_payload = json.loads(queue_body)
            executable_url = queue_payload.get("executable", {}).get("url", "")
            if executable_url:
                result["build_url"] = executable_url
                break
        time.sleep(poll_interval_seconds)
    if not executable_url:
        raise TimeoutError(f"Timed out waiting for Jenkins job {job_name} to leave the queue.")

    while time.monotonic() < deadline:
        _, _, build_body = request(
            f"{executable_url.rstrip('/')}/api/json",
            headers=auth_headers,
            opener=opener,
            timeout=15,
        )
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
    parser = argparse.ArgumentParser(
        description=(
            "Bootstrap the first promoted BST model, or leave later versions in MLflow "
            "until an operator sets candidate=test."
        )
    )
    parser.add_argument("--manifest-path", required=True)
    parser.add_argument("--score-threshold", type=float, default=0.0)
    parser.add_argument("--metric-name", default="")
    parser.add_argument("--jenkins-url", default=os.getenv("JENKINS_URL", DEFAULT_JENKINS_URL))
    parser.add_argument("--job-name", default=os.getenv("KSERVE_CD_JOB_NAME", DEFAULT_JOB_NAME))
    parser.add_argument("--jenkins-user", default=os.getenv("JENKINS_USER") or os.getenv("JENKINS_USERNAME", ""))
    parser.add_argument("--jenkins-token", default=os.getenv("JENKINS_TOKEN") or os.getenv("JENKINS_PASSWORD", ""))
    parser.add_argument("--status-path", default=DEFAULT_STATUS_PATH)
    parser.add_argument("--stable-manifest-uri", default=stable_manifest_uri())
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

    registered_model_name, registry_version = registry_coordinates(manifest)
    if manifest_exists(args.stable_manifest_uri):
        set_registry_rollout_state(
            manifest,
            rollout_status="awaiting_candidate_selection",
        )
        status = {
            **base_status,
            "triggered": False,
            "reason": "champion_exists_waiting_for_candidate_tag",
            "stable_manifest_uri": args.stable_manifest_uri,
            "mlflow_registered_model_name": registered_model_name,
            "mlflow_registered_model_version": registry_version,
            "next_action": f"set candidate=test on MLflow registry version {registry_version}",
        }
        write_status(args.status_path, status)
        print(json.dumps(status, sort_keys=True))
        return 0

    if not args.wait:
        raise ValueError("Cold-start bootstrap requires --wait so latest.json is published only after Jenkins succeeds")

    build_status = trigger_jenkins_cd(
        jenkins_url=args.jenkins_url,
        job_name=args.job_name,
        params={
            "ROLLOUT_STAGE": "deploy",
            "PROMOTION_MANIFEST_URI": str(manifest_uri),
            "CONTROL_MANIFEST_URI": str(manifest_uri),
            "CANDIDATE_MANIFEST_URI": "",
            "AB_CANDIDATE_WEIGHT_PERCENT": "0",
            "MODEL_VERSION": str(manifest.get("model_version", "")),
            "METRIC_NAME": str(metric_name),
            "METRIC_VALUE": str(score),
            "TRIGGER_SOURCE": "kubeflow-cold-start-bootstrap",
        },
        user=args.jenkins_user,
        token=args.jenkins_token,
        wait=args.wait,
        poll_interval_seconds=args.poll_interval_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    publish_stable_manifest(manifest, args.stable_manifest_uri)
    set_registry_rollout_state(
        manifest,
        rollout_status="champion",
        alias="champion",
    )
    status = {
        **base_status,
        **build_status,
        "reason": "initial_champion_bootstrap",
        "stable_manifest_uri": args.stable_manifest_uri,
        "mlflow_registered_model_name": registered_model_name,
        "mlflow_registered_model_version": registry_version,
    }
    write_status(args.status_path, status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

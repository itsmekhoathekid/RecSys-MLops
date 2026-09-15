"""Register an attested LLM catalog entry and emit a Langfuse request.

This command does not deploy a backend or run inference.  Its deliberately
small allowlist turns operator-friendly aliases into reviewed, immutable
catalog manifests; Jenkins remains responsible for compatibility and rollout.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import quote, urlparse

import boto3
import httpx
from botocore.exceptions import ClientError

from apps.agentic.llm_ab_router.trigger import candidate_from_config
from jenkins.python.llm_agent_cd.provision import forward, secret
from jenkins.python.llm_agent_cd.release import digest, validate_experiment
from jenkins.python.model_cd.storage import parse_s3_uri, require_versioning
from .model_onboarding import (
    ALIAS_RE,
    attest_huggingface,
    build_catalog,
    build_profile,
    fetch_gguf_metadata,
    load_policy,
    onboarding_id,
    parse_artifact_url,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_REF = "519c167a764597b7b05b19e1f025bb4956e3dc9456ac77775f7f3a06ce2d3ba8"
PROFILE = "qwen35-b8646-stock-adk-cache-v2"
REQUESTED_MODEL = "Qwen/Qwen3.5-0.8B-GGUF"
RESOLVED_MODEL = "ggml-org/Qwen3.5-0.8B-GGUF"
REVISION = "8fea620810c4afa23dd6443f999a48574c1611a3"
FILENAME = "Qwen3.5-0.8B-Q4_0.gguf"
ARTIFACT_SHA256 = "57d1997790d1744fba5b40a7317df71ea5e2acee28c47e78f0cce39c0703f8cf"
ARTIFACT_SIZE = 563036064
CATALOG_FILE = REPO_ROOT / "configs/llm-ab/catalog/qwen3.5-0.8b-q4_0-b8646-stock-adk-cache-v2.json"
STOCK_PROFILE = "qwen35-b8646-stock-adk-v1"
STOCK_EXPECTED_REF = "d0a732fbb109f6209cc6c9ad3f8b3cd03bb62b7349c3327a8460a8dc33e34a93"
QWEN25_REQUESTED_MODEL = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
QWEN25_RESOLVED_MODEL = QWEN25_REQUESTED_MODEL
QWEN25_REVISION = "9217f5db79a29953eb74d5343926648285ec7e67"
QWEN25_FILENAME = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
QWEN25_PROFILE = "qwen25-small-cpu-b8646-terminal-v4"
QWEN25_ARTIFACT_SHA256 = "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db"
QWEN25_ARTIFACT_SIZE = 491400032
QWEN25_EXPECTED_REF = "5f8c2912e51a1ad9ead09ba37f57aae046919dda389e2f619c6a15fe37549f63"


def _reviewed_catalog(path, expected_ref, requested_model, resolved_model,
                      revision, filename, sha256, size):
    return {
        "path": path,
        "expected_ref": expected_ref,
        "requested_model": requested_model,
        "resolved_model": resolved_model,
        "revision": revision,
        "filename": filename,
        "sha256": sha256,
        "size": size,
    }


CATALOG_ALLOWLIST = {
    PROFILE: _reviewed_catalog(
        CATALOG_FILE, EXPECTED_REF, REQUESTED_MODEL, RESOLVED_MODEL,
        REVISION, FILENAME, ARTIFACT_SHA256, ARTIFACT_SIZE,
    ),
    STOCK_PROFILE: _reviewed_catalog(
        REPO_ROOT / "configs/llm-ab/catalog/qwen3.5-0.8b-q4_0-b8646-stock-adk-v1.json",
        STOCK_EXPECTED_REF, REQUESTED_MODEL, RESOLVED_MODEL,
        REVISION, FILENAME, ARTIFACT_SHA256, ARTIFACT_SIZE,
    ),
    QWEN25_PROFILE: _reviewed_catalog(
        REPO_ROOT / "configs/llm-ab/catalog/qwen2.5-0.5b-q4_k_m-tools-v3.json",
        QWEN25_EXPECTED_REF, QWEN25_REQUESTED_MODEL, QWEN25_RESOLVED_MODEL,
        QWEN25_REVISION, QWEN25_FILENAME,
        QWEN25_ARTIFACT_SHA256, QWEN25_ARTIFACT_SIZE,
    ),
}
MODEL_ALIASES = {
    "qwen35-0.8b-q4": {
        "model": REQUESTED_MODEL,
        "revision": REVISION,
        "filename": FILENAME,
        "profile": PROFILE,
    },
    # This is the previous production champion. It already completed the
    # Recommendation contract successfully and remains an immutable rollback
    # release, making it the safest non-champion choice for an evidence run.
    "qwen25-0.5b-q4-terminal-v4": {
        "model": QWEN25_REQUESTED_MODEL,
        "revision": QWEN25_REVISION,
        "filename": QWEN25_FILENAME,
        "profile": QWEN25_PROFILE,
    },
}


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def reviewed_catalog(model, revision, filename, profile):
    selected = CATALOG_ALLOWLIST.get(profile)
    if not selected or (model, revision, filename) != (
        selected["requested_model"], selected["revision"], selected["filename"]
    ):
        raise ValueError("model, revision, file and profile must match an allowlisted catalog entry")
    return selected


def profile_manifest(model, revision, filename, profile):
    selected = reviewed_catalog(model, revision, filename, profile)
    llm = json.loads(selected["path"].read_text())
    expected_uri = ("https://huggingface.co/" + selected["resolved_model"]
                    + "/resolve/" + selected["revision"] + "/" + selected["filename"])
    if (llm.get("artifact_uri") != expected_uri
            or llm.get("artifact_sha256") != selected["sha256"]
            or llm.get("serving", {}).get("resourceProfile") != profile):
        raise ValueError("reviewed local catalog profile identity mismatch")
    if digest(llm) != selected["expected_ref"]:
        raise ValueError("reviewed local catalog digest mismatch")
    return llm


def resolve_model_alias(model_alias):
    """Resolve an operator-friendly name to one reviewed immutable tuple."""
    selected = MODEL_ALIASES.get(model_alias)
    if selected is None:
        raise ValueError("model alias is not allowlisted")
    # Return a copy so callers cannot mutate the process-wide allowlist.
    return deepcopy(selected)


def verify_huggingface(client, llm):
    selected = next(
        (item for item in CATALOG_ALLOWLIST.values()
         if item["expected_ref"] == digest(llm)),
        None,
    )
    if selected is None:
        raise ValueError("catalog is not in the reviewed registry")
    response = client.get(
        "https://huggingface.co/api/models/" + selected["resolved_model"]
        + "/revision/" + selected["revision"],
        params={"blobs": "true"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("sha") != selected["revision"]:
        raise ValueError("Hugging Face revision attestation mismatch")
    matches = [row for row in payload.get("siblings", [])
               if row.get("rfilename") == selected["filename"]]
    if len(matches) != 1:
        raise ValueError("Hugging Face artifact file is missing or ambiguous")
    row = matches[0]
    lfs = row.get("lfs") or {}
    sha = lfs.get("sha256") or row.get("sha256")
    size = lfs.get("size") or row.get("size")
    if sha != selected["sha256"] or size != selected["size"]:
        raise ValueError("Hugging Face artifact checksum or size mismatch")
    if llm["artifact_sha256"] != sha:
        raise ValueError("catalog and Hugging Face attestation disagree")


def _client(endpoint, values):
    return boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=values["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=values["AWS_SECRET_ACCESS_KEY"],
        region_name=values.get("AWS_DEFAULT_REGION", "us-east-1"),
    )


@contextmanager
def production_clients():
    cd = secret("ci", "recsys-llm-ab-cd")
    poller = secret("kagent", "recsys-recommendation-trigger")
    if not cd or not poller:
        raise ValueError("catalog CD and Recommendation poller credentials are required")
    with forward("experiment-tracking", "minio", 9000) as endpoint:
        yield _client(endpoint, cd), _client(endpoint, poller), (
            cd.get("AB_RECOMMENDATION_STATE_URI") or cd.get("AB_STATE_URI")
            or "s3://recsys-llm-ab/recommendation/state.json"
        )


def put_create_only(client, bucket, key, value):
    body = canonical_bytes(value)
    try:
        client.put_object(Bucket=bucket, Key=key, Body=body,
                          ContentType="application/json", IfNoneMatch="*")
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
            raise
        if client.get_object(Bucket=bucket, Key=key)["Body"].read() != body:
            raise ValueError("immutable catalog object collision")
    return body


def persist_intent(client, bucket, key, intent):
    """Create once, or resume the same immutable intent after a CLI crash."""
    body = canonical_bytes(intent)
    try:
        client.put_object(Bucket=bucket, Key=key, Body=body,
                          ContentType="application/json", IfNoneMatch="*")
        return intent
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
            raise
    existing = _get_json(client, bucket, key)
    def stable(value):
        return {key: item for key, item in value.items() if key != "created_at"}

    if stable(existing) != stable(intent):
        raise ValueError("immutable onboarding intent collision")
    return existing


def read_state(client, uri):
    """Read-only CLI does not require botocore's newer conditional IfMatch."""
    bucket, key = parse_s3_uri(uri)
    require_versioning(client, bucket)
    return json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read())


def register(scope, model, revision, filename, profile, *, cd_client, poller_client,
             state_uri, hf_client, model_alias=None):
    if scope != "recommendation":
        raise ValueError("only Recommendation catalog registration is supported")
    llm = profile_manifest(model, revision, filename, profile)
    selected = reviewed_catalog(model, revision, filename, profile)
    llm_release_ref = digest(llm)
    verify_huggingface(hf_client, llm)
    before = read_state(cd_client, state_uri)
    champion = before["champion"]
    if llm == champion["llm"] and digest(llm) == champion["llm_version_id"]:
        return {"status": "NOOP", "llm_release_ref": llm_release_ref,
                "reason": "requested LLM is already champion"}
    config = {
        "schema_version": 1,
        "scope": "recommendation",
        "baseline_release_id": champion["release_id"],
        "generation": deepcopy(champion["config"]),
        "llm_release_ref": llm_release_ref,
        "experiment_type": "llm_only",
        "policy_ref": "recommendation-live-test",
    }
    candidate = candidate_from_config(champion, config, llm, allow_live_test=True)
    validate_experiment(champion, candidate, "llm_only")
    bucket = "recsys-llm-ab"
    key = "recommendation/catalog/" + llm_release_ref + ".json"
    body = put_create_only(cd_client, bucket, key, llm)
    readback = poller_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    if readback != body or digest(json.loads(readback)) != llm_release_ref:
        raise ValueError("Recommendation poller catalog readback failed")
    attestation_key = "recommendation/catalog-attestations/" + llm_release_ref + ".json"
    attestation = {
        "schema_version": 1,
        "status": "legacy-approved",
        "scope": "recommendation",
        "llm_release_ref": llm_release_ref,
        "source": str(selected["path"].relative_to(REPO_ROOT)),
    }
    attestation_body = put_create_only(cd_client, bucket, attestation_key, attestation)
    if poller_client.get_object(Bucket=bucket, Key=attestation_key)["Body"].read() != attestation_body:
        raise ValueError("Recommendation poller attestation readback failed")
    after = read_state(cd_client, state_uri)
    if after["champion"]["release_id"] != champion["release_id"]:
        raise ValueError("champion changed during catalog registration; no runnable request emitted")
    common = {
        "llm_release_ref": llm_release_ref,
        "catalog_uri": "s3://" + bucket + "/" + key,
        "requested_model": selected["requested_model"],
        "resolved_model": selected["resolved_model"],
    }
    if model_alias is not None:
        common["model_alias"] = model_alias
    return {
        "status": "REGISTERED",
        **common,
        "candidate_release_id": candidate["release_id"],
        "langfuse_config": config,
    }


def register_alias(scope, model_alias, **kwargs):
    selected = MODEL_ALIASES.get(model_alias)
    if selected:
        return register(
            scope,
            selected["model"],
            selected["revision"],
            selected["filename"],
            selected["profile"],
            model_alias=model_alias,
            **kwargs,
        )
    return register_dynamic_alias(scope, model_alias, **kwargs)


def _get_json(client, bucket, key):
    return json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read())


def _langfuse_config(champion, llm_release_ref):
    return {
        "schema_version": 1,
        "scope": "recommendation",
        "baseline_release_id": champion["release_id"],
        "generation": deepcopy(champion["config"]),
        "llm_release_ref": llm_release_ref,
        "experiment_type": "llm_only",
        "policy_ref": "recommendation-live-test",
    }


def register_dynamic_alias(scope, model_alias, *, cd_client, poller_client,
                           state_uri, hf_client=None):
    """Start a previously onboarded alias; no artifact is re-discovered here."""
    if scope != "recommendation" or not ALIAS_RE.fullmatch(model_alias):
        raise ValueError("scope or model alias is invalid")
    bucket = os.environ.get("AB_CATALOG_BUCKET", "recsys-llm-ab")
    alias = _get_json(cd_client, bucket, "recommendation/aliases/" + model_alias + ".json")
    ref = alias.get("llm_release_ref", "")
    attestation = _get_json(cd_client, bucket, "recommendation/catalog-attestations/" + ref + ".json")
    if (alias.get("model_alias") != model_alias
            or attestation.get("llm_release_ref") != ref
            or attestation.get("status") not in {"READY", "legacy-approved"}):
        raise ValueError("model alias lacks a READY onboarding attestation")
    llm = _get_json(cd_client, bucket, "recommendation/catalog/" + ref + ".json")
    if digest(llm) != ref:
        raise ValueError("reviewed alias catalog digest mismatch")
    # Prove the exact objects are readable with the poller's credential too.
    for key in ("recommendation/aliases/" + model_alias + ".json",
                "recommendation/catalog-attestations/" + ref + ".json",
                "recommendation/catalog/" + ref + ".json"):
        poller_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    before = read_state(cd_client, state_uri)
    champion = before["champion"]
    if llm == champion["llm"]:
        return {"status": "NOOP", "model_alias": model_alias,
                "llm_release_ref": ref, "reason": "requested LLM is already champion"}
    config = _langfuse_config(champion, ref)
    candidate = candidate_from_config(champion, config, llm, allow_live_test=True)
    validate_experiment(champion, candidate, "llm_only")
    after = read_state(cd_client, state_uri)
    if after["champion"]["release_id"] != champion["release_id"]:
        raise ValueError("champion changed while resolving alias")
    result = {
        "status": "REGISTERED",
        "model_alias": model_alias,
        "llm_release_ref": ref,
        "candidate_release_id": candidate["release_id"],
        "catalog_uri": f"s3://{bucket}/recommendation/catalog/{ref}.json",
    }
    return {**result, "langfuse_config": config}


def _job_entries(session, endpoint, job):
    encoded = quote(job, safe="")
    tree = "builds[number,url,building,result,actions[parameters[name,value]]]{0,100}"
    build_response = session.get(endpoint + "/job/" + encoded + "/api/json",
                                 params={"tree": tree}, timeout=20)
    queue_response = session.get(
        endpoint + "/queue/api/json",
        params={"tree": "items[id,task[name],actions[parameters[name,value]]]"},
        timeout=20,
    )
    build_response.raise_for_status()
    queue_response.raise_for_status()
    return (build_response.json().get("builds", []),
            queue_response.json().get("items", []))


def _parameter(entry, name):
    return next((value.get("value") for action in entry.get("actions", [])
                 for value in action.get("parameters", []) if value.get("name") == name), None)


def local_artifact_url(endpoint, build_url):
    """Rebase Jenkins' internal absolute build URL onto the active port-forward."""
    path = urlparse(build_url).path
    if not path.startswith("/job/") or not path.endswith("/"):
        raise ValueError("Jenkins returned an invalid build URL")
    return endpoint.rstrip("/") + path + "artifact/.llm-onboarding/result.json"


def dispatch_onboarding(endpoint, credentials, onboarding_id_value, intent_uri,
                        router_image, timeout_seconds=3600):
    """Submit exactly one Jenkins build and return its immutable result artifact."""
    import requests

    job = credentials.get("AB_ONBOARDING_JENKINS_JOB", "RecSys-LLM-Candidate-Onboard")
    session = requests.Session()
    session.auth = (credentials.get("AB_JENKINS_USER") or credentials.get("username"),
                    credentials.get("AB_JENKINS_TOKEN") or credentials.get("password"))
    if not all(session.auth):
        raise ValueError("Jenkins onboarding credential is unavailable")
    deadline = time.monotonic() + timeout_seconds
    submitted = False
    while time.monotonic() < deadline:
        builds, queued = _job_entries(session, endpoint, job)
        matches = [item for item in builds if _parameter(item, "ONBOARDING_ID") == onboarding_id_value]
        waiting = [item for item in queued if item.get("task", {}).get("name") == job
                   and _parameter(item, "ONBOARDING_ID") == onboarding_id_value]
        if len(matches) + len(waiting) > 1:
            raise RuntimeError("multiple Jenkins onboarding deliveries found")
        if matches:
            build = matches[0]
            if build.get("building"):
                time.sleep(5)
                continue
            if build.get("result") != "SUCCESS":
                raise RuntimeError("Jenkins model onboarding failed: " + build["url"])
            artifact = session.get(
                local_artifact_url(endpoint, build["url"]),
                timeout=20,
            )
            artifact.raise_for_status()
            return artifact.json()
        if waiting or submitted:
            time.sleep(5)
            continue
        crumb = session.get(endpoint + "/crumbIssuer/api/json", timeout=20)
        headers = {}
        if crumb.status_code == 200:
            value = crumb.json()
            headers[value["crumbRequestField"]] = value["crumb"]
        response = session.post(endpoint + "/job/" + job + "/buildWithParameters",
                                data={"ACTION": "prepare", "ONBOARDING_ID": onboarding_id_value,
                                      "INTENT_URI": intent_uri, "ROUTER_IMAGE": router_image},
                                headers=headers, timeout=20)
        response.raise_for_status()
        submitted = True
    raise TimeoutError("Jenkins model onboarding did not finish before the CLI timeout")


def discover(scope, model_alias, artifact_url, *, cd_client, state_uri, hf_client,
             policy=None, clock=time.time):
    """Create one immutable onboarding intent; it never downloads the full GGUF."""
    policy = policy or load_policy()
    if scope != "recommendation" or not ALIAS_RE.fullmatch(model_alias):
        raise ValueError("model alias must be lowercase and 3-64 safe characters")
    if model_alias in MODEL_ALIASES:
        raise ValueError("model alias is already reserved by the legacy reviewed registry")
    identity = parse_artifact_url(artifact_url)
    artifact = attest_huggingface(hf_client, identity, policy)
    if artifact.get("status") == "NEEDS_LICENSE_REVIEW":
        return artifact, None
    metadata = fetch_gguf_metadata(hf_client, identity, policy)
    try:
        profile = build_profile(artifact, metadata, policy)
    except ValueError as exc:
        if str(exc).startswith("HOLD_CAPACITY"):
            return {"status": "HOLD_CAPACITY", "reason": str(exc),
                    "repository": artifact["repository"],
                    "revision": artifact["revision"],
                    "filename": artifact["filename"],
                    "size_bytes": artifact["size_bytes"]}, None
        raise
    catalog = build_catalog(artifact, metadata, profile)
    ref = digest(catalog)
    try:
        existing_alias = _get_json(
            cd_client, "recsys-llm-ab", "recommendation/aliases/" + model_alias + ".json"
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"NoSuchKey", "404", "NotFound"}:
            raise
    else:
        if existing_alias.get("llm_release_ref") != ref:
            raise ValueError("model alias already points to a different immutable LLM release")
    state = read_state(cd_client, state_uri)
    champion = state["champion"]
    if catalog == champion["llm"]:
        return {"status": "NOOP", "llm_release_ref": ref}, None
    config = _langfuse_config(champion, ref)
    candidate = candidate_from_config(champion, config, catalog, allow_live_test=True)
    validate_experiment(champion, candidate, "llm_only")
    oid = onboarding_id(scope, model_alias, artifact, policy)
    intent = {
        "schema_version": 1,
        "onboarding_id": oid,
        "scope": scope,
        "model_alias": model_alias,
        "artifact": artifact,
        "profile": profile,
        "catalog": catalog,
        "llm_release_ref": ref,
        "candidate": candidate,
        "expected_champion": champion["release_id"],
        "policy_checksum": digest(policy),
        "created_at": clock(),
    }
    key = "recommendation/onboarding/" + oid + "/intent.json"
    intent = persist_intent(cd_client, "recsys-llm-ab", key, intent)
    return artifact, (intent, "s3://recsys-llm-ab/" + key, config)


def onboard(scope, model_alias, artifact_url, output_dir, *, cd_client, poller_client,
            state_uri, hf_client, jenkins_endpoint, jenkins_credentials,
            router_image, dispatch=dispatch_onboarding):
    artifact, discovered = discover(
        scope, model_alias, artifact_url, cd_client=cd_client,
        state_uri=state_uri, hf_client=hf_client,
    )
    if discovered is None:
        result = artifact
    else:
        intent, intent_uri, config = discovered
        prepared = dispatch(jenkins_endpoint, jenkins_credentials,
                            intent["onboarding_id"], intent_uri, router_image)
        if prepared.get("status") != "READY":
            raise ValueError("Jenkins did not produce READY onboarding evidence")
        after = read_state(cd_client, state_uri)
        if after["champion"]["release_id"] != intent["expected_champion"]:
            raise ValueError("champion changed after preparation; no runnable config emitted")
        # Final caller-side proof using the poller's independent credential.
        for uri in (prepared["catalog_uri"], prepared["profile_uri"],
                    prepared["alias_uri"], prepared["attestation_uri"]):
            bucket, key = parse_s3_uri(uri)
            poller_client.get_object(Bucket=bucket, Key=key)["Body"].read()
        result = {
            "status": "READY",
            "onboarding_id": intent["onboarding_id"],
            "model_alias": model_alias,
            "llm_release_ref": intent["llm_release_ref"],
            "candidate_release_id": intent["candidate"]["release_id"],
            "catalog_uri": prepared["catalog_uri"],
            "profile_uri": prepared["profile_uri"],
            "alias_uri": prepared["alias_uri"],
            "prepared": {
                "traffic_weight": prepared["traffic_weight"],
                "backend_ready": prepared["backend_ready"],
                "compatibility": prepared["compatibility"],
                "expires_at": prepared["expires_at"],
            },
            "langfuse_config": config,
        }
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / "registration.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def parse_args(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "onboard":
        parser = argparse.ArgumentParser()
        parser.add_argument("command", choices=["onboard"])
        parser.add_argument("--scope", required=True)
        parser.add_argument("--model-alias", required=True)
        parser.add_argument("--artifact-url", required=True)
        parser.add_argument("--output-dir", required=True)
        parser.add_argument("--timeout-seconds", type=int, default=3600)
        args = parser.parse_args(argv)
        if not ALIAS_RE.fullmatch(args.model_alias):
            parser.error("--model-alias must match ^[a-z0-9][a-z0-9._-]{2,63}$")
        return args
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--model-alias")
    source.add_argument("--model")
    parser.add_argument("--revision")
    parser.add_argument("--file")
    parser.add_argument("--profile")
    args = parser.parse_args(argv)
    args.command = "start"
    explicit = (args.revision, args.file, args.profile)
    if args.model_alias and any(value is not None for value in explicit):
        parser.error("--model-alias cannot be combined with --revision, --file or --profile")
    if args.model and any(value is None for value in explicit):
        parser.error("--model requires --revision, --file and --profile")
    return args


def main():
    args = parse_args()
    with httpx.Client(follow_redirects=args.command == "onboard",
                      transport=httpx.HTTPTransport(retries=0)) as hf:
        with production_clients() as (cd_client, poller_client, current_state_uri):
            if args.command == "onboard":
                jenkins_credentials = secret("kagent", "recsys-workflow-jenkins-dispatch")
                if not jenkins_credentials:
                    raise ValueError("scoped Jenkins dispatch credential is unavailable")
                router_image = json.loads(
                    subprocess.check_output([
                        "kubectl", "-n", "kagent", "get", "deployment", "recsys-ab-router",
                        "-o", "json"], text=True)
                )["spec"]["template"]["spec"]["containers"][0]["image"]
                print("Inspecting pinned GGUF metadata and preparing a zero-traffic candidate...",
                      file=sys.stderr)
                with forward("ci", "recsys-jenkins", 8080) as jenkins_endpoint:
                    result = onboard(
                        args.scope, args.model_alias, args.artifact_url, args.output_dir,
                        cd_client=cd_client, poller_client=poller_client,
                        state_uri=current_state_uri, hf_client=hf,
                        jenkins_endpoint=jenkins_endpoint,
                        jenkins_credentials=jenkins_credentials,
                        router_image=router_image,
                        dispatch=lambda *values: dispatch_onboarding(
                            *values, timeout_seconds=args.timeout_seconds),
                    )
                print(json.dumps(result, sort_keys=True))
                return
            clients = {
                "cd_client": cd_client,
                "poller_client": poller_client,
                "state_uri": current_state_uri,
                "hf_client": hf,
            }
            if args.model_alias:
                result = register_alias(args.scope, args.model_alias, **clients)
            else:
                result = register(
                    args.scope, args.model, args.revision, args.file, args.profile,
                    **clients,
                )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

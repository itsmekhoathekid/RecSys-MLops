"""Create least-privilege credentials for the Recommendation poller."""

from __future__ import annotations

import argparse
import json
import secrets

from .provision import apply, kube, secret
from .recommendation_langfuse_automation import PROJECT, PROMPT, ensure_parking

NAME = "recsys-recommendation-trigger"


def configure(enabled):
    existing_raw = kube("kagent", "get", "secret", NAME, "--ignore-not-found", "-o", "json")
    if existing_raw and json.loads(existing_raw)["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd":
        raise ValueError("foreign Recommendation trigger secret")
    current = secret("kagent", NAME) if existing_raw else {}
    runtime = secret("kagent", "recsys-llm-ab-runtime")
    evaluation = secret("kagent", "recsys-recommendation-evaluation")
    jenkins = secret("kagent", "recsys-workflow-jenkins-dispatch")
    if not runtime or not evaluation or not jenkins:
        raise ValueError("Recommendation trigger dependencies are incomplete")
    if jenkins.get("AB_JENKINS_USER") != "recsys-workflow-dispatch":
        raise ValueError("unexpected Jenkins dispatch identity")
    account = "recsys-recommendation-trigger"
    password = current.get("AWS_SECRET_ACCESS_KEY") or secrets.token_urlsafe(40)
    parking = ensure_parking(False)
    settings = {
        **jenkins,
        "AB_DATABASE_URL": runtime["AB_DATABASE_URL"],
        "AB_RECOMMENDATION_STATE_URI": runtime["AB_STATE_URI"],
        "AB_RECOMMENDATION_JENKINS_JOB": "RecSys-LLM-Agent-CD",
        "AB_ONBOARDING_JENKINS_JOB": "RecSys-LLM-Candidate-Onboard",
        "MODEL_STORE_ENDPOINT": runtime["MODEL_STORE_ENDPOINT"],
        "AWS_DEFAULT_REGION": runtime.get("AWS_DEFAULT_REGION", "us-east-1"),
        "AWS_ACCESS_KEY_ID": account,
        "AWS_SECRET_ACCESS_KEY": password,
        "AB_DISPATCH_ENABLED": str(enabled).lower(),
        "AB_TRIGGER_MODE": "poll",
        "AB_TRIGGER_SCOPE": "recommendation",
        "AB_ALLOW_LIVE_TEST": "true",
        "LANGFUSE_PROJECT_ID": PROJECT,
        "AB_LANGFUSE_PROMPT": PROMPT,
        "AB_LANGFUSE_PARKING_VERSION": str(parking["version"]),
        "LANGFUSE_BASE_URL": evaluation["LANGFUSE_BASE_URL"],
        "LANGFUSE_PUBLIC_KEY": evaluation["LANGFUSE_PUBLIC_KEY"],
        "LANGFUSE_SECRET_KEY": evaluation["LANGFUSE_SECRET_KEY"],
    }
    apply("kagent", {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": NAME,
        "labels": {"recsys.ai/owner": "llm-agent-cd"}}, "stringData": settings})
    # stringData server-side apply cannot reliably remove keys written by an
    # older Secret revision. The rollback HMAC stays in its separate Secret.
    stale = sorted(set(current) & {"LANGFUSE_WEBHOOK_SECRET", "AB_TRIGGER_STATUS_TOKEN"})
    if stale:
        operations = [{"op": "remove", "path": "/data/" + key} for key in stale]
        kube("kagent", "patch", "secret", NAME, "--type=json", "-p", json.dumps(operations))
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetBucketVersioning"],
         "Resource": ["arn:aws:s3:::recsys-llm-ab"]},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"],
         "Resource": ["arn:aws:s3:::recsys-llm-ab/recommendation/state.json",
                      "arn:aws:s3:::recsys-llm-ab/recommendation/catalog/*",
                      "arn:aws:s3:::recsys-llm-ab/recommendation/catalog-attestations/*",
                      "arn:aws:s3:::recsys-llm-ab/recommendation/aliases/*",
                      "arn:aws:s3:::recsys-llm-ab/recommendation/profiles/*",
                      "arn:aws:s3:::recsys-llm-ab/recommendation/onboarding/*",
                      "arn:aws:s3:::recsys-llm-ab/recommendation/candidates/*",
                      "arn:aws:s3:::recsys-llm-ab/recommendation/experiments/*"]},
        {"Effect": "Allow", "Action": ["s3:PutObject"],
         "Resource": ["arn:aws:s3:::recsys-llm-ab/recommendation/candidates/*",
                      "arn:aws:s3:::recsys-llm-ab/recommendation/state.json"]},
    ]}
    script = """set -eu
read -r account
read -r password
read -r policy
directory=$(mktemp -d /tmp/recommendation-trigger-mc.XXXXXX)
mc --config-dir "$directory" alias set owned http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc --config-dir "$directory" admin user add owned "$account" "$password" >/dev/null
printf '%s' "$policy" | mc --config-dir "$directory" admin policy create owned "$account" /dev/stdin >/dev/null
mc --config-dir "$directory" admin policy attach owned "$account" --user "$account" >/dev/null
rm -f -- "$directory/config.json"
rmdir -- "$directory/certs/CAs" "$directory/certs" "$directory" 2>/dev/null || true
"""
    kube("experiment-tracking", "exec", "-i", "deployment/minio", "--", "sh", "-c", script,
         data="\n".join([account, password, json.dumps(policy)]) + "\n")
    return {"secret": NAME, "dispatch_enabled": enabled, "values_logged": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--enable-dispatch", action="store_true")
    args = parser.parse_args()
    print(json.dumps(configure(args.enable_dispatch), sort_keys=True))


if __name__ == "__main__":
    main()

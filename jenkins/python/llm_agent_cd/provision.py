"""Explicit production bootstrap. Credentials stay in Kubernetes or process memory."""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import socket
import subprocess
import time
import os
from contextlib import contextmanager
from pathlib import Path

import boto3

from .release import DEFAULT_POLICY, digest, release


def kube(ns, *args, data=None):
    p = subprocess.run(
        ["kubectl", "-n", ns, *args], input=data, capture_output=True, text=True
    )
    if p.returncode:
        # Bootstrap inputs can contain credentials: do not echo subprocess output.
        raise RuntimeError(f"kubectl {args[0]} failed in {ns} (exit {p.returncode})")
    return p.stdout


def secret(ns, name):
    raw = kube(ns, "get", "secret", name, "--ignore-not-found", "-o", "json")
    if not raw:
        return None
    return {k: base64.b64decode(v).decode() for k, v in json.loads(raw)["data"].items()}


def apply(ns, obj):
    return kube(ns, "apply", "-f", "-", data=json.dumps(obj)).strip()


def seed_legacy_catalog_attestations(client):
    """Add approval sidecars without rewriting content-addressed legacy JSON."""
    from botocore.exceptions import ClientError

    for path in sorted(Path("configs/llm-ab/catalog").glob("*.json")):
        catalog = json.loads(path.read_text())
        ref = digest(catalog)
        catalog_key = "recommendation/catalog/" + ref + ".json"
        try:
            stored = client.get_object(Bucket="recsys-llm-ab", Key=catalog_key)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                continue
            raise
        if digest(json.loads(stored)) != ref:
            raise ValueError("legacy catalog object digest mismatch")
        key = "recommendation/catalog-attestations/" + ref + ".json"
        value = {"schema_version": 1, "status": "legacy-approved",
                 "scope": "recommendation", "llm_release_ref": ref,
                 "source": str(path)}
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        try:
            client.put_object(Bucket="recsys-llm-ab", Key=key, Body=body,
                              ContentType="application/json", IfNoneMatch="*")
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            if client.get_object(Bucket="recsys-llm-ab", Key=key)["Body"].read() != body:
                raise ValueError("legacy catalog attestation collision") from exc


def legacy_attestations():
    """Seed approval sidecars only; do not rotate credentials or mutate catalogs."""
    cd = secret("ci", "recsys-llm-ab-cd")
    if not cd:
        raise ValueError("LLM A/B CD credential is unavailable")
    with forward("experiment-tracking", "minio", 9000) as endpoint:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=cd["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=cd["AWS_SECRET_ACCESS_KEY"],
            region_name=cd.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
        seed_legacy_catalog_attestations(client)
    print("Legacy catalog approval sidecars verified")


@contextmanager
def forward(ns, service, remote):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        ["kubectl", "-n", ns, "port-forward", "service/" + service, f"{port}:{remote}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError("port-forward failed")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("port-forward readiness timeout")
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(timeout=10)


def credentials():
    runtime = secret("kagent", "recsys-llm-ab-runtime")
    if runtime is None:
        dbpass, token = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        runtime = {
            "AB_DATABASE_URL": f"postgresql://recsys_ab:{dbpass}@kagent-postgresql.kagent.svc.cluster.local:5432/recsys_ab",
            "AB_INTERNAL_TOKEN": token,
            "AB_STATE_URI": "s3://recsys-llm-ab/recommendation/state.json",
            "MODEL_STORE_ENDPOINT": "http://minio.experiment-tracking.svc.cluster.local:9000",
            "AWS_ACCESS_KEY_ID": "recsys-ab-runtime",
            "AWS_SECRET_ACCESS_KEY": secrets.token_urlsafe(32),
            "AWS_DEFAULT_REGION": "us-east-1",
            "AB_CASE_TICKET_KEY": secrets.token_urlsafe(48),
            "AB_LIVE_TEST_TOKEN": secrets.token_urlsafe(48),
        }
        print(
            apply(
                "kagent",
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "recsys-llm-ab-runtime"},
                    "stringData": runtime,
                },
            )
        )
    if "AB_CASE_TICKET_KEY" not in runtime:
        runtime["AB_CASE_TICKET_KEY"] = secrets.token_urlsafe(48)
        kube("kagent", "patch", "secret", "recsys-llm-ab-runtime", "--type=merge",
             "-p", json.dumps({"stringData": {"AB_CASE_TICKET_KEY": runtime["AB_CASE_TICKET_KEY"]}}))
    if "AB_LIVE_TEST_TOKEN" not in runtime:
        runtime["AB_LIVE_TEST_TOKEN"] = secrets.token_urlsafe(48)
        kube("kagent", "patch", "secret", "recsys-llm-ab-runtime", "--type=merge",
             "-p", json.dumps({"stringData": {"AB_LIVE_TEST_TOKEN": runtime["AB_LIVE_TEST_TOKEN"]}}))
    cd = secret("ci", "recsys-llm-ab-cd")
    if cd is None:
        cd = {
            **runtime,
            "AWS_ACCESS_KEY_ID": "recsys-ab-cd",
            "AWS_SECRET_ACCESS_KEY": secrets.token_urlsafe(32),
        }
        print(
            apply(
                "ci",
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "recsys-llm-ab-cd"},
                    "stringData": cd,
                },
            )
        )
    elif cd.get("AB_CASE_TICKET_KEY") != runtime["AB_CASE_TICKET_KEY"]:
        kube("ci", "patch", "secret", "recsys-llm-ab-cd", "--type=merge", "-p",
             json.dumps({"stringData": {"AB_CASE_TICKET_KEY": runtime["AB_CASE_TICKET_KEY"]}}))
        cd["AB_CASE_TICKET_KEY"] = runtime["AB_CASE_TICKET_KEY"]
    if cd.get("AB_LIVE_TEST_TOKEN") != runtime["AB_LIVE_TEST_TOKEN"]:
        kube("ci", "patch", "secret", "recsys-llm-ab-cd", "--type=merge", "-p",
             json.dumps({"stringData": {"AB_LIVE_TEST_TOKEN": runtime["AB_LIVE_TEST_TOKEN"]}}))
        cd["AB_LIVE_TEST_TOKEN"] = runtime["AB_LIVE_TEST_TOKEN"]

    # The shared gateway secret may be supplied as an operator-owned htpasswd
    # override, in which case Terraform intentionally cannot recover its
    # plaintext password.  Give the public A/B edge a dedicated service
    # identity instead of rotating or weakening the existing gateway account.
    edge_user = cd.get("AB_EDGE_BASIC_USER")
    edge_password = cd.get("AB_EDGE_BASIC_PASSWORD")
    if bool(edge_user) != bool(edge_password):
        raise ValueError("incomplete Recommendation A/B edge credential")
    if not edge_user:
        edge_user = "recsys-ab"
        edge_password = secrets.token_urlsafe(32)
        kube(
            "ci",
            "patch",
            "secret",
            "recsys-llm-ab-cd",
            "--type=merge",
            "-p",
            json.dumps(
                {
                    "stringData": {
                        "AB_EDGE_BASIC_USER": edge_user,
                        "AB_EDGE_BASIC_PASSWORD": edge_password,
                    }
                }
            ),
        )
        cd["AB_EDGE_BASIC_USER"] = edge_user
        cd["AB_EDGE_BASIC_PASSWORD"] = edge_password
    htpasswd = subprocess.run(
        ["htpasswd", "-niB", "-C", "5", edge_user],
        input=edge_password + "\n",
        capture_output=True,
        text=True,
    )
    if htpasswd.returncode or not htpasswd.stdout.startswith(edge_user + ":$2"):
        raise RuntimeError("failed to derive Recommendation A/B edge htpasswd")
    apply(
        "kagent",
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "recsys-recommendation-ab-basic-auth"},
            "type": "Opaque",
            "stringData": {"auth": htpasswd.stdout.strip() + "\n"},
        },
    )
    # The Traffic Job needs the same service identity in plaintext for the
    # outbound HTTPS client. It is a separate Secret from NGINX's htpasswd and
    # is never mounted by the router, poller or application agents.
    apply(
        "kagent",
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "recsys-recommendation-ab-traffic-auth"},
            "type": "Opaque",
            "stringData": {
                "AB_EDGE_BASIC_USER": edge_user,
                "AB_EDGE_BASIC_PASSWORD": edge_password,
            },
        },
    )
    from urllib.parse import urlparse

    dbpass = urlparse(runtime["AB_DATABASE_URL"]).password
    exists = kube(
        "kagent",
        "exec",
        "deployment/kagent-postgresql",
        "--",
        "psql",
        "-U",
        "kagent",
        "-d",
        "kagent",
        "-Atc",
        "SELECT rolname FROM pg_roles WHERE rolname='recsys_ab'",
    ).strip()
    if not exists:
        kube(
            "kagent",
            "exec",
            "-i",
            "deployment/kagent-postgresql",
            "--",
            "psql",
            "-U",
            "kagent",
            "-d",
            "kagent",
            "-v",
            "ON_ERROR_STOP=1",
            data=f"CREATE ROLE recsys_ab LOGIN PASSWORD '{dbpass}';\n",
        )
    database = kube(
        "kagent",
        "exec",
        "deployment/kagent-postgresql",
        "--",
        "psql",
        "-U",
        "kagent",
        "-d",
        "kagent",
        "-Atc",
        "SELECT datname FROM pg_database WHERE datname='recsys_ab'",
    ).strip()
    if not database:
        kube(
            "kagent",
            "exec",
            "-i",
            "deployment/kagent-postgresql",
            "--",
            "psql",
            "-U",
            "kagent",
            "-d",
            "kagent",
            "-v",
            "ON_ERROR_STOP=1",
            data="CREATE DATABASE recsys_ab OWNER recsys_ab;\nREVOKE ALL ON DATABASE recsys_ab FROM PUBLIC;\n",
        )

    def policy(write):
        return json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetBucketVersioning", "s3:ListBucket"],
                        "Resource": ["arn:aws:s3:::recsys-llm-ab"],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject", "s3:GetObjectVersion"]
                        + (["s3:PutObject"] if write else []),
                        "Resource": ["arn:aws:s3:::recsys-llm-ab/*"],
                    },
                ],
            }
        )

    script = """set -eu
read -r ab_cd_password
read -r ab_runtime_password
read -r ab_cd_policy
read -r ab_runtime_policy
ab_mc_config=/tmp/recsys-ab-bootstrap-20260905
trap 'rm -f /tmp/recsys-ab-bootstrap-20260905/config.json' EXIT
mc --config-dir "$ab_mc_config" alias set ab http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc --config-dir "$ab_mc_config" mb --ignore-existing ab/recsys-llm-ab >/dev/null
mc --config-dir "$ab_mc_config" version enable ab/recsys-llm-ab >/dev/null
mc --config-dir "$ab_mc_config" admin user add ab recsys-ab-cd "$ab_cd_password" >/dev/null
mc --config-dir "$ab_mc_config" admin user add ab recsys-ab-runtime "$ab_runtime_password" >/dev/null
printf '%s' "$ab_cd_policy" | mc --config-dir "$ab_mc_config" admin policy create ab recsys-ab-cd /dev/stdin >/dev/null
printf '%s' "$ab_runtime_policy" | mc --config-dir "$ab_mc_config" admin policy create ab recsys-ab-runtime /dev/stdin >/dev/null
mc --config-dir "$ab_mc_config" admin policy attach ab recsys-ab-cd --user recsys-ab-cd >/dev/null
mc --config-dir "$ab_mc_config" admin policy attach ab recsys-ab-runtime --user recsys-ab-runtime >/dev/null
rm -f /tmp/recsys-ab-bootstrap-20260905/config.json
"""
    kube(
        "experiment-tracking",
        "exec",
        "-i",
        "deployment/minio",
        "--",
        "sh",
        "-c",
        script,
        data="\n".join(
            [
                cd["AWS_SECRET_ACCESS_KEY"],
                runtime["AWS_SECRET_ACCESS_KEY"],
                policy(True),
                policy(False),
            ]
        )
        + "\n",
    )
    with forward("experiment-tracking", "minio", 9000) as endpoint:
        client = boto3.client(
            "s3", endpoint_url=endpoint,
            aws_access_key_id=cd["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=cd["AWS_SECRET_ACCESS_KEY"],
            region_name=cd.get("AWS_DEFAULT_REGION", "us-east-1"),
        )
        seed_legacy_catalog_attestations(client)
    print(
        "Dedicated DB, versioned bucket and scoped runtime/CD accounts ready; credentials not printed"
    )


def initialize(manifest_path):
    cd = secret("ci", "recsys-llm-ab-cd")
    champion = release(json.loads(Path(manifest_path).read_text()))
    with forward("experiment-tracking", "minio", 9000) as endpoint:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=cd["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=cd["AWS_SECRET_ACCESS_KEY"],
            region_name="us-east-1",
        )
        from .state import StateStore

        store = StateStore(cd["AB_STATE_URI"], client)
        try:
            existing, _ = store.read()
        except client.exceptions.NoSuchKey:
            existing = None
        if existing:
            if existing["champion"]["release_id"] != champion["release_id"]:
                raise RuntimeError("existing champion differs; refusing replacement")
        else:
            store.write(
                {
                    "phase": "IDLE",
                    "champion": champion,
                    "previous": None,
                    "baseline": champion,
                    "pending": champion,
                    "disabled": [],
                    "releases": {champion["release_id"]: champion},
                    "events": [],
                    "experiment_ids": [],
                    "policy": DEFAULT_POLICY,
                    "policy_checksum": digest(DEFAULT_POLICY),
                },
                None,
            )
        client.put_object(
            Bucket="recsys-llm-ab",
            Key="releases/" + champion["release_id"] + ".json",
            Body=json.dumps(champion).encode(),
            ContentType="application/json",
        )
    print("Champion snapshot and create-only state ready")


def install_jenkins(image):
    """Update managed Recommendation jobs; preserve activation and unrelated jobs."""
    import requests

    cd = secret("ci", "recsys-llm-ab-cd")
    admin = secret("ci", "recsys-jenkins-admin")
    # Public-edge Basic Auth is injected through a dedicated Jenkins
    # UsernamePassword credential.  It must never be duplicated into the
    # generic JSON file consumed by the CD CLI allowlist.
    env = {
        k: v
        for k, v in cd.items()
        if k not in {"AB_STATE_URI", "AB_EDGE_BASIC_USER", "AB_EDGE_BASIC_PASSWORD"}
    }
    script = Path("jenkins/LLMAgentCD.Jenkinsfile").read_text()
    script = script.replace("['scm', 'deployed-image']", "['deployed-image']")
    script = script.replace(
        "s3://recsys-model-store/llm-agent-cd/recommendation/state.json",
        cd["AB_STATE_URI"],
    )
    script = script.replace(
        "string(name: 'ROUTER_IMAGE', defaultValue: ''",
        "string(name: 'ROUTER_IMAGE', defaultValue: '" + image + "'",
    )
    baseline = Path("jenkins/RecommendationBaselineMigration.Jenkinsfile").read_text()
    baseline = baseline.replace(
        "string(name: 'ROUTER_IMAGE', defaultValue: ''",
        "string(name: 'ROUTER_IMAGE', defaultValue: '" + image + "'",
    )
    onboarding = Path("jenkins/LLMCandidateOnboard.Jenkinsfile").read_text()
    onboarding = onboarding.replace(
        "string(name: 'ROUTER_IMAGE', defaultValue: ''",
        "string(name: 'ROUTER_IMAGE', defaultValue: '" + image + "'",
    )
    jobs = {
        "RecSys-LLM-Agent-CD": script,
        "RecSys-Recommendation-Baseline-Migration": baseline,
        "RecSys-LLM-Candidate-Onboard": onboarding,
    }
    encoded_env = base64.b64encode(json.dumps(env).encode()).decode()
    encoded_jobs = base64.b64encode(json.dumps(jobs).encode()).decode()
    edge_user = cd.get("AB_EDGE_BASIC_USER") or os.environ.get("AB_EDGE_BASIC_USER", "")
    edge_password = cd.get("AB_EDGE_BASIC_PASSWORD") or os.environ.get("AB_EDGE_BASIC_PASSWORD", "")
    if not edge_user or not edge_password:
        raise ValueError("AB_EDGE_BASIC_USER and AB_EDGE_BASIC_PASSWORD are required")
    encoded_edge_user = base64.b64encode(edge_user.encode()).decode()
    encoded_edge_password = base64.b64encode(edge_password.encode()).decode()
    groovy = """
import jenkins.model.Jenkins
import com.cloudbees.plugins.credentials.CredentialsScope
import com.cloudbees.plugins.credentials.SystemCredentialsProvider
import com.cloudbees.plugins.credentials.domains.Domain
import com.cloudbees.plugins.credentials.SecretBytes
import org.jenkinsci.plugins.plaincredentials.impl.FileCredentialsImpl
import com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl
import org.jenkinsci.plugins.workflow.job.WorkflowJob
import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition
import hudson.model.ChoiceParameterDefinition
import hudson.model.ParametersDefinitionProperty
import hudson.model.StringParameterDefinition
def j = Jenkins.get()
def provider = SystemCredentialsProvider.getInstance()
def existing = provider.getCredentials().find { it.id == 'recsys-llm-ab-env' }
def desiredEnv = new FileCredentialsImpl(CredentialsScope.GLOBAL, 'recsys-llm-ab-env', 'LLM Agent CD scoped credentials', 'llm-ab-env.json', SecretBytes.fromBytes('__ENV__'.decodeBase64()))
if (existing == null) {
  provider.getStore().addCredentials(Domain.global(), desiredEnv)
} else {
  if (!(existing instanceof FileCredentialsImpl) || existing.description != 'LLM Agent CD scoped credentials') {
    throw new IllegalStateException('Refusing to replace foreign recsys-llm-ab-env credential')
  }
  provider.getStore().updateCredentials(Domain.global(), existing, desiredEnv)
}
def edge = provider.getCredentials().find { it.id == 'recsys-agents-edge-auth' }
def desiredEdge = new UsernamePasswordCredentialsImpl(
  CredentialsScope.GLOBAL, 'recsys-agents-edge-auth',
  'Recommendation A/B public edge Basic Auth',
  new String('__EDGE_USER__'.decodeBase64(), 'UTF-8'),
  new String('__EDGE_PASSWORD__'.decodeBase64(), 'UTF-8'))
if (edge == null) {
  provider.getStore().addCredentials(Domain.global(), desiredEdge)
} else {
  if (!(edge instanceof UsernamePasswordCredentialsImpl) || edge.description != 'Recommendation A/B public edge Basic Auth') {
    throw new IllegalStateException('Refusing to replace foreign recsys-agents-edge-auth credential')
  }
  provider.getStore().updateCredentials(Domain.global(), edge, desiredEdge)
}
def jobs = new groovy.json.JsonSlurperClassic().parseText(new String('__JOBS__'.decodeBase64(), 'UTF-8'))
def managedDescriptions = [
  'LLM Agent A/B production',
  'Immutable config/LLM A/B delivery',
  'Generic GGUF candidate onboarding'
]
jobs.each { name, source ->
  def job = j.getItem(name)
  def managed = job == null || managedDescriptions.any { prefix -> job.description?.startsWith(prefix) }
  if (job != null && (!managed || job.isBuilding())) {
    throw new IllegalStateException('Refusing to replace foreign or running job: ' + name)
  }
  if (job == null) job = j.createProject(WorkflowJob, name)
  job.description = name == 'RecSys-LLM-Candidate-Onboard' ?
    'Generic GGUF candidate onboarding: artifact attestation, zero-traffic preparation and compatibility.' :
    'LLM Agent A/B production: digest-pinned runtime source, durable state and production release lock.'
  job.definition = new CpsFlowDefinition(source, true)
  if (name in ['RecSys-LLM-Agent-CD', 'RecSys-LLM-Candidate-Onboard']) {
    def parameters = job.getProperty(ParametersDefinitionProperty.class)
    def definitions = parameters == null ? [] : parameters.parameterDefinitions.collect { it }
    if (name == 'RecSys-LLM-Agent-CD') {
      definitions = [
        new ChoiceParameterDefinition('SOURCE_MODE', ['deployed-image'] as String[],
          'Use the digest-pinned production image.'),
        new ChoiceParameterDefinition('ACTION',
          ['prepare', 'route', 'promote', 'rollback', 'cleanup'] as String[],
          'One controller-authorized action per build.'),
        new StringParameterDefinition('ACTION_KEY', '', 'Immutable controller action identity.'),
        new StringParameterDefinition('EXPERIMENT_ID', '', 'Recommendation experiment ID.'),
        new StringParameterDefinition('EXPECTED_PHASE', '', 'Controller state phase.'),
        new StringParameterDefinition('EXPECTED_STATE_ETAG', '', 'Controller state ETag.'),
        new ChoiceParameterDefinition('TARGET_WEIGHT', ['', '0', '10', '50', '100'] as String[],
          'Route action target.'),
        new ChoiceParameterDefinition('EXPERIMENT_TYPE', ['llm_only', 'config_only', 'combined'] as String[],
          'Prepare-only experiment axis.'),
        new StringParameterDefinition('BASELINE_RELEASE_ID', '', 'Expected prepare baseline.'),
        new StringParameterDefinition('CANDIDATE_MANIFEST', '', 'Prepare-only candidate URI.'),
        new StringParameterDefinition('STATE_URI', 's3://recsys-llm-ab/recommendation/state.json',
          'Recommendation CAS state.'),
        new StringParameterDefinition('ROUTER_IMAGE',
          new String('__IMAGE__'.decodeBase64(), 'UTF-8'), 'Digest-pinned router image.'),
        new StringParameterDefinition('POLICY', 'configs/llm-ab/recommendation-live-test-policy.json',
          'Frozen gate policy.'),
        new StringParameterDefinition('FIXTURES', 'configs/llm-ab/cases.json',
          'Exactly 20 fixtures.'),
        new StringParameterDefinition('REASON', '', 'Controller rollback reason.')
      ]
    } else if (name == 'RecSys-LLM-Candidate-Onboard' && definitions.isEmpty()) {
      definitions = [
        new ChoiceParameterDefinition(
          'ACTION', ['prepare', 'cleanup'] as String[],
          'Prepare at 0% traffic or retire an expired prepared candidate.'),
        new StringParameterDefinition(
          'ONBOARDING_ID', '', 'Deterministic onb- identity.'),
        new StringParameterDefinition(
          'INTENT_URI', '', 'Create-only onboarding intent in MinIO.'),
        new StringParameterDefinition(
          'ROUTER_IMAGE', new String('__IMAGE__'.decodeBase64(), 'UTF-8'),
          'Built llm_ab_router image pinned by digest.')
      ]
    } else {
      definitions = definitions.collect { definition ->
        if (definition.name == 'ROUTER_IMAGE') {
          return new StringParameterDefinition(
              'ROUTER_IMAGE',
              new String('__IMAGE__'.decodeBase64(), 'UTF-8'),
              'Built llm_ab_router image pinned by digest.')
        }
        return definition
      }
    }
    if (!definitions.isEmpty()) {
      if (parameters != null) {
        job.removeProperty(ParametersDefinitionProperty.class)
      }
      job.addProperty(new ParametersDefinitionProperty(definitions))
    }
  }
  job.save()
  println('Ready: ' + name)
}
""".replace("__ENV__", encoded_env).replace("__JOBS__", encoded_jobs).replace(
    "__IMAGE__", base64.b64encode(image.encode()).decode()).replace(
    "__EDGE_USER__", encoded_edge_user).replace("__EDGE_PASSWORD__", encoded_edge_password)
    with forward("ci", "recsys-jenkins", 8080) as endpoint:
        session = requests.Session()
        session.auth = (admin["username"], admin["password"])
        crumb = session.get(endpoint + "/crumbIssuer/api/json", timeout=15)
        crumb.raise_for_status()
        session.headers[crumb.json()["crumbRequestField"]] = crumb.json()["crumb"]
        result = session.post(
            endpoint + "/scriptText", data={"script": groovy}, timeout=30
        )
        result.raise_for_status()
        # Error output could contain scripts; never echo it, only expected markers.
        for job in jobs:
            if "Ready: " + job not in result.text:
                raise RuntimeError(
                    "Jenkins scoped job installation failed; inspect server logs"
                )
            print("Ready: " + job)


def observability():
    """Add only A/B telemetry to live resources; preserve unrelated Helm drift."""
    import yaml

    ns = "observability"

    def get(kind, name):
        return json.loads(kube(ns, "get", kind, name, "-o", "json"))

    def patch(kind, obj, path, value):
        ops = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": obj["metadata"]["resourceVersion"],
            },
            {"op": "add", "path": path, "value": value},
        ]
        print(
            kube(
                ns,
                "patch",
                kind,
                obj["metadata"]["name"],
                "--type=json",
                "--patch-file=/dev/stdin",
                data=json.dumps(ops),
            ).strip()
        )

    dashboard = Path(
        "infra/helm/recsys-observability/dashboards/llm-ab-rollout.json"
    ).read_text()
    dashboard_name = "recsys-grafana-dashboard-llm-ab-rollout"
    print(
        apply(
            ns,
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": dashboard_name,
                    "labels": {
                        "grafana_dashboard": "1",
                        "app.kubernetes.io/name": "recsys-grafana",
                        "recsys.io/dashboard-folder": "RecSys",
                    },
                },
                "data": {"llm-ab-rollout.json": dashboard},
            },
        )
    )
    grafana = get("deployment", "recsys-grafana")
    volumes = grafana["spec"]["template"]["spec"]["volumes"]
    projected = next(v for v in volumes if v["name"] == "dashboards")["projected"][
        "sources"
    ]
    if not any(s.get("configMap", {}).get("name") == dashboard_name for s in projected):
        projected.append(
            {
                "configMap": {
                    "name": dashboard_name,
                    "items": [
                        {"key": "llm-ab-rollout.json", "path": "llm-ab-rollout.json"}
                    ],
                }
            }
        )
        patch("deployment", grafana, "/spec/template/spec/volumes", volumes)
    prom = get("configmap", "recsys-prometheus-config")
    config = yaml.safe_load(prom["data"]["prometheus.yml"])
    if not any(j["job_name"] == "recsys-llm-ab" for j in config["scrape_configs"]):
        config["scrape_configs"].append(
            {
                "job_name": "recsys-llm-ab",
                "metrics_path": "/metrics",
                "static_configs": [
                    {"targets": ["recsys-ab-router.kagent.svc.cluster.local:80"]}
                ],
            }
        )
        patch(
            "configmap",
            prom,
            "/data/prometheus.yml",
            yaml.safe_dump(config, sort_keys=False),
        )
    otel = get("configmap", "recsys-otel-collector-config")
    config = yaml.safe_load(otel["data"]["collector.yaml"])
    changed = False
    for block in config["processors"]["transform/operational_sanitize"][
        "trace_statements"
    ]:
        for i, statement in enumerate(block["statements"]):
            if statement.startswith("keep_keys(span.attributes"):
                for attr in (
                    "experiment_id",
                    "release_id",
                    "config_id",
                    "llm_version_id",
                    "source",
                ):
                    key = "recsys.ab." + attr
                    if json.dumps(key) not in statement:
                        statement = statement.replace(
                            "])", ", " + json.dumps(key) + "])"
                        )
                        changed = True
                block["statements"][i] = statement
    if changed:
        patch(
            "configmap",
            otel,
            "/data/collector.yaml",
            yaml.safe_dump(config, sort_keys=False),
        )
        deployment = get("deployment", "recsys-otel-collector")
        annotations = deployment["spec"]["template"]["metadata"].get("annotations", {})
        annotations["recsys.ai/ab-telemetry-checksum"] = digest(config)
        patch(
            "deployment", deployment, "/spec/template/metadata/annotations", annotations
        )
    print(
        "A/B dashboard and telemetry additions applied; reload Prometheus after projected config updates"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=["credentials", "legacy-attestations", "state", "jenkins", "observability"],
    )
    parser.add_argument("--champion")
    parser.add_argument("--image")
    args = parser.parse_args()
    if args.action == "credentials":
        credentials()
    elif args.action == "legacy-attestations":
        legacy_attestations()
    elif args.action == "state":
        initialize(args.champion)
    elif args.action == "observability":
        observability()
    else:
        install_jenkins(args.image)


if __name__ == "__main__":
    main()

"""Scoped workflow bootstrap; never replaces a live champion or legacy release."""
import argparse
import json
import secrets
import base64
import hashlib
from pathlib import Path
import boto3
from .provision import secret, apply, kube, forward
from .release import release, digest, policy
from .state import StateStore

STATE = "s3://recsys-llm-ab/workflow/state.json"


def acceptance_serving_profile(profile):
    """Return whether an operator-pinned profile may change with an LLM release."""
    from .serving_profiles import (SMALL, SMALL_V2, SMALL_B8646,
                                   SMALL_B8646_TERMINAL, QWEN35_NATIVE)

    return profile in {SMALL, SMALL_V2, SMALL_B8646,
                       SMALL_B8646_TERMINAL, QWEN35_NATIVE}


def install_jenkins():
    import requests
    from .evaluation_prepare import bundle, groovy_base64
    cd = secret("ci", "recsys-workflow-cd")
    admin = secret("ci", "recsys-jenkins-admin")
    allowed = {"AB_DATABASE_URL", "AB_INTERNAL_TOKEN", "AB_ROUTER_URL", "MODEL_STORE_ENDPOINT",
               "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"}
    env = base64.b64encode(json.dumps({k: v for k, v in cd.items() if k in allowed}).encode()).decode()
    runtime_bundle = bundle()
    runtime_checksum = hashlib.sha256(runtime_bundle).hexdigest()
    source = base64.b64encode(Path("jenkins/LLMWorkflowCD.Jenkinsfile").read_text().replace(
        '__SOURCE_BUNDLE_SHA256__', runtime_checksum).encode()).decode()
    global_source = base64.b64encode(Path("jenkins/GlobalModelConfig.Jenkinsfile").read_text().replace(
        '__SOURCE_BUNDLE_SHA256__', runtime_checksum).encode()).decode()
    script = '''
import jenkins.model.Jenkins
import com.cloudbees.plugins.credentials.CredentialsScope
import com.cloudbees.plugins.credentials.SystemCredentialsProvider
import com.cloudbees.plugins.credentials.domains.Domain
import com.cloudbees.plugins.credentials.SecretBytes
import org.jenkinsci.plugins.plaincredentials.impl.FileCredentialsImpl
import org.jenkinsci.plugins.workflow.job.WorkflowJob
import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition
import hudson.model.ParametersDefinitionProperty
import hudson.model.ChoiceParameterDefinition
import hudson.model.StringParameterDefinition
def j = Jenkins.get()
def job = j.getItem('RecSys-LLM-Workflow-CD')
if (job != null && (!job.description?.startsWith('Workflow A/B') || job.isBuilding())) {
  throw new IllegalStateException('Refusing to replace foreign/running workflow job')
}
def provider = SystemCredentialsProvider.getInstance()
def old = provider.getCredentials().find { it.id == 'recsys-workflow-ab-env' }
def credential = new FileCredentialsImpl(CredentialsScope.GLOBAL, 'recsys-workflow-ab-env', 'Workflow A/B scoped CD credentials', 'workflow-env.json', SecretBytes.fromBytes('__ENV__'.decodeBase64()))
if (old == null) provider.getStore().addCredentials(Domain.global(), credential)
else {
  if (!old.description?.startsWith('Workflow A/B')) throw new IllegalStateException('Foreign credential')
  provider.getStore().updateCredentials(Domain.global(), old, credential)
}
def oldSource = provider.getCredentials().find { it.id == 'recsys-workflow-source' }
def sourceCredential = new FileCredentialsImpl(CredentialsScope.GLOBAL, 'recsys-workflow-source', 'Workflow A/B checksum-pinned source bundle', 'workflow-source.tgz', SecretBytes.fromBytes(__BUNDLE__.decodeBase64()))
if (oldSource == null) provider.getStore().addCredentials(Domain.global(), sourceCredential)
else {
  if (oldSource.description != 'Workflow A/B checksum-pinned source bundle') throw new IllegalStateException('Foreign workflow source credential')
  provider.getStore().updateCredentials(Domain.global(), oldSource, sourceCredential)
}
if (job == null) job = j.createProject(WorkflowJob, 'RecSys-LLM-Workflow-CD')
job.description = 'Workflow A/B: immutable releases, shared deployment lock, no post-promotion monitoring.'
job.definition = new CpsFlowDefinition(new String('__SOURCE__'.decodeBase64(), 'UTF-8'), true)
job.addProperty(new ParametersDefinitionProperty([
  new ChoiceParameterDefinition('ACTION', ['resume', 'activate', 'run', 'rollback', 'restore-baseline'] as String[], 'Workflow action'),
  new ChoiceParameterDefinition('EXPERIMENT_TYPE', ['config_only', 'llm_only', 'combined'] as String[], 'Change axes'),
  new StringParameterDefinition('EXPERIMENT_ID', '', 'Idempotent experiment ID'),
  new StringParameterDefinition('BASELINE_RELEASE_ID', '', 'Expected baseline'),
  new StringParameterDefinition('CANDIDATE_MANIFEST', '', 'Content-addressed manifest'),
  new ChoiceParameterDefinition('POLICY', ['configs/llm-ab/workflow-production-policy.json', 'configs/llm-ab/workflow-live-test-policy.json'] as String[], 'Frozen policy')
]))
job.save()
def globalJob = j.getItem('RecSys-Global-Model-Config')
if (globalJob != null && (!globalJob.description?.startsWith('Workflow A/B global configuration') || globalJob.isBuilding())) {
  throw new IllegalStateException('Refusing foreign/running global job')
}
if (globalJob == null) globalJob = j.createProject(WorkflowJob, 'RecSys-Global-Model-Config')
globalJob.description = 'Workflow A/B global configuration: shared release lock; no direct Helm fallback.'
globalJob.definition = new CpsFlowDefinition(new String('__GLOBAL_SOURCE__'.decodeBase64(), 'UTF-8'), true)
globalJob.addProperty(new ParametersDefinitionProperty([
  new hudson.model.TextParameterDefinition('VALUES_JSON', '', 'Operator global Helm values')
]))
globalJob.save()
println('WORKFLOW_JOB_READY')
'''.replace("__ENV__", env).replace("__BUNDLE__", groovy_base64(runtime_bundle)).replace(
        "__SOURCE__", source).replace("__GLOBAL_SOURCE__", global_source)
    with forward("ci", "recsys-jenkins", 8080) as endpoint:
        client = requests.Session()
        client.auth = (admin["username"], admin["password"])
        crumb = client.get(endpoint + "/crumbIssuer/api/json", timeout=15)
        crumb.raise_for_status()
        client.headers[crumb.json()["crumbRequestField"]] = crumb.json()["crumb"]
        result = client.post(endpoint + "/scriptText", data={"script": script}, timeout=30)
        result.raise_for_status()
        if "WORKFLOW_JOB_READY" not in result.text:
            raise RuntimeError("Workflow Jenkins installation failed; secret-bearing response withheld")
    print("RecSys-LLM-Workflow-CD ready; pinned source " + runtime_checksum + "; unrelated jobs unchanged")


def credentials():
    old = secret("kagent", "recsys-llm-ab-runtime")
    if not old:
        raise RuntimeError("existing A/B DB and bucket must be provisioned first")
    runtime = secret("kagent", "recsys-workflow-runtime")
    if runtime is None:
        runtime = {**old, "AB_STATE_URI": STATE, "AB_INTERNAL_TOKEN": secrets.token_urlsafe(48),
                   "AB_LIVE_TEST_TOKEN": secrets.token_urlsafe(48), "AWS_ACCESS_KEY_ID": "recsys-workflow-runtime",
                   "AWS_SECRET_ACCESS_KEY": secrets.token_urlsafe(40),
                   "AB_ROUTER_URL": "http://recsys-workflow-router.kagent.svc.cluster.local"}
        apply("kagent", {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "recsys-workflow-runtime"}, "stringData": runtime})
    cd = secret("ci", "recsys-workflow-cd")
    if cd is None:
        cd = {**runtime, "AWS_ACCESS_KEY_ID": "recsys-workflow-cd", "AWS_SECRET_ACCESS_KEY": secrets.token_urlsafe(40)}
        apply("ci", {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "recsys-workflow-cd"}, "stringData": cd})
    for account in (runtime, cd):
        write = account is cd
        acl = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetBucketVersioning"], "Resource": ["arn:aws:s3:::recsys-llm-ab"]},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"] + (["s3:PutObject"] if write else []),
             "Resource": ["arn:aws:s3:::recsys-llm-ab/workflow/*"]}]}
        # The CLI reads credentials/policy from stdin. Do not print its output or
        # embed secret values in command arguments / process listings.
        script = '''set -eu
read -r account
read -r password
read -r acl
config_dir=$(mktemp -d /tmp/workflow-mc.XXXXXX)
mc --config-dir "$config_dir" alias set workflow http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc --config-dir "$config_dir" admin user add workflow "$account" "$password" >/dev/null
printf '%s' "$acl" | mc --config-dir "$config_dir" admin policy create workflow "$account" /dev/stdin >/dev/null
mc --config-dir "$config_dir" admin policy attach workflow "$account" --user "$account" >/dev/null
rm -f -- "$config_dir/config.json"
rmdir -- "$config_dir/certs/CAs" "$config_dir/certs" "$config_dir" 2>/dev/null || true
'''
        kube("experiment-tracking", "exec", "-i", "deployment/minio", "--", "sh", "-c", script,
             data="\n".join([account["AWS_ACCESS_KEY_ID"], account["AWS_SECRET_ACCESS_KEY"], json.dumps(acl)]) + "\n")
    print("Scoped workflow runtime/CD credentials ready; no credential values printed")


def initialize(path):
    baseline = release(json.loads(Path(path).read_text()))
    if baseline.get("scope") != "workflow":
        raise ValueError("workflow baseline required")
    cd = secret("ci", "recsys-workflow-cd")
    rules = policy(json.loads(Path("configs/llm-ab/workflow-production-policy.json").read_text()))
    with forward("experiment-tracking", "minio", 9000) as endpoint:
        s3 = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                          aws_access_key_id=cd["AWS_ACCESS_KEY_ID"], aws_secret_access_key=cd["AWS_SECRET_ACCESS_KEY"])
        store = StateStore(STATE, s3)
        try:
            state, etag = store.read()
        except s3.exceptions.NoSuchKey:
            state = None
        if state is not None and state["champion"] != baseline:
            raise RuntimeError("existing workflow state differs; refusing bootstrap overwrite")
        if state is None:
            store.write({"phase": "IDLE", "champion": baseline, "baseline": baseline, "pending": baseline,
                         "previous": None, "disabled": [], "releases": {baseline["release_id"]: baseline},
                         "events": [], "experiment_ids": [], "policy": rules, "policy_checksum": digest(rules)}, None)
        for key, value in (("workflow/releases/" + baseline["release_id"] + ".json", baseline),
                           ("workflow/catalog/" + baseline["llm_version_id"] + ".json", baseline["llm"])):
            body = json.dumps(value, sort_keys=True).encode()
            try:
                s3.put_object(Bucket=store.bucket, Key=key, Body=body, ContentType="application/json", IfNoneMatch="*")
            except s3.exceptions.ClientError as exc:
                if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                    raise
                if s3.get_object(Bucket=store.bucket, Key=key)["Body"].read() != body:
                    raise RuntimeError("immutable bootstrap object conflict")
    print("Workflow baseline create-only state/catalog ready: " + baseline["release_id"])


def register_catalog(path, baseline_path, artifact_path):
    """Operator-only catalog publication; never starts a model or changes routes."""
    from apps.agentic.llm_ab_router.trigger import candidate_from_config
    llm = json.loads(Path(path).read_text())
    baseline = release(json.loads(Path(baseline_path).read_text()))
    from .serving_profiles import validate_profile
    validate_profile(llm)
    # Small model uses an explicit, hash-bound deployment profile. Unprofiled
    # catalogs retain the original quantization-only acceptance restriction.
    if llm["image"] != baseline["llm"]["image"] or (
        llm["serving"] != baseline["llm"]["serving"]
        and not acceptance_serving_profile(llm["serving"].get("resourceProfile"))
    ):
        raise ValueError("acceptance LLM catalog must preserve serving runtime/settings")
    ref = digest(llm)
    candidate_from_config(baseline, {"schema_version": 1, "scope": "workflow",
        "baseline_workflow_release_id": baseline["release_id"], "global_generation": baseline["global_generation"],
        "llm_release_ref": ref, "experiment_type": "llm_only", "policy_ref": "workflow-production"}, llm)
    checksum = hashlib.sha256()
    with Path(artifact_path).open("rb") as artifact:
        while chunk := artifact.read(1024 * 1024):
            checksum.update(chunk)
    if checksum.hexdigest() != llm["artifact_sha256"]:
        raise ValueError("downloaded catalog artifact checksum mismatch")
    cd = secret("ci", "recsys-workflow-cd")
    with forward("experiment-tracking", "minio", 9000) as endpoint:
        s3 = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                         aws_access_key_id=cd["AWS_ACCESS_KEY_ID"], aws_secret_access_key=cd["AWS_SECRET_ACCESS_KEY"])
        key = "workflow/catalog/" + ref + ".json"
        body = json.dumps(llm, sort_keys=True).encode()
        try:
            s3.put_object(Bucket="recsys-llm-ab", Key=key, Body=body, ContentType="application/json", IfNoneMatch="*")
        except s3.exceptions.ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                raise
            if s3.get_object(Bucket="recsys-llm-ab", Key=key)["Body"].read() != body:
                raise RuntimeError("immutable catalog conflict")
    print(json.dumps({"llm_version_id": ref, "artifact_sha256": checksum.hexdigest(), "registered": True,
                      "serving_started": False, "artifact_bytes": Path(artifact_path).stat().st_size}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["credentials", "initialize", "jenkins", "catalog"])
    parser.add_argument("--baseline")
    parser.add_argument("--catalog")
    parser.add_argument("--artifact")
    args = parser.parse_args()
    if args.action == "credentials":
        credentials()
    elif args.action == "jenkins":
        install_jenkins()
    elif args.action == "catalog":
        register_catalog(args.catalog, args.baseline, args.artifact)
    else:
        initialize(args.baseline)

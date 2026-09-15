from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from jenkins.python.change_detection.detector import (
    ChangedFile,
    detect_changed_components,
)

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "configs/agentic/mcp-auth-versions.yaml"
VALIDATOR = ROOT / "ops/security/mcp_auth_versions.py"
TEST_COMMIT = "a" * 40


def _validator_module():
    spec = importlib.util.spec_from_file_location("mcp_auth_versions", VALIDATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rotation_manifest_selects_every_consumer_and_coordinator_verification():
    result = detect_changed_components(
        [ChangedFile("M", "configs/agentic/mcp-auth-versions.yaml")],
        commit=TEST_COMMIT,
    )

    assert result.component_names == (
        "feature_rag_mcp",
        "context_agent",
        "recommendation_mcp",
        "recommendation_agent",
        "coordinator_agent",
    )
    units = result.release_plan["deployUnits"]
    assert units.index("feature-rag-mcp") < units.index("context-agent")
    assert units.index("recommendation-mcp") < units.index("recommendation-agent")
    assert units.index("context-agent") < units.index("coordinator-agent")
    assert units.index("recommendation-agent") < units.index("coordinator-agent")


def test_reset_values_deploy_always_reapplies_non_secret_rotation_manifest():
    runtime = (ROOT / "jenkins/scripts/deploy/release_unit_runtime.sh").read_text(
        encoding="utf-8"
    )
    rotation = (ROOT / "jenkins/scripts/deploy/agentic/rotation.sh").read_text(
        encoding="utf-8"
    )

    for unit in (
        "feature-rag-mcp",
        "context-agent",
        "recommendation-mcp",
        "recommendation-agent",
    ):
        assert unit in rotation
    assert 'mcp_auth_chart_consumes_manifest "${unit_name}"' in runtime
    assert "mcp_auth_validate" in runtime
    assert 'helm_args+=(-f "$(mcp_auth_versions_file)")' in runtime
    assert runtime.index('helm_args+=(-f "$(mcp_auth_versions_file)")') < runtime.index(
        "--reset-values"
    )
    assert "MCP_AUTH_TOKEN" not in rotation
    assert "Authorization" not in rotation
    assert "mcp_auth_verify_prepare" in runtime
    assert "mcp_auth_continuous_probe.sh" in runtime
    assert "mcp_auth_retirement_gate.sh" in runtime
    registry = (ROOT / "jenkins/scripts/deploy/agentic/registry.sh").read_text(
        encoding="utf-8"
    )
    assert 'policy="$(mcp_auth_image_policy "${workload_unit}")"' in registry
    assert '"${reference}" != *@sha256:*' in runtime
    assert registry.index('policy="$(mcp_auth_image_policy') < registry.index(
        'reference="$(image_manifest_lookup "${image_name}")"'
    )
    assert "installed-digest" in rotation


@pytest.mark.parametrize(
    ("unit", "transition", "expected"),
    (
        ("feature-rag-mcp", "featureRag:prepare", "installed-digest"),
        (
            "recommendation-mcp",
            "recommendation:cutover-or-rollback",
            "installed-digest",
        ),
        ("recommendation-mcp", "bootstrap", "installed-digest"),
        ("feature-rag-mcp", "unchanged", "release-artifact"),
        ("context-agent", "featureRag:prepare", "release-artifact"),
    ),
)
def test_rotation_image_policy_preserves_installed_mcp_digest(
    unit: str, transition: str, expected: str
):
    rotation = ROOT / "jenkins/scripts/deploy/agentic/rotation.sh"
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; transition_value="$2"; '
            'mcp_auth_transition() { printf "%s\\n" "${transition_value}"; }; '
            'mcp_auth_image_policy "$3"',
            "mcp-auth-image-policy-test",
            str(rotation),
            transition,
            unit,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == expected


def test_registry_and_runtime_resolve_active_workload_instead_of_fixed_slot():
    registry = (ROOT / "jenkins/scripts/deploy/agentic/registry.sh").read_text(
        encoding="utf-8"
    )
    smoke = (ROOT / "jenkins/scripts/deploy/agentic/mcp.sh").read_text(encoding="utf-8")

    assert 'mcp_auth_active_url "${remote_key}"' in registry
    assert (
        '"url": "http://recsys-feature-rag-mcp.kagent.svc.cluster.local:8080/mcp"'
        not in registry
    )
    assert (
        '"url": "http://recsys-recommendation-mcp.kagent.svc.cluster.local:8080/mcp"'
        not in registry
    )
    assert "mcp_auth_active_workload featureRag" in smoke
    assert "mcp_auth_active_workload recommendation" in smoke


def test_checked_in_manifest_and_purged_shape_validate():
    module = _validator_module()
    manifest = module.load_manifest(MANIFEST)
    assert manifest["version"] == 1
    for service in manifest["services"].values():
        assert service["activeRevision"] == "v1"
        assert service["revisions"]["legacy"]["deploy"] is True
        assert service["revisions"]["v1"]["vaultVersion"] == "1"

    purged = copy.deepcopy(manifest)
    for service in purged["services"].values():
        service["activeRevision"] = "v1"
        del service["revisions"]["legacy"]
    assert module.validate_manifest(purged) == purged


def test_transition_validator_enforces_one_phase_and_one_service_per_commit():
    module = _validator_module()
    original = module.load_manifest(MANIFEST)
    for service in original["services"].values():
        service["activeRevision"] = "legacy"

    cutover = copy.deepcopy(original)
    cutover["services"]["recommendation"]["activeRevision"] = "v1"
    assert module.validate_transition(original, cutover) == (
        "recommendation:cutover-or-rollback"
    )

    retired = copy.deepcopy(cutover)
    retired["services"]["recommendation"]["revisions"]["legacy"]["deploy"] = False
    assert module.validate_transition(cutover, retired) == "recommendation:retire"

    purged = copy.deepcopy(retired)
    del purged["services"]["recommendation"]["revisions"]["legacy"]
    assert module.validate_transition(retired, purged) == "recommendation:purge"

    unsafe = copy.deepcopy(original)
    unsafe["services"]["recommendation"]["activeRevision"] = "v1"
    unsafe["services"]["recommendation"]["revisions"]["legacy"]["deploy"] = False
    with pytest.raises(module.ManifestError):
        module.validate_transition(original, unsafe)

    two_services = copy.deepcopy(original)
    for service in two_services["services"].values():
        service["activeRevision"] = "v1"
    with pytest.raises(module.ManifestError):
        module.validate_transition(original, two_services)


def test_transition_validator_accepts_prepare_cutover_rollback_then_retirement():
    module = _validator_module()
    original = module.load_manifest(MANIFEST)

    migrated = copy.deepcopy(original)
    migrated["services"]["recommendation"]["activeRevision"] = "v1"

    prepared = copy.deepcopy(migrated)
    prepared["services"]["recommendation"]["revisions"]["v2"] = {
        "vaultVersion": "2",
        "secretName": "recsys-recommendation-mcp-auth-v2",
        "workloadName": "recsys-recommendation-mcp-v2",
        "deploy": True,
    }
    assert module.validate_transition(migrated, prepared) == "recommendation:prepare"

    cutover = copy.deepcopy(prepared)
    cutover["services"]["recommendation"]["activeRevision"] = "v2"
    assert module.validate_transition(prepared, cutover) == (
        "recommendation:cutover-or-rollback"
    )

    rollback = copy.deepcopy(cutover)
    rollback["services"]["recommendation"]["activeRevision"] = "v1"
    assert module.validate_transition(cutover, rollback) == (
        "recommendation:cutover-or-rollback"
    )

    retired = copy.deepcopy(cutover)
    retired["services"]["recommendation"]["revisions"]["v1"]["deploy"] = False
    assert module.validate_transition(cutover, retired) == "recommendation:retire"


@pytest.mark.parametrize(
    "mutation",
    (
        "missing-active",
        "inactive-active",
        "version-mismatch",
        "duplicate-secret",
        "credential-field",
    ),
)
def test_validator_rejects_unsafe_rotation_manifests(mutation: str):
    module = _validator_module()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    feature = manifest["services"]["featureRag"]

    if mutation == "missing-active":
        feature["activeRevision"] = "v999"
    elif mutation == "inactive-active":
        feature["activeRevision"] = "v1"
        feature["revisions"]["v1"]["deploy"] = False
    elif mutation == "version-mismatch":
        feature["revisions"]["v1"]["vaultVersion"] = "2"
    elif mutation == "duplicate-secret":
        manifest["services"]["recommendation"]["revisions"]["v1"]["secretName"] = (
            feature["revisions"]["v1"]["secretName"]
        )
    else:
        feature["revisions"]["v1"]["token"] = "must-never-be-here"

    with pytest.raises(module.ManifestError):
        module.validate_manifest(manifest)


def test_rotation_operational_scripts_are_syntax_checked_and_keep_tokens_in_pods():
    scripts = (
        ROOT / "ops/validation/mcp_auth_rotation_matrix.sh",
        ROOT / "ops/validation/mcp_auth_continuous_probe.sh",
        ROOT / "ops/validation/mcp_auth_rotation_gate.sh",
        ROOT / "ops/validation/mcp_auth_fresh_a2a_smoke.sh",
        ROOT / "ops/validation/mcp_auth_retirement_gate.sh",
        ROOT / "ops/security/validate_mcp_auth_terraform.sh",
    )
    for script in scripts:
        subprocess.run(["bash", "-n", str(script)], check=True)

    matrix = scripts[0].read_text(encoding="utf-8")
    assert 'kubectl -n "${namespace}" get secret' not in matrix
    assert 'get "${resource}/${name}" -o json' not in matrix
    assert "-o jsonpath=" in matrix
    assert 'os.environ["MCP_AUTH_TOKEN"]' in matrix
    assert '"Content-Type": "application/json"' in matrix
    assert "response.body" not in matrix
    assert "response.text" not in matrix

    rollout_gate = scripts[2].read_text(encoding="utf-8")
    retirement_gate = scripts[4].read_text(encoding="utf-8")
    continuous_probe = scripts[1].read_text(encoding="utf-8")
    assert '"Content-Type": "application/json"' in continuous_probe
    assert "probe duration must be a positive integer" in continuous_probe
    assert "attempts == 0 or failures" in continuous_probe
    assert "MCP_AUTH_ROTATION_SESSION_EVIDENCE" in rollout_gate
    assert "expected_actor_ids_json" in rollout_gate
    assert "preexisting_expected_actor_count" in rollout_gate
    assert "refusing to run MCP auth validation while shell xtrace" in rollout_gate
    assert 'get "secret/${expected_secret}"' in rollout_gate
    assert "-o jsonpath=" in rollout_gate
    assert "telemetry_current_query" in retirement_gate
    assert "telemetry_start_query" in retirement_gate
    assert "telemetry_samples_query" in retirement_gate
    assert "window_is_clean" in retirement_gate
    assert "source_workload!=" in retirement_gate
    assert "source_workload_namespace!=" in retirement_gate
    assert "recsys-prometheus" in retirement_gate

    a2a = (ROOT / "jenkins/scripts/deploy/agentic/a2a.sh").read_text(encoding="utf-8")
    kubernetes = (ROOT / "jenkins/scripts/deploy/agentic/kubernetes.sh").read_text(
        encoding="utf-8"
    )
    assert a2a.count("MCP_AUTH_ROTATION_SESSION_EVIDENCE") == 2
    assert "successful_context_ids.append(context_id)" in a2a
    assert 'get "secret/${secret_name}" -o json' not in kubernetes
    assert "-o jsonpath='{.immutable}'" in kubernetes


def test_vault_rotation_helper_uses_cas_and_never_places_token_in_argv():
    helper_path = ROOT / "ops/security/rotate_mcp_auth_vault.sh"
    subprocess.run(["bash", "-n", str(helper_path)], check=True)
    helper = helper_path.read_text(encoding="utf-8")

    assert 'case "$-" in' in helper
    assert 'chmod 600 "${payload_file}" "${result_file}"' in helper
    assert '-cas="${expected_version}"' in helper
    assert '"@${payload_file}"' in helper
    assert '<<<"${token}"' in helper
    assert "MCP_AUTH_TOKEN=${token}" not in helper
    assert "printf '%s\\n' \"${written_version}\"" in helper
    assert "activeRevision" in helper
    assert "retire the mutable legacy workload" in helper
    assert "MCP_AUTH_RETIREMENT_EVIDENCE" in helper
    assert "deployment.apps/${legacy_workload}" in helper
    assert "--ignore-not-found -o name" in helper
    assert "Legacy MCP workload still has live Pods" in helper
    assert 'refreshPolicy == "CreatedOnce"' in helper
    assert ".spec.target.immutable == true" in helper
    assert "extract.version == $version" in helper
    assert "active_deployment_secret" in helper
    assert "active_deployment_image" in helper
    assert "@sha256:[0-9a-f]{64}" in helper
    assert 'active["vaultVersion"]' in helper
    assert "expected current Vault version predates a declared revision" in helper
    assert '--arg version "${active_vault_version}"' in helper
    assert "live_service_legacy_count" in helper
    assert "Legacy ExternalSecret has not been applied as deploy=false" in helper


def test_vault_rotation_helper_supports_new_head_after_rollback(tmp_path: Path):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    recommendation = manifest["services"]["recommendation"]
    recommendation["activeRevision"] = "v1"
    del recommendation["revisions"]["legacy"]
    recommendation["revisions"]["v2"] = {
        "vaultVersion": "2",
        "secretName": "recsys-recommendation-mcp-auth-v2",
        "workloadName": "recsys-recommendation-mcp-v2",
        "deploy": True,
    }
    manifest_path = tmp_path / "versions.yaml"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    kubectl = tmp_path / "kubectl"
    kubectl.write_text(
        """#!/usr/bin/env python3
import json
import sys

args = " ".join(sys.argv[1:])
if "rollout status deployment/recsys-recommendation-mcp-v1" in args:
    raise SystemExit(0)
if "get deployment.apps/recsys-recommendation-mcp-v1" in args:
    print(
        "recsys-recommendation-mcp-auth-v1|registry.example/mcp@sha256:"
        + "a" * 64,
        end="",
    )
    raise SystemExit(0)
if "get externalsecret.external-secrets.io/recsys-recommendation-mcp-auth-v1" in args:
    print(json.dumps({
        "spec": {
            "refreshPolicy": "CreatedOnce",
            "target": {
                "name": "recsys-recommendation-mcp-auth-v1",
                "immutable": True,
            },
            "dataFrom": [{"extract": {"version": "1"}}],
        },
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }))
    raise SystemExit(0)
if "get secret/recsys-recommendation-mcp-auth-v1" in args:
    print("recsys-recommendation-mcp-auth-v1\\ttrue", end="")
    raise SystemExit(0)
if "get externalsecrets.external-secrets.io" in args:
    print('{"items": []}')
    raise SystemExit(0)
print("unexpected kubectl call: " + args, file=sys.stderr)
raise SystemExit(97)
""",
        encoding="utf-8",
    )
    kubectl.chmod(0o755)

    vault = tmp_path / "vault"
    vault.write_text(
        """#!/usr/bin/env python3
import json
import os
import stat
import sys

assert all("test-rotation-token" not in arg for arg in sys.argv[1:])
payload_arg = sys.argv[-1]
assert payload_arg.startswith("@")
payload_path = payload_arg[1:]
assert stat.S_IMODE(os.stat(payload_path).st_mode) == 0o600
with open(payload_path, encoding="utf-8") as stream:
    payload = json.load(stream)
assert payload == {
    "MCP_AUTH_TOKEN": "test-rotation-token",
    "Authorization": "Bearer test-rotation-token",
}
print('{"data":{"version":3}}')
""",
        encoding="utf-8",
    )
    vault.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["MCP_AUTH_VERSIONS_FILE"] = str(manifest_path)
    result = subprocess.run(
        ["bash", "ops/security/rotate_mcp_auth_vault.sh", "recommendation", "2"],
        cwd=ROOT,
        env=env,
        input="test-rotation-token\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "3\n"


def test_terraform_and_jenkins_have_separate_destructive_phase_gates():
    terraform = (
        ROOT / "infra/terraform/gcp/modules/kubernetes-platform/secret_management.tf"
    ).read_text(encoding="utf-8")
    purge_gate = (ROOT / "ops/security/validate_mcp_auth_terraform.sh").read_text(
        encoding="utf-8"
    )
    pipeline = (ROOT / "Jenkinsfile").read_text(encoding="utf-8")
    llm_inference = (
        ROOT / "infra/terraform/gcp/modules/kubernetes-platform/llm_inference.tf"
    ).read_text(encoding="utf-8")
    kagent = (
        ROOT / "infra/terraform/gcp/modules/kubernetes-platform/kagent.tf"
    ).read_text(encoding="utf-8")

    assert 'resource "null_resource" "mcp_auth_versions_valid"' in terraform
    assert "validate_mcp_auth_terraform.sh" in terraform
    assert "mcp_auth_enabled  = tostring(var.config.deploy_llm_inference)" in terraform
    assert "MCP_AUTH_ENABLED" in terraform
    assert "MCP_AUTH_PURGE_APPROVED" in purge_gate
    assert "MCP_AUTH_RETIREMENT_EVIDENCE" in purge_gate
    assert "retirement_records" in purge_gate
    assert "separately reviewed full-stack teardown workflow" in purge_gate
    assert "namespace_exists=false" in purge_gate
    assert "--ignore-not-found -o name" in purge_gate
    assert "Unable to verify whether namespace" in purge_gate
    assert "Unable to verify retired resource state" in purge_gate
    assert "feature-rag|recommendation" in purge_gate
    assert "live_secret_resource_version" in purge_gate
    assert "evidence_secret_resource_version" in purge_gate
    assert "validation_attempt = timestamp()" in terraform
    assert "depends_on = [null_resource.mcp_auth_versions_valid]" not in kagent
    assert "kubernetes_namespace.kagent" in terraform
    assert 'resource "kubernetes_namespace" "kagent"' in kagent
    assert "outside the generic feature-disable" in kagent
    assert "count = 1" in kagent
    assert 'check "mcp_auth_rotation_dependencies"' in llm_inference
    assert "var.config.deploy_vault && var.config.deploy_service_mesh" in llm_inference
    assert "Terraform check blocks only warn" in (
        ROOT / "infra/terraform/gcp/modules/kubernetes-platform/recsys_services.tf"
    ).read_text(encoding="utf-8")
    assert "MCP_AUTH_RETIRE_APPROVED" in pipeline


def test_terraform_validator_fails_closed_when_namespace_lookup_fails(
    tmp_path: Path,
):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    kubectl.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["MCP_AUTH_ENABLED"] = "false"

    result = subprocess.run(
        ["bash", "ops/security/validate_mcp_auth_terraform.sh"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Unable to verify whether namespace" in result.stderr


def test_terraform_validator_accepts_authoritative_missing_namespace(
    tmp_path: Path,
):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    kubectl.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["MCP_AUTH_ENABLED"] = "false"

    result = subprocess.run(
        ["bash", "ops/security/validate_mcp_auth_terraform.sh"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mcpAuthEnabled"] is False


def test_specialized_helm_jobs_bundle_and_reapply_the_rotation_manifest():
    prepare = (ROOT / "jenkins/python/llm_agent_cd/evaluation_prepare.py").read_text(
        encoding="utf-8"
    )
    defaults = (
        ROOT / "jenkins/python/llm_agent_cd/default_agents_deploy.py"
    ).read_text(encoding="utf-8")
    context_contract = (
        ROOT / "jenkins/python/llm_agent_cd/context_contract_deploy.py"
    ).read_text(encoding="utf-8")

    assert "configs/agentic/mcp-auth-versions.yaml" in prepare
    assert "MCP_AUTH_VERSIONS" in defaults
    assert "helm_args.extend(('-f', str(MCP_AUTH_VERSIONS)))" in defaults
    assert "MCP_AUTH_VERSIONS" in context_contract
    assert '"-f", str(MCP_AUTH_VERSIONS)' in context_contract

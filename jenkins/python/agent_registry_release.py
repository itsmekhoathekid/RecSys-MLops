"""Pure contracts for registry-gated Agent/MCP releases.

The functions in this module never call Git, Helm, Kubernetes, or ``arctl``.
Shell adapters own transport; this module owns deterministic manifests and the
immutable hand-off consumed by production Helm deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from jenkins.python.configuration import CONFIG_DIR, ROOT, read_json
from jenkins.python.release_plan import load_release_plan, release_version

CATALOG_PATH = CONFIG_DIR / "agent-registry-artifacts.json"
DEPLOY_CONFIG_PATH = CONFIG_DIR / "deploy-units.json"
DIGEST_REFERENCE = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{label} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} contains duplicates")
    return value


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, dict[str, Any]]:
    payload = read_json(path)
    if payload.get("version") != 1 or set(payload) != {"version", "artifacts"}:
        raise ValueError("agent-registry-artifacts.json must use version 1")
    artifacts = payload["artifacts"]
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("Agent Registry artifact catalog must not be empty")
    required = {
        "kind",
        "registryName",
        "component",
        "workloadUnit",
        "chartArtifact",
        "title",
        "description",
        "mcpDependencies",
        "agentDependencies",
    }
    optional = {"image", "imageValue", "remoteUrlKey", "sourcePath"}
    registry_names: set[str] = set()
    workload_units: set[str] = set()
    for artifact_id, spec in artifacts.items():
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or not isinstance(spec, dict)
        ):
            raise ValueError("Agent Registry artifacts must be named objects")
        missing = required - spec.keys()
        unknown = set(spec) - required - optional
        if missing or unknown:
            raise ValueError(
                f"Agent Registry artifact {artifact_id} fields are invalid; "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        if spec["kind"] not in {"mcp", "agent"}:
            raise ValueError(f"Agent Registry artifact {artifact_id} has invalid kind")
        if not re.fullmatch(r"[a-z0-9-]+/[a-z0-9-]+", spec["registryName"]):
            raise ValueError(
                f"Agent Registry artifact {artifact_id} has invalid registryName"
            )
        if spec["registryName"] in registry_names:
            raise ValueError(f"duplicate Agent Registry name: {spec['registryName']}")
        if spec["workloadUnit"] in workload_units:
            raise ValueError(
                f"duplicate Agent Registry workload: {spec['workloadUnit']}"
            )
        registry_names.add(spec["registryName"])
        workload_units.add(spec["workloadUnit"])
        for field in ("mcpDependencies", "agentDependencies"):
            _string_list(spec[field], f"Agent Registry artifact {artifact_id} {field}")
        if spec["kind"] == "mcp":
            for field in ("image", "imageValue", "remoteUrlKey"):
                if not isinstance(spec.get(field), str) or not spec[field]:
                    raise ValueError(
                        f"MCP artifact {artifact_id} requires non-empty {field}"
                    )
        elif any(field in spec for field in ("image", "imageValue", "remoteUrlKey")):
            raise ValueError(f"Agent artifact {artifact_id} cannot declare MCP fields")
        source_path = spec.get("sourcePath")
        if source_path is not None and not (ROOT / source_path).exists():
            raise ValueError(
                f"Agent Registry artifact {artifact_id} sourcePath does not exist"
            )
    for artifact_id, spec in artifacts.items():
        for dependency in [*spec["mcpDependencies"], *spec["agentDependencies"]]:
            if dependency not in artifacts:
                raise ValueError(
                    f"Agent Registry artifact {artifact_id} has unknown dependency {dependency}"
                )
        if any(artifacts[name]["kind"] != "mcp" for name in spec["mcpDependencies"]):
            raise ValueError(
                f"Agent Registry artifact {artifact_id} has non-MCP dependency"
            )
        if any(
            artifacts[name]["kind"] != "agent" for name in spec["agentDependencies"]
        ):
            raise ValueError(
                f"Agent Registry artifact {artifact_id} has non-Agent dependency"
            )
    return artifacts


def registry_tag(commit: str) -> str:
    return release_version(commit)


def _digest_reference(reference: str, label: str) -> str:
    if not DIGEST_REFERENCE.fullmatch(reference):
        raise ValueError(f"{label} must be an immutable @sha256 reference")
    return reference


def _chart_digest_reference(reference: str, label: str) -> str:
    reference = _digest_reference(reference, label)
    if not reference.startswith("oci://"):
        raise ValueError(f"{label} must use an oci:// reference")
    return reference


def _assert_artifact_ownership(
    artifact_id: str,
    spec: dict[str, Any],
    chart_reference: str,
    image_reference: str = "",
) -> None:
    release_artifacts = read_json(DEPLOY_CONFIG_PATH)["artifacts"]
    chart_spec = release_artifacts.get(spec["chartArtifact"], {})
    expected_chart = chart_spec.get("repository")
    if not expected_chart or f"/{expected_chart}@sha256:" not in chart_reference:
        raise ValueError(
            f"chart {artifact_id} does not match configured OCI repository"
        )
    if spec.get("image") and f"/{spec['image']}@sha256:" not in image_reference:
        raise ValueError(f"image {artifact_id} does not match configured repository")


def image_manifest_reference(directory: Path, image_name: str) -> str:
    key = image_name.upper().replace("-", "_") + "_DIGEST"
    for path in sorted(directory.glob("*.env")):
        for line in path.read_text(encoding="utf-8").splitlines():
            candidate_key, separator, value = line.partition("=")
            if separator and candidate_key == key:
                return _digest_reference(value, f"image {image_name}")
    raise ValueError(f"immutable image manifest entry is missing for {image_name}")


def chart_manifest_reference(directory: Path, artifact_id: str, commit: str) -> str:
    path = directory / f"{artifact_id}.json"
    payload = read_json(path)
    expected = {
        "version": 1,
        "artifact": artifact_id,
        "commit": commit,
        "releaseVersion": release_version(commit),
        "kind": "helm-oci",
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"chart manifest {path} has invalid {key}")
    return _chart_digest_reference(payload.get("reference", ""), f"chart {artifact_id}")


def dependency_refs(
    spec: dict[str, Any], catalog: dict[str, dict[str, Any]], tag: str
) -> list[str]:
    return [
        f"{catalog[dependency]['registryName']}@{tag}"
        for dependency in [*spec["mcpDependencies"], *spec["agentDependencies"]]
    ]


def build_resource(
    artifact_id: str,
    *,
    commit: str,
    git_url: str,
    chart_reference: str,
    image_reference: str = "",
    remote_url: str = "",
    catalog: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    catalog = catalog or load_catalog()
    if artifact_id not in catalog:
        raise ValueError(f"unknown Agent Registry artifact: {artifact_id}")
    spec = catalog[artifact_id]
    tag = registry_tag(commit)
    namespace, name = spec["registryName"].split("/", 1)
    chart_reference = _chart_digest_reference(chart_reference, f"chart {artifact_id}")
    if spec["kind"] == "mcp":
        image_reference = _digest_reference(image_reference, f"image {artifact_id}")
        if not remote_url.startswith(("http://", "https://")):
            raise ValueError(f"MCP artifact {artifact_id} requires an HTTP remote URL")
        resource_spec: dict[str, Any] = {
            "title": spec["title"],
            "description": spec["description"],
            "remote": {"type": "streamable-http", "url": remote_url},
        }
    else:
        resource_spec = {
            "title": spec["title"],
            "description": spec["description"],
            "mcpServers": [
                {
                    "kind": "MCPServer",
                    "namespace": catalog[dependency]["registryName"].split("/", 1)[0],
                    "name": catalog[dependency]["registryName"].split("/", 1)[1],
                    "tag": tag,
                }
                for dependency in spec["mcpDependencies"]
            ],
        }
    _assert_artifact_ownership(
        artifact_id, spec, chart_reference, image_reference=image_reference
    )
    contract_checksum = _sha256(resource_spec)
    annotations = {
        "recsys.dev/version": tag,
        "recsys.dev/git-commit": commit,
        "recsys.dev/source": git_url,
        "recsys.dev/helm-chart": chart_reference,
        "recsys.dev/contract-sha256": contract_checksum,
        "recsys.dev/deployment-driver": "helm",
        "recsys.dev/dependencies": ",".join(dependency_refs(spec, catalog, tag)),
    }
    if spec.get("sourcePath"):
        annotations["recsys.dev/source-path"] = spec["sourcePath"]
    if image_reference:
        annotations["recsys.dev/runtime-image"] = image_reference
    labels = {
        "app.kubernetes.io/part-of": "recsys-agentic",
        "recsys.dev/git-sha": commit[:12],
    }
    if spec["kind"] == "agent":
        labels["recsys.dev/variant"] = "sandbox"
    return {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "MCPServer" if spec["kind"] == "mcp" else "Agent",
        "metadata": {
            "namespace": namespace,
            "name": name,
            "tag": tag,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": resource_spec,
    }


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _readback_resource(payload: Any, expected: dict[str, Any]) -> dict[str, Any]:
    expected_metadata = expected["metadata"]
    for candidate in _walk(payload):
        metadata = candidate.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if (
            metadata.get("name") == expected_metadata["name"]
            and metadata.get("namespace") == expected_metadata["namespace"]
            and metadata.get("tag") == expected_metadata["tag"]
        ):
            return candidate
    raise ValueError(
        "Agent Registry read-back does not contain the expected name/namespace/tag"
    )


def validate_readback(expected: dict[str, Any], payload: Any) -> dict[str, Any]:
    resource = _readback_resource(payload, expected)
    if resource.get("apiVersion") != expected["apiVersion"]:
        raise ValueError("Agent Registry read-back apiVersion does not match manifest")
    if resource.get("kind") != expected["kind"]:
        raise ValueError("Agent Registry read-back kind does not match manifest")
    metadata = resource["metadata"]
    annotations = metadata.get("annotations", {})
    for key, value in expected["metadata"]["annotations"].items():
        if annotations.get(key) != value:
            raise ValueError(f"Agent Registry read-back annotation mismatch: {key}")
    labels = metadata.get("labels", {})
    for key, value in expected["metadata"]["labels"].items():
        if labels.get(key) != value:
            raise ValueError(f"Agent Registry read-back label mismatch: {key}")
    if resource.get("spec") != expected["spec"]:
        raise ValueError("Agent Registry read-back spec does not match manifest")
    return resource


def validate_dependency_readbacks(
    artifact_id: str,
    commit: str,
    manifest_dir: Path,
    readback_dir: Path,
) -> None:
    catalog = load_catalog()
    spec = catalog[artifact_id]
    for dependency in [*spec["mcpDependencies"], *spec["agentDependencies"]]:
        expected = read_json(manifest_dir / f"{dependency}.json")
        if expected.get("metadata", {}).get("tag") != registry_tag(commit):
            raise ValueError(
                f"Agent Registry dependency {dependency} is from another release"
            )
        validate_readback(
            expected,
            read_json(readback_dir / f"{dependency}.json"),
        )


def write_chart_record(
    output: Path, artifact_id: str, commit: str, reference: str
) -> dict[str, Any]:
    payload = {
        "version": 1,
        "kind": "helm-oci",
        "artifact": artifact_id,
        "commit": commit,
        "releaseVersion": release_version(commit),
        "reference": _chart_digest_reference(reference, f"chart {artifact_id}"),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def _deploy_units() -> dict[str, dict[str, Any]]:
    payload = read_json(DEPLOY_CONFIG_PATH)
    if payload.get("version") != 3:
        raise ValueError("deploy-units.json must use version 3")
    return {unit["name"]: unit for unit in payload["units"]}


def selected_registry_artifacts(plan: dict[str, Any]) -> list[str]:
    units = _deploy_units()
    artifacts: list[str] = []
    for unit_name in plan["publishUnits"]:
        artifact_id = units[unit_name].get("registryArtifact")
        if not artifact_id:
            raise ValueError(f"publish unit {unit_name} has no registryArtifact")
        artifacts.append(artifact_id)
    return artifacts


def _lock_entry_from_evidence(
    plan: dict[str, Any],
    artifact_id: str,
    manifest_dir: Path,
    readback_dir: Path,
    catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    expected = read_json(manifest_dir / f"{artifact_id}.json")
    raw = read_json(readback_dir / f"{artifact_id}.json")
    resource = validate_readback(expected, raw)
    annotations = expected["metadata"]["annotations"]
    if annotations["recsys.dev/contract-sha256"] != _sha256(expected["spec"]):
        raise ValueError(
            f"Agent Registry manifest {artifact_id} contract checksum is invalid"
        )
    metadata = resource["metadata"]
    readback_checksum = _sha256(resource)
    identity = metadata.get("uid") or resource.get("id") or readback_checksum
    spec = catalog[artifact_id]
    entry = {
        "kind": spec["kind"],
        "workloadUnit": spec["workloadUnit"],
        "registryRef": f"{spec['registryName']}@{plan['releaseVersion']}",
        "readbackIdentity": identity,
        "readbackSha256": readback_checksum,
        "gitCommit": plan["commit"],
        "chart": annotations["recsys.dev/helm-chart"],
        "contractSha256": annotations["recsys.dev/contract-sha256"],
        "dependencies": dependency_refs(spec, catalog, plan["releaseVersion"]),
    }
    if "recsys.dev/runtime-image" in annotations:
        entry["image"] = annotations["recsys.dev/runtime-image"]
    return entry


def seal_lock(
    plan: dict[str, Any], manifest_dir: Path, readback_dir: Path
) -> dict[str, Any]:
    catalog = load_catalog()
    entries: dict[str, Any] = {}
    selected = selected_registry_artifacts(plan)
    for artifact_id in selected:
        entries[artifact_id] = _lock_entry_from_evidence(
            plan, artifact_id, manifest_dir, readback_dir, catalog
        )
    lock = {
        "version": 1,
        "releaseVersion": plan["releaseVersion"],
        "gitCommit": plan["commit"],
        "artifacts": entries,
    }
    validate_lock(plan, lock)
    return lock


def validate_lock(
    plan: dict[str, Any],
    lock: dict[str, Any],
    unit_name: str = "",
    *,
    manifest_dir: Path | None = None,
    readback_dir: Path | None = None,
) -> dict[str, Any]:
    if (manifest_dir is None) != (readback_dir is None):
        raise ValueError(
            "lock validation requires both manifest and read-back evidence"
        )
    if lock.get("version") != 1:
        raise ValueError("Agent Registry lock must use version 1")
    if lock.get("releaseVersion") != plan["releaseVersion"]:
        raise ValueError("Agent Registry lock releaseVersion is stale")
    if lock.get("gitCommit") != plan["commit"]:
        raise ValueError("Agent Registry lock Git commit is stale")
    entries = lock.get("artifacts")
    if not isinstance(entries, dict):
        raise ValueError("Agent Registry lock artifacts must be an object")
    selected = selected_registry_artifacts(plan)
    if set(entries) != set(selected):
        raise ValueError("Agent Registry lock artifact set does not match release plan")
    catalog = load_catalog()
    for artifact_id, entry in entries.items():
        spec = catalog[artifact_id]
        required = {
            "kind",
            "workloadUnit",
            "registryRef",
            "readbackIdentity",
            "readbackSha256",
            "gitCommit",
            "chart",
            "contractSha256",
            "dependencies",
        }
        if spec["kind"] == "mcp":
            required.add("image")
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValueError(
                f"Agent Registry lock entry {artifact_id} fields are invalid"
            )
        if (
            entry["kind"] != spec["kind"]
            or entry["workloadUnit"] != spec["workloadUnit"]
        ):
            raise ValueError(
                f"Agent Registry lock entry {artifact_id} identity is invalid"
            )
        if (
            not isinstance(entry["readbackIdentity"], str)
            or not entry["readbackIdentity"]
        ):
            raise ValueError(
                f"Agent Registry lock entry {artifact_id} read-back identity is invalid"
            )
        if entry["gitCommit"] != plan["commit"]:
            raise ValueError(f"Agent Registry lock entry {artifact_id} commit is stale")
        expected_ref = f"{spec['registryName']}@{plan['releaseVersion']}"
        if entry["registryRef"] != expected_ref:
            raise ValueError(f"Agent Registry lock entry {artifact_id} ref is invalid")
        _chart_digest_reference(entry["chart"], f"lock chart {artifact_id}")
        if spec["kind"] == "mcp":
            _digest_reference(entry["image"], f"lock image {artifact_id}")
        _assert_artifact_ownership(
            artifact_id,
            spec,
            entry["chart"],
            image_reference=entry.get("image", ""),
        )
        for key in ("readbackSha256", "contractSha256"):
            if not SHA256.fullmatch(entry[key]):
                raise ValueError(
                    f"Agent Registry lock entry {artifact_id} has invalid {key}"
                )
        expected_dependencies = dependency_refs(spec, catalog, plan["releaseVersion"])
        if entry["dependencies"] != expected_dependencies:
            raise ValueError(
                f"Agent Registry lock entry {artifact_id} dependencies are invalid"
            )
        missing_dependencies = [
            dependency
            for dependency in [*spec["mcpDependencies"], *spec["agentDependencies"]]
            if dependency not in entries
        ]
        if missing_dependencies:
            raise ValueError(
                f"Agent Registry lock entry {artifact_id} omits dependencies "
                f"{missing_dependencies}"
            )
        if manifest_dir is not None and readback_dir is not None:
            expected_entry = _lock_entry_from_evidence(
                plan, artifact_id, manifest_dir, readback_dir, catalog
            )
            if entry != expected_entry:
                raise ValueError(
                    f"Agent Registry lock entry {artifact_id} does not match evidence"
                )
    if unit_name:
        units = _deploy_units()
        if unit_name not in units:
            raise ValueError(f"unknown deploy unit: {unit_name}")
        artifact_id = units[unit_name].get("registryArtifact")
        if not artifact_id or artifact_id not in entries:
            raise ValueError(
                f"deploy unit {unit_name} is not backed by this Agent Registry lock"
            )
        return entries[artifact_id]
    return lock


def runtime_records(
    plan: dict[str, Any],
    lock: dict[str, Any],
    manifest_dir: Path,
    readback_dir: Path,
) -> list[dict[str, str]]:
    """Return deterministic live-runtime expectations from a validated lock."""
    validate_lock(
        plan,
        lock,
        manifest_dir=manifest_dir,
        readback_dir=readback_dir,
    )
    catalog = load_catalog()
    units = _deploy_units()
    records: list[dict[str, str]] = []
    for artifact_id in selected_registry_artifacts(plan):
        spec = catalog[artifact_id]
        unit = units[spec["workloadUnit"]]
        entry = lock["artifacts"][artifact_id]
        records.append(
            {
                "artifact": artifact_id,
                "kind": spec["kind"],
                "workloadUnit": spec["workloadUnit"],
                "namespace": unit["namespace"],
                "resourceName": spec["registryName"].split("/", 1)[1],
                "remoteUrlKey": spec.get("remoteUrlKey", ""),
                "registryRef": entry["registryRef"],
                "releaseVersion": plan["releaseVersion"],
                "contractSha256": entry["contractSha256"],
                "image": entry.get("image", ""),
            }
        )
    return records


def _assignments(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, assigned = value.partition("=")
        if not separator or not key or not assigned:
            raise ValueError(f"invalid key=value assignment: {value}")
        result[key] = assigned
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-catalog")

    context = subparsers.add_parser("artifact-context")
    context.add_argument("artifact")
    context.add_argument("--commit", required=True)

    render = subparsers.add_parser("render")
    render.add_argument("artifact")
    render.add_argument("--commit", required=True)
    render.add_argument("--git-url", required=True)
    render.add_argument(
        "--image-manifest-dir", type=Path, default=Path(".ci-image-manifest")
    )
    render.add_argument(
        "--artifact-manifest-dir", type=Path, default=Path(".ci-artifact-manifest")
    )
    render.add_argument("--image", action="append", default=[])
    render.add_argument("--remote-url", action="append", default=[])
    render.add_argument("--output", required=True, type=Path)

    readback = subparsers.add_parser("validate-readback")
    readback.add_argument("--expected", required=True, type=Path)
    readback.add_argument("--readback", required=True, type=Path)

    dependencies = subparsers.add_parser("validate-dependencies")
    dependencies.add_argument("artifact")
    dependencies.add_argument("--commit", required=True)
    dependencies.add_argument("--manifest-dir", required=True, type=Path)
    dependencies.add_argument("--readback-dir", required=True, type=Path)

    chart = subparsers.add_parser("write-chart-record")
    chart.add_argument("--artifact", required=True)
    chart.add_argument("--commit", required=True)
    chart.add_argument("--reference", required=True)
    chart.add_argument("--output", required=True, type=Path)

    seal = subparsers.add_parser("seal-lock")
    seal.add_argument("--plan", required=True, type=Path)
    seal.add_argument("--manifest-dir", required=True, type=Path)
    seal.add_argument("--readback-dir", required=True, type=Path)
    seal.add_argument("--output", required=True, type=Path)

    lock_parser = subparsers.add_parser("validate-lock")
    lock_parser.add_argument("--plan", required=True, type=Path)
    lock_parser.add_argument("--lock", required=True, type=Path)
    lock_parser.add_argument("--unit", default="")
    lock_parser.add_argument("--manifest-dir", type=Path)
    lock_parser.add_argument("--readback-dir", type=Path)

    lock_context = subparsers.add_parser("lock-context")
    lock_context.add_argument("--plan", required=True, type=Path)
    lock_context.add_argument("--lock", required=True, type=Path)
    lock_context.add_argument("--unit", required=True)
    lock_context.add_argument(
        "--manifest-dir",
        type=Path,
        default=Path(".ci-deploy/agent-registry-manifests"),
    )
    lock_context.add_argument(
        "--readback-dir",
        type=Path,
        default=Path(".ci-deploy/agent-registry-readbacks"),
    )

    runtime_context = subparsers.add_parser("runtime-context")
    runtime_context.add_argument("--plan", required=True, type=Path)
    runtime_context.add_argument("--lock", required=True, type=Path)
    runtime_context.add_argument(
        "--manifest-dir",
        type=Path,
        default=Path(".ci-deploy/agent-registry-manifests"),
    )
    runtime_context.add_argument(
        "--readback-dir",
        type=Path,
        default=Path(".ci-deploy/agent-registry-readbacks"),
    )
    args = parser.parse_args()

    if args.command == "validate-catalog":
        load_catalog()
        return 0
    if args.command == "artifact-context":
        catalog = load_catalog()
        spec = catalog[args.artifact]
        print(
            f"RESOURCE\t{spec['kind']}\t{spec['registryName']}\t{registry_tag(args.commit)}"
        )
        print(f"WORKLOAD\t{spec['workloadUnit']}\t{spec['chartArtifact']}")
        if spec.get("image"):
            print(f"IMAGE\t{spec['image']}\t{spec['imageValue']}")
        if spec.get("remoteUrlKey"):
            print(f"REMOTE\t{spec['remoteUrlKey']}")
        return 0
    if args.command == "render":
        catalog = load_catalog()
        spec = catalog[args.artifact]
        image_overrides = _assignments(args.image)
        remote_urls = _assignments(args.remote_url)
        image_reference = ""
        if spec.get("image"):
            image_reference = image_overrides.get(
                spec["image"], ""
            ) or image_manifest_reference(args.image_manifest_dir, spec["image"])
        chart_reference = chart_manifest_reference(
            args.artifact_manifest_dir, spec["chartArtifact"], args.commit
        )
        remote_url = remote_urls.get(spec.get("remoteUrlKey", ""), "")
        resource = build_resource(
            args.artifact,
            commit=args.commit,
            git_url=args.git_url,
            chart_reference=chart_reference,
            image_reference=image_reference,
            remote_url=remote_url,
            catalog=catalog,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(resource, indent=2, sort_keys=True) + "\n")
        return 0
    if args.command == "validate-readback":
        validate_readback(read_json(args.expected), read_json(args.readback))
        return 0
    if args.command == "validate-dependencies":
        validate_dependency_readbacks(
            args.artifact,
            args.commit,
            args.manifest_dir,
            args.readback_dir,
        )
        return 0
    if args.command == "write-chart-record":
        write_chart_record(args.output, args.artifact, args.commit, args.reference)
        return 0
    if args.command == "seal-lock":
        plan = load_release_plan(args.plan)
        lock = seal_lock(plan, args.manifest_dir, args.readback_dir)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
        return 0
    if args.command in {"validate-lock", "lock-context"}:
        plan = load_release_plan(args.plan)
        lock = read_json(args.lock)
        entry = validate_lock(
            plan,
            lock,
            args.unit,
            manifest_dir=args.manifest_dir,
            readback_dir=args.readback_dir,
        )
        if args.command == "lock-context":
            print(f"CHART\t{entry['chart']}")
            if entry.get("image"):
                print(f"IMAGE\t{entry['image']}")
            print(f"ANNOTATION\trecsys.dev/agent-registry-ref\t{entry['registryRef']}")
            print(
                f"ANNOTATION\trecsys.dev/agent-release-version\t{plan['releaseVersion']}"
            )
            print(f"ANNOTATION\trecsys.dev/contract-sha256\t{entry['contractSha256']}")
        return 0
    if args.command == "runtime-context":
        plan = load_release_plan(args.plan)
        lock = read_json(args.lock)
        for record in runtime_records(
            plan,
            lock,
            args.manifest_dir,
            args.readback_dir,
        ):
            values = [
                record["artifact"],
                record["kind"],
                record["workloadUnit"],
                record["namespace"],
                record["resourceName"],
                record["remoteUrlKey"] or "-",
                record["registryRef"],
                record["releaseVersion"],
                record["contractSha256"],
                record["image"] or "-",
            ]
            print("RUNTIME\t" + "\t".join(values))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())

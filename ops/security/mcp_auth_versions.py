#!/usr/bin/env python3
"""Validate and safely resolve non-secret MCP authentication revision metadata."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "configs/agentic/mcp-auth-versions.yaml"
SERVICE_CONTRACTS = {
    "featureRag": {
        "secret": "recsys-feature-rag-mcp-auth",
        "workload": "recsys-feature-rag-mcp",
        "vault": "feature-rag-mcp",
    },
    "recommendation": {
        "secret": "recsys-recommendation-mcp-auth",
        "workload": "recsys-recommendation-mcp",
        "vault": "recommendation-mcp",
    },
}
ROOT_KEYS = {"version", "services"}
SERVICE_KEYS = {"namespace", "vaultPath", "activeRevision", "revisions"}
REVISION_KEYS = {
    "legacy",
    "vaultVersion",
    "secretName",
    "workloadName",
    "deploy",
}
DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
VERSIONED_REVISION = re.compile(r"v([1-9][0-9]*)")
DECIMAL_VERSION = re.compile(r"[1-9][0-9]*")


class ManifestError(ValueError):
    """Raised for a malformed or unsafe revision manifest."""


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{location} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ManifestError(f"{location} keys must be strings")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], location: str) -> None:
    unknown = set(value) - expected
    if unknown:
        raise ManifestError(
            f"{location} contains unsupported field(s): {', '.join(sorted(unknown))}"
        )


def _required_string(value: dict[str, Any], key: str, location: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ManifestError(f"{location}.{key} must be a non-empty string")
    return item


def _dns_label(value: str, location: str) -> None:
    if len(value) > 253 or DNS_LABEL.fullmatch(value) is None:
        raise ManifestError(f"{location} must be a lowercase DNS-compatible name")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        # JSON is a strict YAML subset, so this dependency-free parser remains
        # compatible with Helm -f and Terraform yamldecode while rejecting
        # aliases, custom tags, and other unsafe YAML features.
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        # Do not echo parser context: a malformed file must not leak its contents.
        raise ManifestError(f"invalid JSON-compatible YAML syntax: {path}") from exc
    return validate_manifest(payload)


def validate_manifest(payload: Any) -> dict[str, Any]:
    manifest = _mapping(payload, "manifest")
    _exact_keys(manifest, ROOT_KEYS, "manifest")
    if manifest.get("version") != 1 or isinstance(manifest.get("version"), bool):
        raise ManifestError("manifest.version must be integer 1")

    services = _mapping(manifest.get("services"), "manifest.services")
    if set(services) != set(SERVICE_CONTRACTS):
        raise ManifestError(
            "manifest.services must contain exactly featureRag and recommendation"
        )

    all_secret_names: set[str] = set()
    all_workload_names: set[str] = set()
    for service_name, contract in SERVICE_CONTRACTS.items():
        location = f"manifest.services.{service_name}"
        service = _mapping(services[service_name], location)
        _exact_keys(service, SERVICE_KEYS, location)

        namespace = _required_string(service, "namespace", location)
        _dns_label(namespace, f"{location}.namespace")
        if namespace != "kagent":
            raise ManifestError(f"{location}.namespace must be kagent")
        vault_path = _required_string(service, "vaultPath", location)
        if vault_path != contract["vault"]:
            raise ManifestError(f"{location}.vaultPath does not match its service")

        revisions = _mapping(service.get("revisions"), f"{location}.revisions")
        if not revisions:
            raise ManifestError(f"{location}.revisions must not be empty")
        if not any(
            VERSIONED_REVISION.fullmatch(revision_name) for revision_name in revisions
        ):
            raise ManifestError(f"{location}.revisions needs a versioned entry")

        active_revision = _required_string(service, "activeRevision", location)
        if active_revision not in revisions:
            raise ManifestError(f"{location}.activeRevision does not exist")

        service_vault_versions: set[str] = set()
        for revision_name, raw_revision in revisions.items():
            revision_location = f"{location}.revisions.{revision_name}"
            revision = _mapping(raw_revision, revision_location)
            _exact_keys(revision, REVISION_KEYS, revision_location)

            deploy = revision.get("deploy")
            if not isinstance(deploy, bool):
                raise ManifestError(f"{revision_location}.deploy must be boolean")

            secret_name = _required_string(revision, "secretName", revision_location)
            workload_name = _required_string(
                revision, "workloadName", revision_location
            )
            _dns_label(secret_name, f"{revision_location}.secretName")
            _dns_label(workload_name, f"{revision_location}.workloadName")
            if secret_name in all_secret_names:
                raise ManifestError("secretName values must be globally unique")
            if workload_name in all_workload_names:
                raise ManifestError("workloadName values must be globally unique")
            all_secret_names.add(secret_name)
            all_workload_names.add(workload_name)

            if revision_name == "legacy":
                if revision.get("legacy") is not True:
                    raise ManifestError(f"{revision_location}.legacy must be true")
                if "vaultVersion" in revision:
                    raise ManifestError(
                        f"{revision_location} must stay unpinned during migration"
                    )
                expected_secret = contract["secret"]
                expected_workload = contract["workload"]
            else:
                match = VERSIONED_REVISION.fullmatch(revision_name)
                if match is None:
                    raise ManifestError(
                        f"{revision_location} must use a vN revision name"
                    )
                if "legacy" in revision:
                    raise ManifestError(f"{revision_location}.legacy must be omitted")
                vault_version = revision.get("vaultVersion")
                if (
                    not isinstance(vault_version, str)
                    or DECIMAL_VERSION.fullmatch(vault_version) is None
                ):
                    raise ManifestError(
                        f"{revision_location}.vaultVersion must be a canonical decimal string"
                    )
                if vault_version != match.group(1):
                    raise ManifestError(
                        f"{revision_location}.vaultVersion must match {revision_name}"
                    )
                if vault_version in service_vault_versions:
                    raise ManifestError(
                        f"{location} vaultVersion values must be unique"
                    )
                service_vault_versions.add(vault_version)
                expected_secret = f"{contract['secret']}-{revision_name}"
                expected_workload = f"{contract['workload']}-{revision_name}"

            if secret_name != expected_secret:
                raise ManifestError(
                    f"{revision_location}.secretName does not match its revision"
                )
            if workload_name != expected_workload:
                raise ManifestError(
                    f"{revision_location}.workloadName does not match its revision"
                )

        active = _mapping(revisions[active_revision], f"{location}.activeRevision")
        if active.get("deploy") is not True:
            raise ManifestError(f"{location}.activeRevision must be deployed")

    return manifest


def _manifest_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _service(manifest: dict[str, Any], service_name: str) -> dict[str, Any]:
    return manifest["services"][service_name]


def validate_transition(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Validate that one commit represents exactly one safe lifecycle phase."""

    validate_manifest(before)
    validate_manifest(after)
    changed_services: list[tuple[str, str]] = []

    for service_name in SERVICE_CONTRACTS:
        old_service = before["services"][service_name]
        new_service = after["services"][service_name]
        if old_service == new_service:
            continue

        for field in ("namespace", "vaultPath"):
            if old_service[field] != new_service[field]:
                raise ManifestError(
                    f"{service_name}.{field} cannot change during rotation"
                )

        old_active = old_service["activeRevision"]
        new_active = new_service["activeRevision"]
        old_revisions = old_service["revisions"]
        new_revisions = new_service["revisions"]
        old_names = set(old_revisions)
        new_names = set(new_revisions)
        added = new_names - old_names
        removed = old_names - new_names
        shared = old_names & new_names
        modified = {
            name
            for name in shared
            if old_revisions[name] != new_revisions[name]
        }

        phase = ""
        if old_active != new_active:
            if added or removed or modified:
                raise ManifestError(
                    f"{service_name} cutover/rollback commit may only change activeRevision"
                )
            phase = "cutover-or-rollback"
        elif added:
            if removed or modified:
                raise ManifestError(
                    f"{service_name} prepare commit may only add revisions"
                )
            if len(added) != 1:
                raise ManifestError(
                    f"{service_name} prepare commit must add exactly one revision"
                )
            if any(new_revisions[name]["deploy"] is not True for name in added):
                raise ManifestError(
                    f"{service_name} prepared revisions must start deploy=true"
                )
            phase = "prepare"
        elif removed:
            if modified:
                raise ManifestError(
                    f"{service_name} purge commit may only remove revisions"
                )
            if len(removed) != 1:
                raise ManifestError(
                    f"{service_name} purge commit must remove exactly one revision"
                )
            unsafe = [
                name for name in removed if old_revisions[name]["deploy"] is not False
            ]
            if unsafe:
                raise ManifestError(
                    f"{service_name} revisions must be deploy=false before purge: "
                    + ", ".join(sorted(unsafe))
                )
            phase = "purge"
        elif modified:
            if len(modified) != 1:
                raise ManifestError(
                    f"{service_name} retirement commit must change one revision"
                )
            name = next(iter(modified))
            old_revision = old_revisions[name]
            new_revision = new_revisions[name]
            old_without_deploy = {k: v for k, v in old_revision.items() if k != "deploy"}
            new_without_deploy = {k: v for k, v in new_revision.items() if k != "deploy"}
            if (
                name == old_active
                or old_without_deploy != new_without_deploy
                or old_revision["deploy"] is not True
                or new_revision["deploy"] is not False
            ):
                raise ManifestError(
                    f"{service_name} retirement may only set one inactive revision deploy=false"
                )
            phase = "retire"
        else:
            raise ManifestError(f"{service_name} has an unsupported manifest change")

        changed_services.append((service_name, phase))

    if len(changed_services) > 1:
        raise ManifestError(
            "one commit may change the rotation lifecycle of only one MCP service"
        )
    if not changed_services:
        return "unchanged"
    service_name, phase = changed_services[0]
    return f"{service_name}:{phase}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate revision metadata")
    validate.add_argument(
        "path", nargs="?", default=DEFAULT_MANIFEST, type=_manifest_path
    )

    get = subparsers.add_parser("get", help="resolve one active non-secret value")
    get.add_argument("service", choices=tuple(SERVICE_CONTRACTS))
    get.add_argument(
        "field", choices=("activeRevision", "activeSecret", "activeWorkload")
    )
    get.add_argument("--file", default=DEFAULT_MANIFEST, type=_manifest_path)

    deployed = subparsers.add_parser(
        "list-deployed", help="list deployed revisions as tab-separated metadata"
    )
    deployed.add_argument("service", choices=tuple(SERVICE_CONTRACTS))
    deployed.add_argument("--file", default=DEFAULT_MANIFEST, type=_manifest_path)

    transition = subparsers.add_parser(
        "validate-transition",
        help="validate prepare/cutover/rollback/retire/purge commit ordering",
    )
    transition.add_argument("before", type=_manifest_path)
    transition.add_argument("after", type=_manifest_path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "validate-transition":
        try:
            before = load_manifest(args.before)
            after = load_manifest(args.after)
            phase = validate_transition(before, after)
        except ManifestError as exc:
            print(f"mcp-auth manifest error: {exc}", file=sys.stderr)
            return 2
        print(phase)
        return 0

    path = args.path if args.command == "validate" else args.file
    try:
        manifest = load_manifest(path)
    except ManifestError as exc:
        print(f"mcp-auth manifest error: {exc}", file=sys.stderr)
        return 2

    if args.command == "validate":
        print(f"validated {path}")
        return 0

    service = _service(manifest, args.service)
    active_revision = service["activeRevision"]
    active = service["revisions"][active_revision]
    if args.command == "get":
        resolved = {
            "activeRevision": active_revision,
            "activeSecret": active["secretName"],
            "activeWorkload": active["workloadName"],
        }
        print(resolved[args.field])
        return 0

    for revision_name, revision in service["revisions"].items():
        if revision["deploy"]:
            vault_version = revision.get("vaultVersion", "-")
            print(
                revision_name,
                revision["secretName"],
                revision["workloadName"],
                vault_version,
                sep="\t",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = ROOT / "jenkins" / "config" / "container-scan-policy.json"


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def evaluate(
    image_name: str,
    report: dict[str, Any],
    policy: dict[str, Any],
    *,
    today: dt.date | None = None,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    if policy.get("version") != 1:
        raise ValueError("container scan policy version must be 1")
    blocking = {str(value).upper() for value in policy["blockingSeverities"]}
    exception = policy.get("exceptions", {}).get(image_name)
    accepted_types: set[str] = set()
    accepted_vulnerabilities: set[tuple[str, str, str]] = set()
    if exception:
        expires_on = dt.date.fromisoformat(exception["expiresOn"])
        if (today or dt.date.today()) > expires_on:
            raise ValueError(
                f"{image_name} vulnerability exception expired on {expires_on.isoformat()}"
            )
        if not str(exception.get("owner", "")).strip() or not str(
            exception.get("reason", "")
        ).strip():
            raise ValueError(f"{image_name} exception requires owner and reason")
        accepted_types = {str(value) for value in exception.get("types", [])}
        accepted_vulnerabilities = {
            (
                str(value["id"]),
                str(value["package"]),
                str(value["type"]),
            )
            for value in exception.get("acceptedVulnerabilities", [])
        }

    rejected: list[dict[str, str]] = []
    accepted_counts = {"HIGH": 0, "CRITICAL": 0}
    for result in report.get("Results") or []:
        result_type = str(result.get("Type", ""))
        target = str(result.get("Target", ""))
        for vulnerability in result.get("Vulnerabilities") or []:
            severity = str(vulnerability.get("Severity", "")).upper()
            if severity not in blocking:
                continue
            record = {
                "id": str(vulnerability.get("VulnerabilityID", "")),
                "package": str(vulnerability.get("PkgName", "")),
                "severity": severity,
                "target": target,
                "type": result_type,
            }
            exact_key = (record["id"], record["package"], result_type)
            if exception and (
                result_type in accepted_types
                or exact_key in accepted_vulnerabilities
            ):
                accepted_counts[severity] += 1
            else:
                rejected.append(record)

    if exception:
        limits = {
            "HIGH": int(exception["maxHigh"]),
            "CRITICAL": int(exception["maxCritical"]),
        }
        exceeded = {
            severity: count
            for severity, count in accepted_counts.items()
            if count > limits[severity]
        }
        if exceeded:
            raise ValueError(
                f"{image_name} vendor baseline exceeded: {exceeded}; limits={limits}"
            )
    return rejected, accepted_counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Enforce the RecSys container scan policy.")
    parser.add_argument("--image-name", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args()

    rejected, accepted = evaluate(
        args.image_name,
        load_json(args.report),
        load_json(args.policy),
    )
    summary = {
        "image": args.image_name,
        "acceptedVendorBaseline": accepted,
        "rejectedCount": len(rejected),
    }
    print(json.dumps(summary, sort_keys=True))
    if rejected:
        for item in rejected[:25]:
            print(
                "{severity} {id} {package} type={type} target={target}".format(**item)
            )
        if len(rejected) > 25:
            print(f"... {len(rejected) - 25} additional blocking vulnerabilities")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

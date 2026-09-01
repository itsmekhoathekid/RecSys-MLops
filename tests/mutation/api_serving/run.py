#!/usr/bin/env python3
"""Run target-scoped Mutmut gates for the three serving APIs.

Mutmut 3 resolves trampolines only for conventional ``src/<package>`` layouts.
The repository is a monorepo, so this runner copies the authoritative package
sources and mutation oracles into an isolated conventional workspace. Nothing
under ``apps/`` is rewritten and only JSON/text reports are retained.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[3]
REPORT_ROOT = ROOT / "tests" / "mutation" / "api_serving" / "reports"
BAD_STATUSES = {
    "not checked",
    "no tests",
    "suspicious",
    "timeout",
    "check was interrupted by user",
    "interrupted",
    "segfault",
    "skipped",
}
SERVICE_CONFIG = {
    "inference": {
        "patterns": ["recsys_inference_api*"],
        "test_dir": "inference_api",
        "prefixes": ["recsys_inference_api."],
    },
    "online-feature": {
        "patterns": [
            "recsys_online_feature_api*",
            "recsys_serving_common.contracts*",
        ],
        "test_dir": "online_feature_api",
        "prefixes": [
            "recsys_online_feature_api.",
            "recsys_serving_common.contracts.",
        ],
    },
    "rag": {
        "patterns": ["recsys_rag_api*"],
        "test_dir": "rag_api",
        "prefixes": ["recsys_rag_api."],
    },
}
SOURCE_PACKAGES = {
    "recsys_inference_api": ROOT
    / "apps/api-serving/inference-api/src/recsys_inference_api",
    "recsys_online_feature_api": ROOT
    / "apps/api-serving/online-feature-api/src/recsys_online_feature_api",
    "recsys_serving_common": ROOT / "apps/api-serving/shared/src/recsys_serving_common",
    "recsys_rag_api": ROOT / "apps/api-serving/rag-api/src/recsys_rag_api",
    "recsys_feature_store_runtime": ROOT
    / "apps/data-platform/feature-store/runtime/src/recsys_feature_store_runtime",
    "recsys_rag_runtime": ROOT
    / "apps/data-platform/rag-runtime/src/recsys_rag_runtime",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "service",
        choices=["inference", "online-feature", "rag", "all"],
    )
    parser.add_argument("--max-children", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--output-dir", type=Path, default=REPORT_ROOT)
    return parser.parse_args()


def copy_workspace(workspace: Path, service: str) -> None:
    src = workspace / "src"
    src.mkdir()
    for package, source in SOURCE_PACKAGES.items():
        shutil.copytree(source, src / package)
    tests = workspace / "tests"
    shutil.copytree(
        ROOT
        / "tests"
        / "mutation"
        / "api_serving"
        / SERVICE_CONFIG[service]["test_dir"],
        tests,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(ROOT / "pyproject.toml", workspace / "pyproject.toml")


def command(
    workspace: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("RECSYS_OTEL_ENABLED", "0")
    env["PYTHONPATH"] = str(workspace / "src")
    return subprocess.run(
        [sys.executable, "-m", "mutmut", *args],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def parse_results(output: str, prefixes: list[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if ": " not in line:
            continue
        name, status = line.rsplit(": ", 1)
        if any(name.startswith(prefix) for prefix in prefixes):
            results[name] = status.strip()
    return results


def write_report(
    output_dir: Path,
    service: str,
    results: dict[str, str],
    run_output: str,
) -> bool:
    counts: dict[str, int] = {}
    for status in results.values():
        counts[status] = counts.get(status, 0) + 1
    killed = counts.get("killed", 0)
    survived = counts.get("survived", 0)
    denominator = killed + survived
    score = killed / denominator if denominator else 0.0
    bad = {status: count for status, count in counts.items() if status in BAD_STATUSES}
    unknown = {
        status: count
        for status, count in counts.items()
        if status not in {"killed", "survived"} | BAD_STATUSES
    }
    passed = bool(results) and score > 0.80 and not bad and not unknown
    report = {
        "service": service,
        "killed": killed,
        "survived": survived,
        "score": round(score, 6),
        "score_percent": round(score * 100, 2),
        "status_counts": counts,
        "bad_statuses": bad,
        "unknown_statuses": unknown,
        "selected_mutants": len(results),
        "mutants": dict(sorted(results.items())),
        "gate": "PASS" if passed else "FAIL",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{service}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    text = [
        f"service: {service}",
        f"gate: {report['gate']}",
        f"score: {killed}/{denominator} = {report['score_percent']:.2f}%",
        f"selected mutants: {len(results)}",
        f"statuses: {json.dumps(counts, sort_keys=True)}",
        "",
        "mutmut run output:",
        run_output.strip(),
        "",
    ]
    (output_dir / f"{service}.txt").write_text("\n".join(text), encoding="utf-8")
    print("\n".join(text[:5]))
    return passed


def run_service(service: str, output_dir: Path, max_children: int) -> bool:
    with tempfile.TemporaryDirectory(prefix=f"recsys-mutmut-{service}-") as directory:
        workspace = Path(directory)
        copy_workspace(workspace, service)
        config = SERVICE_CONFIG[service]
        result = command(
            workspace,
            "run",
            *config["patterns"],
            "--max-children",
            str(max_children),
            check=False,
        )
        combined = result.stdout + result.stderr
        if result.returncode not in {0, 1}:
            print(combined, file=sys.stderr)
            return False
        listed = command(workspace, "results", "--all", "true")
        results = parse_results(listed.stdout, config["prefixes"])
        suspicious = [
            name for name, status in results.items() if status == "suspicious"
        ]
        if suspicious:
            initial_results = results.copy()
            unresolved = suspicious
            for attempt in range(1, 6):
                retest = command(
                    workspace,
                    "run",
                    *unresolved,
                    "--max-children",
                    str(max_children),
                    check=False,
                )
                combined += (
                    f"\nSuspicious-mutant retest {attempt}:\n"
                    + retest.stdout
                    + retest.stderr
                )
                listed = command(workspace, "results", "--all", "true")
                retested_results = parse_results(listed.stdout, config["prefixes"])
                for name in unresolved:
                    initial_results[name] = retested_results.get(name, "not checked")
                unresolved = [
                    name for name in unresolved if initial_results[name] == "suspicious"
                ]
                if not unresolved:
                    break
            results = initial_results
        return write_report(output_dir, service, results, combined)


def main() -> int:
    args = parse_args()
    services = list(SERVICE_CONFIG) if args.service == "all" else [args.service]
    outcomes = [
        run_service(service, args.output_dir, args.max_children) for service in services
    ]
    return 0 if all(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())

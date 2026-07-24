from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path


COMPONENTS = (
    "MATERIALIZE",
    "TRAINING",
    "SPARK_BATCH",
    "DP1",
    "DP2",
    "DP3",
    "API",
    "KSERVE",
    "ROLLOUT",
    "DRIFT",
    "STREAM_OFFLINE",
    "STREAM_ONLINE",
    "ANALYTICS",
    "DEMO_WEB",
)

FLAGS = {f"RUN_{component}": False for component in COMPONENTS}
FLAGS.update(
    {
        "RUN_CI_CONFIG": False,
        "RUN_COMPONENT_CI": False,
        "RUN_COMPONENT_BUILD": False,
        "RUN_COMPONENT_DEPLOY": False,
        "RUN_PYTHON": False,
    }
)

DATA_PLATFORM_COMPONENTS = (
    "MATERIALIZE",
    "SPARK_BATCH",
    "DP1",
    "DP2",
    "DP3",
    "DRIFT",
    "STREAM_OFFLINE",
    "STREAM_ONLINE",
)

PYTHON_COMPONENTS = COMPONENTS
PRODUCT_FLAG_NAMES = tuple(f"RUN_{component}" for component in COMPONENTS)
ROUTING_FLAG_NAMES = PRODUCT_FLAG_NAMES + ("RUN_CI_CONFIG",)

IGNORED_PREFIXES = (
    "docs/",
    "graphify-out/",
    "ci-image-manifest/",
    ".ci-image-manifest/",
    "docker-metrics/",
    ".docker-metrics/",
    "tmp/",
    ".idea/",
    ".vscode/",
)
IGNORED_SUFFIXES = (
    ".md",
    ".rst",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".pyc",
)
IGNORED_NAMES = {
    ".DS_Store",
    ".gitignore",
    ".gitattributes",
    ".gitkeep",
    "LICENSE",
    "AGENTS.md",
    "coverage",
    ".coverage",
}


@dataclass(frozen=True)
class ClassificationResult:
    flags: dict[str, bool]
    changed_paths: tuple[str, ...]
    ignored_paths: tuple[str, ...]
    unmapped_paths: tuple[str, ...]

    @property
    def component_names(self) -> tuple[str, ...]:
        names = [component.lower() for component in COMPONENTS if self.flags[f"RUN_{component}"]]
        if self.flags["RUN_CI_CONFIG"]:
            names.append("ci_config")
        return tuple(names)


def git_lines(args: list[str]) -> list[str]:
    output = subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL)
    return [line.strip() for line in output.splitlines() if line.strip()]


def current_commit_paths() -> list[str]:
    fallback_commands = (
        ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-m", "HEAD"],
        ["show", "--pretty=format:", "--name-only", "HEAD"],
    )
    for args in fallback_commands:
        try:
            return list(dict.fromkeys(git_lines(args)))
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    return []


def changed_paths(base_ref: str | None) -> list[str]:
    """Return the exact changed range, preserving a valid empty diff.

    A successful empty diff must remain empty. The old implementation treated it
    as a failure and fell back to HEAD~1, which could re-run stale components.
    """
    if base_ref:
        try:
            return git_lines(["diff", "--name-only", f"{base_ref}...HEAD"])
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    try:
        return git_lines(["diff", "--name-only", "HEAD~1", "HEAD"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return current_commit_paths()


def mark(flags: dict[str, bool], *components: str) -> None:
    for component in components:
        flags[f"RUN_{component}"] = True


def mark_ci_config(flags: dict[str, bool]) -> None:
    flags["RUN_CI_CONFIG"] = True


def mark_data_platform(flags: dict[str, bool]) -> None:
    mark(flags, *DATA_PLATFORM_COMPONENTS)


def mark_all_python(flags: dict[str, bool]) -> None:
    mark(flags, *PYTHON_COMPONENTS)


def normalize_path(path: str) -> str:
    normalized = path.strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_ignored_path(path: str) -> bool:
    normalized = normalize_path(path)
    name = Path(normalized).name
    lower = normalized.lower()
    return (
        not normalized
        or normalized.startswith(IGNORED_PREFIXES)
        or name in IGNORED_NAMES
        or lower.endswith(IGNORED_SUFFIXES)
        or "/__pycache__/" in normalized
    )


def classify_config(flags: dict[str, bool], path: str) -> None:
    name = Path(path).name
    if path.startswith("configs/datahub/"):
        mark(flags, "MATERIALIZE", "DP1", "DP2", "DP3")
    elif name.startswith("bst") or name == "ray-cluster.yaml":
        mark(flags, "TRAINING")
    elif name.startswith("spark_batch"):
        mark(flags, "SPARK_BATCH", "DP2", "DP3")
    elif name == "data_generator_drift.yaml":
        mark(flags, "DP1", "DRIFT")
    elif name.startswith("data_generator") or name in {"postgres_source.yaml", "kafka_topics.yaml"}:
        mark(flags, "DP1")
    elif name in {"flink_streaming.yaml", "redis_online_store.yaml"}:
        mark(flags, "STREAM_OFFLINE", "STREAM_ONLINE")
    elif name in {"data_flow.yaml", "airflow.yaml"}:
        mark_data_platform(flags)


def classify_spark_source(flags: dict[str, bool], path: str) -> None:
    name = Path(path).name
    silver_files = {"build_silver_tables.py", "dp2_silver_gold_entrypoint.py"}
    feature_files = {
        "build_bst_training_table.py",
        "build_item_features.py",
        "build_ranking_labels.py",
        "build_user_aggregate_features.py",
        "build_user_sequence_features.py",
        "spark_batch_entrypoint.py",
    }
    if name in silver_files:
        mark(flags, "SPARK_BATCH", "DP2", "DP3")
    elif name in feature_files:
        mark(flags, "SPARK_BATCH", "DP3")
    else:
        mark(flags, "SPARK_BATCH", "DP2", "DP3")


def classify_feature_store_source(flags: dict[str, bool], path: str) -> None:
    name = Path(path).name
    if name == "feast_registry.py":
        mark(flags, "MATERIALIZE")
    elif name == "online_writer.py":
        mark(flags, "STREAM_ONLINE")
    elif name == "postgres_offline_store.py":
        mark(flags, "DP3", "STREAM_OFFLINE")
    else:
        mark(flags, "MATERIALIZE", "DP3", "STREAM_OFFLINE", "STREAM_ONLINE")


def classify_data_platform_source(flags: dict[str, bool], path: str) -> None:
    if path.startswith("apps/data-platform/src/feature_store/"):
        classify_feature_store_source(flags, path)
    elif path.startswith("apps/data-platform/src/ingest/"):
        mark(flags, "DP1")
    elif path.startswith("apps/data-platform/src/features/spark/"):
        classify_spark_source(flags, path)
    elif path.startswith("apps/data-platform/src/features/flink/"):
        mark(flags, "STREAM_OFFLINE", "STREAM_ONLINE")
    elif path.endswith("validate/governance_contracts.py"):
        mark(flags, "DP1", "DP2", "DP3")
    elif path.startswith("apps/data-platform/src/validate/"):
        mark(flags, "DRIFT")
    elif path.startswith("apps/data-platform/src/mlops/"):
        mark(flags, "TRAINING", "DRIFT")
    elif path.startswith("apps/data-platform/src/lakehouse/"):
        mark(flags, "SPARK_BATCH", "DP1", "DP2", "DP3", "STREAM_OFFLINE")
    elif path.startswith("apps/data-platform/src/metadata/"):
        mark(flags, "MATERIALIZE", "DP1", "DP2", "DP3")
    elif path.startswith("apps/data-platform/src/orchestration/airflow/dags/"):
        classify_airflow_dag(flags, path)
    elif path.startswith("apps/data-platform/src/config/"):
        mark_data_platform(flags)
    elif path.startswith("apps/data-platform/src/monitoring/"):
        mark(flags, "DRIFT")
    elif path.startswith("apps/data-platform/src/"):
        mark_data_platform(flags)


def classify_airflow_dag(flags: dict[str, bool], path: str) -> None:
    name = Path(path).name
    if name == "rubric_data_pipeline_dags.py":
        mark(flags, "SPARK_BATCH", "DP1", "DP2", "DP3")
    else:
        mark_data_platform(flags)


def classify_infra(flags: dict[str, bool], path: str) -> None:
    if path.startswith("infra/helm/recsys-ci/"):
        mark_ci_config(flags)
        if path in {
            "infra/helm/recsys-ci/values.yaml",
            "infra/helm/recsys-ci/values-gke.yaml",
            "infra/helm/recsys-ci/templates/model-rollout-watcher.yaml",
        }:
            mark(flags, "ROLLOUT")
    elif path.startswith("infra/kubeflow/"):
        mark(flags, "TRAINING")
    elif path.startswith("infra/helm/recsys-serving/"):
        mark(flags, "API", "KSERVE", "ROLLOUT")
    elif path.startswith("infra/helm/recsys-demo-web/"):
        mark(flags, "DEMO_WEB")
    elif path == "infra/helm/recsys-security/templates/istio-authorization.yaml":
        mark(flags, "DEMO_WEB")
        mark_ci_config(flags)
    elif path.startswith("infra/helm/recsys-data-platform/"):
        mark_data_platform(flags)
    elif path.startswith("infra/helm/recsys-analytics/"):
        mark(flags, "ANALYTICS")
    elif path.startswith(("infra/helm/ray-cluster/", "infra/helm/recsys-runtime/", "infra/helm/mlflow-stack/")):
        mark(flags, "TRAINING")
    elif path.startswith("infra/helm/recsys-observability/"):
        mark(flags, "API", "KSERVE", "ROLLOUT", "DRIFT")
    elif path == "infra/docker/Dockerfile.base-python":
        mark(flags, "MATERIALIZE", "TRAINING", "DP1", "DP3", "DRIFT", "STREAM_ONLINE")
    elif path == "infra/docker/Dockerfile.airflow":
        mark_data_platform(flags)
        mark(flags, "ANALYTICS")
    elif path == "infra/docker/Dockerfile.kafka-connect":
        mark(flags, "DP1")
    elif path.startswith("infra/docker/"):
        mark_data_platform(flags)
    elif path.startswith(("infra/k8s/scripts/data_platform", "infra/k8s/scripts/cluster_data_setup")):
        mark_data_platform(flags)
    elif path.startswith("infra/k8s/scripts/cluster_mlops_serving"):
        mark(flags, "TRAINING", "API", "KSERVE")
    elif path.startswith("infra/k8s/processing-baseline/"):
        mark(flags, "SPARK_BATCH", "STREAM_OFFLINE")
    elif path.startswith("infra/k8s/datahub"):
        mark(flags, "MATERIALIZE", "DP1", "DP2", "DP3")
    else:
        # Cluster/IaC changes need lint/contract validation, not every product image.
        mark_ci_config(flags)


def classify_jenkins(flags: dict[str, bool], path: str) -> None:
    mark_ci_config(flags)
    if path.startswith("jenkins/demo-web-rollback/"):
        mark(flags, "DEMO_WEB")
    elif path == "jenkins/scripts/model_cd.py":
        mark(flags, "KSERVE", "ROLLOUT")
    elif path in {
        "jenkins/KServeModelCD.Jenkinsfile",
        "jenkins/scripts/autonomous_rollout_locust.sh",
        "jenkins/scripts/model_rollout_demo.sh",
        "jenkins/scripts/verify_champion_only.sh",
    }:
        mark(flags, "ROLLOUT")
    elif path == "jenkins/scripts/kubeflow_pipeline_cicd.sh":
        mark(flags, "TRAINING")
    elif Path(path).name in {"validation_evidence.sh", "validation_load_test.sh", "validation_mutation.sh"}:
        mark(flags, "API")
    elif Path(path).name in {"demo_web_smoke.sh", "demo_web_rollback.sh"}:
        mark(flags, "DEMO_WEB")


def classify_tests(flags: dict[str, bool], path: str) -> None:
    name = Path(path).name
    if path.startswith("tests/unit/jenkins/"):
        mark_ci_config(flags)
    elif path.startswith("tests/unit/demo_web/") or path == "tests/contract/test_demo_web_contracts.py":
        mark(flags, "DEMO_WEB")
    elif path.startswith("tests/unit/api_serving/"):
        mark(flags, "API")
    elif path.startswith("tests/unit/analytics/") or path == "tests/contract/test_analytics_contracts.py":
        mark(flags, "ANALYTICS")
    elif path.startswith("tests/unit/ml_system/"):
        mark(flags, "TRAINING")
        if name == "test_model_rollout_controller.py":
            mark(flags, "ROLLOUT")
        if name == "test_kserve_cd_trigger.py":
            mark(flags, "ROLLOUT")
        if name in {"test_model_promotion.py", "test_kserve_cd_trigger.py"}:
            mark(flags, "KSERVE")
    elif path.endswith("tests/unit/data_platform/test_lakehouse_optimization.py"):
        mark(flags, "SPARK_BATCH", "DP2", "DP3")
    elif path.endswith("tests/unit/data_platform/test_governance_lineage.py"):
        mark(flags, "MATERIALIZE", "DP1", "DP2", "DP3")
    elif path == "tests/unit/test_runtime_lineage.py":
        mark(flags, "MATERIALIZE", "DP1", "DP2", "DP3")
    elif path.startswith("tests/unit/data_platform/"):
        mark_data_platform(flags)
    elif path.startswith("tests/unit/data_generator/"):
        mark(flags, "DP1")
        if name.startswith("test_drift"):
            mark(flags, "DRIFT")
    elif path == "tests/contract/test_serving_contracts.py":
        mark(flags, "API", "KSERVE", "ROLLOUT")
    elif path == "tests/contract/test_gateway_contracts.py":
        mark(flags, "API", "DEMO_WEB")
    elif path == "tests/contract/test_observability_contracts.py":
        mark(flags, "API", "KSERVE", "ROLLOUT", "DRIFT")
    elif path == "tests/contract/test_docker_dataflow_contracts.py":
        mark_data_platform(flags)
    elif path.startswith("tests/e2e/"):
        mark(flags, "API", "KSERVE")
    elif path.startswith("tests/load/"):
        mark(flags, "API", "ROLLOUT")
    elif path.startswith("tests/unit/feature_store/"):
        mark(flags, "MATERIALIZE", "DP3")
    elif path.startswith("tests/integration/"):
        parts = Path(path).parts
        if len(parts) >= 3:
            component = parts[2].upper().replace("-", "_")
            if component in COMPONENTS:
                mark(flags, component)


def apply_path_rules(flags: dict[str, bool], normalized: str) -> None:
    parts = Path(normalized).parts
    if normalized in {"pyproject.toml", "uv.lock", "requirements.txt"}:
        mark_all_python(flags)
    elif normalized == ".dockerignore":
        mark_all_python(flags)
    elif normalized in {".gcloudignore", ".python-version"}:
        mark_ci_config(flags)
    elif normalized == "Jenkinsfile" or normalized.startswith(".github/"):
        mark_ci_config(flags)
    elif normalized.startswith("configs/"):
        classify_config(flags, normalized)
    elif normalized.startswith("notebooks/"):
        mark(flags, "TRAINING")
    elif normalized.startswith(("apps/api-serving/", "apps/api/")):
        mark(flags, "API")
    elif normalized.startswith("apps/demo-web/"):
        mark(flags, "DEMO_WEB")
    elif normalized == "apps/ml-system/src/cli/model_rollout_controller.py":
        mark(flags, "ROLLOUT")
    elif normalized.startswith("apps/ml-system/"):
        mark(flags, "TRAINING")
        if parts[-1] == "trigger_kserve_cd.py":
            mark(flags, "KSERVE", "ROLLOUT")
        elif parts[-1] == "model_promotion.py":
            mark(flags, "KSERVE")
    elif normalized.startswith("apps/analytics/"):
        mark(flags, "ANALYTICS")
    elif normalized.startswith("apps/data-platform/feature-store/"):
        mark(flags, "MATERIALIZE", "DP3", "STREAM_OFFLINE", "STREAM_ONLINE")
    elif normalized.startswith("apps/data-platform/data-generator/"):
        mark(flags, "DP1")
        if normalized.startswith("apps/data-platform/data-generator/src/drift/"):
            mark(flags, "DRIFT")
    elif normalized == "apps/data-platform/Dockerfile.spark":
        mark(flags, "SPARK_BATCH", "DP2", "DP3")
    elif normalized in {
        "apps/data-platform/Dockerfile.flink",
        "apps/data-platform/flink-runtime-pom.xml",
    }:
        mark(flags, "STREAM_OFFLINE", "STREAM_ONLINE")
    elif normalized == "apps/data-platform/Dockerfile.dataflow-cli":
        mark(flags, "MATERIALIZE", "DP1", "DP3", "DRIFT", "STREAM_ONLINE")
    elif normalized == "apps/data-platform/uv.lock":
        mark_data_platform(flags)
    elif normalized == "apps/data-platform/pyproject.toml":
        mark_data_platform(flags)
    elif normalized.startswith("apps/data-platform/src/"):
        classify_data_platform_source(flags, normalized)
    elif normalized.startswith("infra/"):
        classify_infra(flags, normalized)
    elif normalized.startswith("jenkins/"):
        classify_jenkins(flags, normalized)
    elif normalized.startswith("tests/"):
        classify_tests(flags, normalized)
    elif normalized.startswith("tools/proofs/compare_spark"):
        mark(flags, "SPARK_BATCH", "DP2", "DP3")
    elif normalized.startswith("tools/proofs/detect_duplicate_events"):
        mark(flags, "DP1", "DP2")
    elif normalized in {"Makefile", "docker-compose.yml", "docker-compose.yaml"}:
        mark_ci_config(flags)


def route_path(flags: dict[str, bool], path: str) -> str:
    normalized = normalize_path(path)
    if is_ignored_path(normalized):
        return "ignored"

    probe = dict(FLAGS)
    apply_path_rules(probe, normalized)
    if not any(probe[name] for name in ROUTING_FLAG_NAMES):
        return "unmapped"
    apply_path_rules(flags, normalized)
    return "mapped"


def classify_paths(paths: list[str]) -> ClassificationResult:
    flags = dict(FLAGS)
    normalized_paths = tuple(dict.fromkeys(normalize_path(path) for path in paths if path.strip()))
    ignored: list[str] = []
    unmapped: list[str] = []
    for path in normalized_paths:
        status = route_path(flags, path)
        if status == "ignored":
            ignored.append(path)
        elif status == "unmapped":
            unmapped.append(path)

    enabled_components = [component for component in COMPONENTS if flags[f"RUN_{component}"]]
    if enabled_components:
        flags["RUN_COMPONENT_CI"] = True
        flags["RUN_COMPONENT_BUILD"] = True
        flags["RUN_COMPONENT_DEPLOY"] = True
        flags["RUN_PYTHON"] = any(component in PYTHON_COMPONENTS for component in enabled_components)

    return ClassificationResult(
        flags=flags,
        changed_paths=normalized_paths,
        ignored_paths=tuple(ignored),
        unmapped_paths=tuple(unmapped),
    )


def classify(paths: list[str]) -> dict[str, bool]:
    """Compatibility wrapper used by unit tests and existing callers."""
    return classify_paths(paths).flags


def render_environment(result: ClassificationResult) -> str:
    lines = [f"{name}={'true' if value else 'false'}" for name, value in sorted(result.flags.items())]
    lines.extend(
        (
            f"CHANGED_COMPONENTS={','.join(result.component_names) if result.component_names else 'unchanged'}",
            f"CHANGED_PATHS_COUNT={len(result.changed_paths)}",
            f"IGNORED_PATHS_COUNT={len(result.ignored_paths)}",
            f"UNMAPPED_PATHS_COUNT={len(result.unmapped_paths)}",
            f"UNMAPPED_PATHS={'|'.join(result.unmapped_paths)}",
        )
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Map changed paths to path-based RecSys CI/CD component flags.")
    parser.add_argument("--base-ref", default="")
    parser.add_argument("--path", action="append", default=[], help="Classify an explicit path instead of reading git diff.")
    args = parser.parse_args()

    paths = args.path or changed_paths(args.base_ref or None)
    result = classify_paths(paths)
    print(render_environment(result))
    if result.unmapped_paths:
        print(
            "ERROR: Unmapped runtime path(s). Add an explicit CI/CD routing rule: "
            + ", ".join(result.unmapped_paths),
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

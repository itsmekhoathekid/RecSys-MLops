from __future__ import annotations

import argparse
import subprocess
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
    "DRIFT",
    "STREAM_OFFLINE",
    "STREAM_ONLINE",
)

FLAGS = {f"RUN_{component}": False for component in COMPONENTS}
FLAGS.update(
    {
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

PYTHON_COMPONENTS = (
    "MATERIALIZE",
    "TRAINING",
    "SPARK_BATCH",
    "DP1",
    "DP2",
    "DP3",
    "API",
    "KSERVE",
    "DRIFT",
    "STREAM_OFFLINE",
    "STREAM_ONLINE",
)


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
            paths = git_lines(args)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        if paths:
            return list(dict.fromkeys(paths))
    return []


def changed_paths(base_ref: str | None) -> list[str]:
    candidates: list[list[str]] = []
    if base_ref:
        candidates.append(["diff", "--name-only", f"{base_ref}...HEAD"])
    candidates.append(["diff", "--name-only", "HEAD~1", "HEAD"])

    for args in candidates:
        try:
            paths = git_lines(args)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        if paths:
            return paths

    return current_commit_paths()


def mark(flags: dict[str, bool], *components: str) -> None:
    for component in components:
        flags[f"RUN_{component}"] = True


def mark_data_platform(flags: dict[str, bool]) -> None:
    mark(flags, *DATA_PLATFORM_COMPONENTS)


def mark_all_python(flags: dict[str, bool]) -> None:
    mark(flags, *PYTHON_COMPONENTS)


def classify_config(flags: dict[str, bool], path: str) -> None:
    name = Path(path).name
    if name.startswith("bst") or name in {"ray-cluster.yaml"}:
        mark(flags, "TRAINING")
    if name.startswith("spark_batch"):
        mark(flags, "SPARK_BATCH", "DP2", "DP3")
    if name.startswith("data_generator") or name in {"postgres_source.yaml", "kafka_topics.yaml"}:
        mark(flags, "DP1")
    if name in {"flink_streaming.yaml", "redis_online_store.yaml"}:
        mark(flags, "STREAM_OFFLINE", "STREAM_ONLINE")
    if name in {"data_flow.yaml", "airflow.yaml"}:
        mark_data_platform(flags)


def classify_data_platform_source(flags: dict[str, bool], path: str) -> None:
    if path.startswith("apps/data-platform/src/feature_store/"):
        mark(flags, "MATERIALIZE", "DP3", "STREAM_ONLINE")
    elif path.startswith("apps/data-platform/src/local/"):
        mark(flags, "MATERIALIZE")
    elif path.startswith("apps/data-platform/src/ingest/"):
        mark(flags, "DP1")
    elif path.startswith("apps/data-platform/src/features/spark/"):
        mark(flags, "SPARK_BATCH", "DP2", "DP3")
    elif path.startswith("apps/data-platform/src/features/flink/"):
        mark(flags, "STREAM_OFFLINE", "STREAM_ONLINE")
    elif path.startswith("apps/data-platform/src/validate/") or path.startswith("apps/data-platform/src/mlops/"):
        mark(flags, "DRIFT")
    elif path.startswith("apps/data-platform/src/lakehouse/"):
        mark(flags, "SPARK_BATCH", "DP1", "DP2", "DP3", "STREAM_OFFLINE")
    elif path.startswith("apps/data-platform/src/metadata/"):
        mark(flags, "MATERIALIZE", "DP1", "DP2", "DP3")
    elif path.startswith("apps/data-platform/src/orchestration/airflow/dags/"):
        classify_airflow_dag(flags, path)
    elif path.startswith("apps/data-platform/src/config/") or path.startswith("apps/data-platform/src/monitoring/"):
        mark_data_platform(flags)
    elif path.startswith("apps/data-platform/src/"):
        mark_data_platform(flags)


def classify_airflow_dag(flags: dict[str, bool], path: str) -> None:
    name = Path(path).name
    if name == "raw_ingestion_dag.py":
        mark(flags, "DP1")
    elif name == "batch_feature_pipeline_dag.py":
        mark(flags, "SPARK_BATCH", "DP2", "DP3")
    elif name == "streaming_feature_pipeline_dag.py":
        mark(flags, "STREAM_OFFLINE", "STREAM_ONLINE")
    elif name in {"full_dataflow_local_dag.py", "k8s_data_platform_dag.py"}:
        mark_data_platform(flags)
    else:
        mark_data_platform(flags)


def classify_infra(flags: dict[str, bool], path: str) -> None:
    if path.startswith("infra/kubeflow/"):
        mark(flags, "TRAINING")
    elif path.startswith("infra/helm/recsys-serving/"):
        mark(flags, "API", "KSERVE")
    elif path.startswith("infra/helm/recsys-data-platform/"):
        mark_data_platform(flags)
    elif path.startswith("infra/helm/ray-cluster/") or path.startswith("infra/helm/recsys-runtime/") or path.startswith(
        "infra/helm/mlflow-stack/"
    ):
        mark(flags, "TRAINING")
    elif path.startswith("infra/helm/recsys-observability/"):
        mark(flags, "API", "KSERVE", "DRIFT")
    elif path.startswith("infra/docker/Dockerfile.base-python"):
        mark(flags, "MATERIALIZE", "TRAINING", "DP1", "DP3", "DRIFT", "STREAM_ONLINE")
    elif path.startswith("infra/docker/Dockerfile.airflow"):
        mark_data_platform(flags)
    elif path.startswith("infra/docker/Dockerfile.kafka-connect"):
        mark(flags, "DP1")
    elif path.startswith("infra/docker/"):
        mark_data_platform(flags)
    elif path.startswith("infra/k8s/scripts/data_platform") or path.startswith("infra/k8s/scripts/cluster_data_setup"):
        mark_data_platform(flags)
    elif path.startswith("infra/k8s/scripts/cluster_mlops_serving"):
        mark(flags, "TRAINING", "API", "KSERVE")


def classify_tests(flags: dict[str, bool], path: str) -> None:
    if path.startswith("tests/unit/api_serving/"):
        mark(flags, "API")
    elif path.startswith("tests/unit/ml_system/"):
        mark(flags, "TRAINING")
        if Path(path).name == "test_model_promotion.py":
            mark(flags, "KSERVE")
    elif path.startswith("tests/unit/data_generator/"):
        mark(flags, "DP1")
    elif path.startswith("tests/unit/data_platform/"):
        mark_data_platform(flags)
    elif path.startswith("tests/contract/test_serving_contracts.py"):
        mark(flags, "API", "KSERVE")
    elif path.startswith("tests/contract/test_gateway_contracts.py"):
        mark(flags, "API")
    elif path.startswith("tests/contract/test_docker_dataflow_contracts.py"):
        mark_data_platform(flags)
    elif path.startswith("tests/integration/"):
        parts = Path(path).parts
        if len(parts) >= 3:
            component = parts[2].upper().replace("-", "_")
            if component in COMPONENTS:
                mark(flags, component)


def classify(paths: list[str]) -> dict[str, bool]:
    flags = dict(FLAGS)
    for path in paths:
        parts = Path(path).parts

        if path in {"pyproject.toml", "uv.lock", "requirements.txt"}:
            mark_all_python(flags)
        elif path.startswith("configs/"):
            classify_config(flags, path)
        elif path.startswith("apps/api-serving/") or path.startswith("apps/api/"):
            mark(flags, "API")
        elif path.startswith("apps/ml-system/"):
            mark(flags, "TRAINING")
            if parts[-1] in {"model_promotion.py", "trigger_kserve_cd.py"}:
                mark(flags, "KSERVE")
        elif path.startswith("apps/data-platform/data-generator/"):
            mark(flags, "DP1")
        elif path == "apps/data-platform/Dockerfile.spark":
            mark(flags, "SPARK_BATCH", "DP2", "DP3")
        elif path == "apps/data-platform/Dockerfile.flink":
            mark(flags, "STREAM_OFFLINE", "STREAM_ONLINE")
        elif path == "apps/data-platform/Dockerfile.dataflow-cli":
            mark(flags, "MATERIALIZE", "DP1", "DP3", "DRIFT", "STREAM_ONLINE")
        elif path.startswith("apps/data-platform/src/"):
            classify_data_platform_source(flags, path)
        elif path.startswith("infra/"):
            classify_infra(flags, path)
        elif path == "jenkins/scripts/model_cd.py":
            mark(flags, "KSERVE")
        elif path.startswith("jenkins/"):
            mark_all_python(flags)
        elif path.startswith("tests/"):
            classify_tests(flags, path)

    enabled_components = [component for component in COMPONENTS if flags[f"RUN_{component}"]]
    if enabled_components:
        flags["RUN_COMPONENT_CI"] = True
        flags["RUN_COMPONENT_BUILD"] = True
        flags["RUN_COMPONENT_DEPLOY"] = True
        flags["RUN_PYTHON"] = any(component in PYTHON_COMPONENTS for component in enabled_components)
    return flags


def main() -> int:
    parser = argparse.ArgumentParser(description="Map changed paths to path-based RecSys CI/CD component flags.")
    parser.add_argument("--base-ref", default="")
    args = parser.parse_args()

    paths = changed_paths(args.base_ref or None)
    flags = classify(paths)
    components = [component.lower() for component in COMPONENTS if flags[f"RUN_{component}"]]

    for name in sorted(flags):
        print(f"{name}={'true' if flags[name] else 'false'}")
    print(f"CHANGED_COMPONENTS={','.join(components) if components else 'docs-only'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

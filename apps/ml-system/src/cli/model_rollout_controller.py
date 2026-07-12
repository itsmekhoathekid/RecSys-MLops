from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Any

from cli.trigger_kserve_cd import basic_auth_header, request, trigger_jenkins_cd


ROLLOUT_STAGES = ("shadow-start", "ab-start", "ab-step", "evaluate", "promote", "rollback")


def _field(value: Any, name: str, default: Any = "") -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _tags(version: Any) -> dict[str, str]:
    return {str(key): str(value) for key, value in (_field(version, "tags", {}) or {}).items()}


def _safe_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")
    return normalized[:120] or "candidate"


@dataclass(frozen=True)
class RolloutConfig:
    registered_model_name: str = "recsys_bst_ranker"
    candidate_tag_key: str = "candidate"
    pending_value: str = "test"
    claimed_value: str = "testing"
    tested_value: str = "tested"
    control_manifest_uri: str = "s3://recsys-model-store/promotions/bst/latest.json"
    stable_manifest_uri: str = "s3://recsys-model-store/promotions/bst/latest.json"
    jenkins_url: str = "http://recsys-jenkins.ci.svc.cluster.local:8080"
    jenkins_job_name: str = "RecSys-KServe-Model-CD"
    jenkins_user: str = ""
    jenkins_token: str = ""
    jenkins_workspace: str = "/var/jenkins_home/recsys-workspace"
    image_tag: str = "manual"
    prometheus_url: str = "http://recsys-prometheus.observability.svc.cluster.local:9090"
    gate_window: str = "10m"
    min_samples: int = 100
    auto_progressive_enabled: bool = True
    progressive_weights: tuple[int, ...] = (10, 25, 50)
    stage_min_observation_seconds: int = 30
    poll_seconds: int = 15
    build_timeout_seconds: int = 1800

    @classmethod
    def from_env(cls) -> "RolloutConfig":
        raw_weights = os.getenv("ROLLOUT_PROGRESSIVE_WEIGHTS", "10,25,50")
        progressive_weights = tuple(
            int(item.strip()) for item in raw_weights.split(",") if item.strip()
        )
        if not progressive_weights:
            raise ValueError("ROLLOUT_PROGRESSIVE_WEIGHTS must contain at least one weight")
        if any(weight <= 0 or weight >= 100 for weight in progressive_weights):
            raise ValueError("ROLLOUT_PROGRESSIVE_WEIGHTS must be between 1 and 99")
        return cls(
            registered_model_name=os.getenv("MLFLOW_REGISTERED_MODEL_NAME", cls.registered_model_name),
            candidate_tag_key=os.getenv("CANDIDATE_TAG_KEY", cls.candidate_tag_key),
            pending_value=os.getenv("CANDIDATE_PENDING_VALUE", cls.pending_value),
            claimed_value=os.getenv("CANDIDATE_CLAIMED_VALUE", cls.claimed_value),
            tested_value=os.getenv("CANDIDATE_TESTED_VALUE", cls.tested_value),
            control_manifest_uri=os.getenv("CONTROL_MANIFEST_URI", cls.control_manifest_uri),
            stable_manifest_uri=os.getenv("PROMOTION_MANIFEST_URI", cls.stable_manifest_uri),
            jenkins_url=os.getenv("JENKINS_URL", cls.jenkins_url),
            jenkins_job_name=os.getenv("KSERVE_CD_JOB_NAME", cls.jenkins_job_name),
            jenkins_user=os.getenv("JENKINS_USER") or os.getenv("JENKINS_USERNAME", ""),
            jenkins_token=os.getenv("JENKINS_TOKEN") or os.getenv("JENKINS_PASSWORD", ""),
            jenkins_workspace=os.getenv("RECSYS_CI_WORKSPACE", cls.jenkins_workspace),
            image_tag=os.getenv("IMAGE_TAG", cls.image_tag),
            prometheus_url=os.getenv("PROMETHEUS_URL", cls.prometheus_url),
            gate_window=os.getenv("AB_GATE_WINDOW", cls.gate_window),
            min_samples=int(os.getenv("AB_MIN_SAMPLES", str(cls.min_samples))),
            auto_progressive_enabled=os.getenv(
                "ROLLOUT_AUTO_PROGRESSIVE_ENABLED", "true"
            ).lower()
            in {"1", "true", "yes", "on"},
            progressive_weights=progressive_weights,
            stage_min_observation_seconds=max(
                1,
                int(
                    os.getenv(
                        "ROLLOUT_STAGE_MIN_OBSERVATION_SECONDS",
                        str(cls.stage_min_observation_seconds),
                    )
                ),
            ),
            poll_seconds=max(1, int(os.getenv("ROLLOUT_WATCH_POLL_SECONDS", str(cls.poll_seconds)))),
            build_timeout_seconds=max(
                30,
                int(os.getenv("ROLLOUT_BUILD_TIMEOUT_SECONDS", str(cls.build_timeout_seconds))),
            ),
        )


def mlflow_client():
    import mlflow
    from mlflow.tracking import MlflowClient

    tracking_uri = os.getenv("MLFLOW_TRACKING_URI")
    if tracking_uri:
        mlflow.set_tracking_uri(tracking_uri)
    return MlflowClient(tracking_uri=tracking_uri)


def get_version(client: Any, config: RolloutConfig, version: str) -> Any:
    return client.get_model_version(config.registered_model_name, str(version))


def pending_candidates(client: Any, config: RolloutConfig) -> list[Any]:
    versions = client.search_model_versions(
        filter_string=f"name='{config.registered_model_name}'",
        max_results=100,
    )
    matches = [
        version
        for version in versions
        if _tags(version).get(config.candidate_tag_key) == config.pending_value
    ]

    def sort_key(version: Any) -> tuple[int, str]:
        raw = str(_field(version, "version", "0"))
        return (int(raw) if raw.isdigit() else -1, raw)

    return sorted(matches, key=sort_key, reverse=True)


def active_candidates(client: Any, config: RolloutConfig) -> list[Any]:
    versions = client.search_model_versions(
        filter_string=f"name='{config.registered_model_name}'",
        max_results=100,
    )
    matches = []
    for version in versions:
        tags = _tags(version)
        status = tags.get("rollout_status", "")
        active_status = status == "shadow_ready" or re.fullmatch(
            r"(?:ab|hold|gate_passed)_\d+", status
        )
        if tags.get(config.candidate_tag_key) == config.tested_value and active_status:
            matches.append(version)

    def sort_key(version: Any) -> tuple[int, str]:
        raw = str(_field(version, "version", "0"))
        return (int(raw) if raw.isdigit() else -1, raw)

    return sorted(matches, key=sort_key, reverse=True)


def set_tag(client: Any, config: RolloutConfig, version: str, key: str, value: str) -> None:
    client.set_model_version_tag(config.registered_model_name, str(version), key, str(value))


def delete_candidate_alias_if_owned(client: Any, config: RolloutConfig, version: str) -> None:
    try:
        current = client.get_model_version_by_alias(config.registered_model_name, "candidate")
    except Exception:
        return
    if str(_field(current, "version")) == str(version):
        client.delete_registered_model_alias(config.registered_model_name, "candidate")


def candidate_manifest_uri(version: Any) -> str:
    tags = _tags(version)
    manifest_uri = tags.get("promotion_manifest_uri", "")
    if not manifest_uri:
        raise ValueError(
            f"MLflow model version {_field(version, 'version')} is missing promotion_manifest_uri tag"
        )
    return manifest_uri


def experiment_id(version: Any) -> str:
    tags = _tags(version)
    existing = tags.get("rollout_experiment_id")
    if existing:
        return existing
    model_version = tags.get("model_version") or str(_field(version, "version"))
    return f"bst-{_safe_id(model_version)}"


def rollout_params(
    config: RolloutConfig,
    version: Any,
    *,
    stage: str,
    weight: int,
    gate_window: str | None = None,
) -> dict[str, str]:
    if stage not in ROLLOUT_STAGES:
        raise ValueError(f"Unsupported rollout stage: {stage}")
    tags = _tags(version)
    model_version = tags.get("model_version") or str(_field(version, "version"))
    return {
        "ROLLOUT_STAGE": stage,
        "PROMOTION_MANIFEST_URI": config.stable_manifest_uri,
        "CONTROL_MANIFEST_URI": config.control_manifest_uri,
        "CANDIDATE_MANIFEST_URI": candidate_manifest_uri(version),
        "AB_EXPERIMENT_ID": experiment_id(version),
        "AB_CANDIDATE_WEIGHT_PERCENT": str(max(0, min(100, weight))),
        "PROMETHEUS_URL": config.prometheus_url,
        "AB_GATE_WINDOW": gate_window or config.gate_window,
        "AB_MIN_SAMPLES": str(config.min_samples),
        "MODEL_VERSION": model_version,
        "METRIC_NAME": tags.get("metric_name", ""),
        "METRIC_VALUE": tags.get("metric_value", ""),
        "TRIGGER_SOURCE": "mlflow-candidate-watcher",
        "RECSYS_CI_WORKSPACE": config.jenkins_workspace,
        "IMAGE_TAG": config.image_tag,
    }


def trigger_stage(
    client: Any,
    config: RolloutConfig,
    version: Any,
    *,
    stage: str,
    weight: int = 0,
    gate_window: str | None = None,
) -> dict[str, Any]:
    version_id = str(_field(version, "version"))
    params = rollout_params(
        config,
        version,
        stage=stage,
        weight=weight,
        gate_window=gate_window,
    )
    set_tag(client, config, version_id, "rollout_status", f"jenkins_{stage}")
    result = trigger_jenkins_cd(
        jenkins_url=config.jenkins_url,
        job_name=config.jenkins_job_name,
        params=params,
        user=config.jenkins_user,
        token=config.jenkins_token,
        wait=True,
        poll_interval_seconds=5,
        timeout_seconds=config.build_timeout_seconds,
    )
    set_tag(client, config, version_id, "rollout_experiment_id", params["AB_EXPERIMENT_ID"])
    set_tag(client, config, version_id, "rollout_build_number", str(result.get("build_number", "")))

    if stage == "shadow-start":
        set_tag(client, config, version_id, config.candidate_tag_key, config.tested_value)
        set_tag(client, config, version_id, "rollout_status", "shadow_ready")
    elif stage in {"ab-start", "ab-step"}:
        set_tag(client, config, version_id, "rollout_status", f"ab_{weight}")
        set_tag(client, config, version_id, "rollout_stage_weight", str(weight))
        set_tag(client, config, version_id, "rollout_stage_started_at", str(int(time.time())))
        set_tag(client, config, version_id, "rollout_decision", "pending")
        set_tag(client, config, version_id, "rollout_candidate_samples", "0")
        set_tag(client, config, version_id, "rollout_control_samples", "0")
    elif stage == "evaluate":
        decision = jenkins_gate_decision(config, result)
        if decision:
            set_tag(client, config, version_id, "rollout_decision", decision)
        set_tag(client, config, version_id, "rollout_gate_window", params["AB_GATE_WINDOW"])
        if decision == "rollback":
            delete_candidate_alias_if_owned(client, config, version_id)
            set_tag(client, config, version_id, config.candidate_tag_key, "rolled_back")
            set_tag(client, config, version_id, "rollout_status", "rolled_back")
        elif decision == "hold":
            set_tag(client, config, version_id, "rollout_status", f"hold_{weight}")
            set_tag(client, config, version_id, "rollout_stage_started_at", str(int(time.time())))
            set_tag(client, config, version_id, "rollout_candidate_samples", "0")
            set_tag(client, config, version_id, "rollout_control_samples", "0")
        elif decision == "promote":
            set_tag(client, config, version_id, "rollout_status", f"gate_passed_{weight}")
        else:
            set_tag(client, config, version_id, "rollout_status", f"evaluated_{weight}")
    elif stage == "promote":
        promote_aliases(client, config, version_id)
        set_tag(client, config, version_id, config.candidate_tag_key, "promoted")
        set_tag(client, config, version_id, "rollout_status", "champion")
    elif stage == "rollback":
        delete_candidate_alias_if_owned(client, config, version_id)
        set_tag(client, config, version_id, config.candidate_tag_key, "rolled_back")
        set_tag(client, config, version_id, "rollout_status", "rolled_back")
    return result


def jenkins_gate_decision(config: RolloutConfig, result: dict[str, Any]) -> str:
    build_url = str(result.get("build_url", "")).rstrip("/")
    if not build_url:
        return ""
    try:
        _, _, console = request(
            f"{build_url}/consoleText",
            headers=basic_auth_header(config.jenkins_user, config.jenkins_token),
            timeout=30,
        )
    except Exception:
        return ""
    matches = re.findall(r'"decision"\s*:\s*"(hold|promote|rollback)"', console)
    return matches[-1] if matches else ""


def promote_aliases(client: Any, config: RolloutConfig, version: str) -> None:
    try:
        champion = client.get_model_version_by_alias(config.registered_model_name, "champion")
    except Exception:
        champion = None
    if champion is not None and str(_field(champion, "version")) != str(version):
        client.set_registered_model_alias(
            config.registered_model_name,
            "previous",
            str(_field(champion, "version")),
        )
    client.set_registered_model_alias(config.registered_model_name, "champion", str(version))
    delete_candidate_alias_if_owned(client, config, str(version))


def process_candidate(client: Any, config: RolloutConfig, version: Any) -> dict[str, Any]:
    version_id = str(_field(version, "version"))
    if _tags(version).get(config.candidate_tag_key) != config.pending_value:
        return {"processed": False, "version": version_id, "reason": "not_pending"}
    try:
        candidate_manifest_uri(version)
    except ValueError as exc:
        delete_candidate_alias_if_owned(client, config, version_id)
        set_tag(client, config, version_id, config.candidate_tag_key, "invalid")
        set_tag(client, config, version_id, "rollout_status", "manifest_missing")
        set_tag(client, config, version_id, "rollout_error", str(exc)[:250])
        return {
            "processed": False,
            "version": version_id,
            "reason": "missing_promotion_manifest_uri",
        }
    set_tag(client, config, version_id, config.candidate_tag_key, config.claimed_value)
    set_tag(client, config, version_id, "rollout_status", "shadow_deploying")
    client.set_registered_model_alias(config.registered_model_name, "candidate", version_id)
    try:
        result = trigger_stage(client, config, version, stage="shadow-start", weight=0)
    except Exception as exc:
        delete_candidate_alias_if_owned(client, config, version_id)
        set_tag(client, config, version_id, config.candidate_tag_key, "failed")
        set_tag(client, config, version_id, "rollout_status", "shadow_failed")
        set_tag(client, config, version_id, "rollout_error", str(exc)[:250])
        raise
    return {"processed": True, "version": version_id, "jenkins": result}


def query_prometheus(config: RolloutConfig, query: str) -> float:
    encoded = urllib.parse.urlencode({"query": query})
    _, _, body = request(
        f"{config.prometheus_url.rstrip('/')}/api/v1/query?{encoded}",
        timeout=15,
    )
    payload = json.loads(body)
    result = payload.get("data", {}).get("result", [])
    if not result:
        return 0.0
    return float(result[0]["value"][1])


def stage_sample_counts(config: RolloutConfig, version: Any) -> dict[str, Any]:
    tags = _tags(version)
    started_at = int(float(tags.get("rollout_stage_started_at", "0") or "0"))
    elapsed_seconds = max(0, int(time.time()) - started_at) if started_at else 0
    if elapsed_seconds < config.stage_min_observation_seconds:
        return {
            "candidate": 0.0,
            "control": 0.0,
            "elapsed_seconds": elapsed_seconds,
            "ready": False,
            "phase": "warming_up_after_stage_transition",
            "progress_percent": 0.0,
        }

    experiment = experiment_id(version).replace("\\", "\\\\").replace('"', '\\"')
    window = f"{max(1, elapsed_seconds)}s"
    candidate_samples = query_prometheus(
        config,
        f'sum(increase(model_predictions_total{{ab_variant="candidate",experiment_id="{experiment}"}}[{window}]))',
    )
    control_samples = query_prometheus(
        config,
        f'sum(increase(model_predictions_total{{ab_variant="control",experiment_id="{experiment}"}}[{window}]))',
    )
    ready = candidate_samples >= config.min_samples and control_samples >= config.min_samples
    if ready:
        phase = "ready_for_evaluation"
    elif candidate_samples == 0 and control_samples == 0:
        phase = "waiting_for_traffic_or_first_prometheus_scrape"
    else:
        phase = "collecting_prometheus_samples"
    return {
        "candidate": candidate_samples,
        "control": control_samples,
        "elapsed_seconds": elapsed_seconds,
        "ready": ready,
        "phase": phase,
        "progress_percent": min(
            100.0,
            min(candidate_samples, control_samples) / max(1, config.min_samples) * 100.0,
        ),
    }


def reconcile_progressive_candidate(
    client: Any,
    config: RolloutConfig,
    version: Any,
) -> dict[str, Any]:
    version_id = str(_field(version, "version"))
    tags = _tags(version)
    status = tags.get("rollout_status", "")
    weights = config.progressive_weights

    if not config.auto_progressive_enabled:
        return {"processed": False, "version": version_id, "reason": "auto_progressive_disabled"}

    if status == "shadow_ready":
        result = trigger_stage(client, config, version, stage="ab-start", weight=weights[0])
        return {
            "processed": True,
            "version": version_id,
            "action": f"open_ab_{weights[0]}",
            "jenkins": result,
        }

    match = re.fullmatch(r"(?:ab|hold)_(\d+)", status)
    if match:
        weight = int(match.group(1))
        if not tags.get("rollout_stage_started_at"):
            set_tag(
                client,
                config,
                version_id,
                "rollout_stage_started_at",
                str(int(time.time())),
            )
            set_tag(client, config, version_id, "rollout_stage_weight", str(weight))
            set_tag(client, config, version_id, "rollout_candidate_samples", "0")
            set_tag(client, config, version_id, "rollout_control_samples", "0")
            set_tag(client, config, version_id, "rollout_required_samples", str(config.min_samples))
            return {
                "processed": True,
                "version": version_id,
                "action": f"initialize_sample_window_{weight}",
            }
        samples = stage_sample_counts(config, version)
        set_tag(client, config, version_id, "rollout_candidate_samples", f"{samples['candidate']:.0f}")
        set_tag(client, config, version_id, "rollout_control_samples", f"{samples['control']:.0f}")
        set_tag(client, config, version_id, "rollout_required_samples", str(config.min_samples))
        if not samples["ready"]:
            return {
                "processed": False,
                "healthy": True,
                "decision": "WAIT",
                "version": version_id,
                "reason": "awaiting_prometheus_samples",
                "weight": weight,
                "samples": samples,
            }
        gate_window = f"{max(config.stage_min_observation_seconds, int(samples['elapsed_seconds']))}s"
        result = trigger_stage(
            client,
            config,
            version,
            stage="evaluate",
            weight=weight,
            gate_window=gate_window,
        )
        return {
            "processed": True,
            "version": version_id,
            "action": f"evaluate_{weight}",
            "gate_window": gate_window,
            "samples": samples,
            "jenkins": result,
        }

    match = re.fullmatch(r"gate_passed_(\d+)", status)
    if match:
        weight = int(match.group(1))
        try:
            next_weight = weights[weights.index(weight) + 1]
        except IndexError:
            result = trigger_stage(client, config, version, stage="promote", weight=0)
            return {
                "processed": True,
                "version": version_id,
                "action": "promote_champion",
                "jenkins": result,
            }
        except ValueError as exc:
            raise ValueError(f"Unexpected passed rollout weight: {weight}") from exc
        result = trigger_stage(client, config, version, stage="ab-step", weight=next_weight)
        return {
            "processed": True,
            "version": version_id,
            "action": f"increase_ab_{next_weight}",
            "jenkins": result,
        }

    return {"processed": False, "version": version_id, "reason": f"inactive_status:{status}"}


def watch_once(client: Any, config: RolloutConfig) -> dict[str, Any]:
    candidates = pending_candidates(client, config)
    if candidates:
        return process_candidate(client, config, candidates[0])
    active = active_candidates(client, config)
    if active:
        return reconcile_progressive_candidate(client, config, active[0])
    return {"processed": False, "reason": "no_pending_or_active_candidate"}


def status_payload(version: Any, config: RolloutConfig) -> dict[str, Any]:
    tags = _tags(version)
    return {
        "registered_model_name": config.registered_model_name,
        "registry_version": str(_field(version, "version")),
        "model_version": tags.get("model_version", ""),
        "candidate": tags.get(config.candidate_tag_key, ""),
        "rollout_status": tags.get("rollout_status", ""),
        "rollout_experiment_id": tags.get("rollout_experiment_id", ""),
        "promotion_manifest_uri": tags.get("promotion_manifest_uri", ""),
        "rollout_build_number": tags.get("rollout_build_number", ""),
        "rollout_stage_weight": tags.get("rollout_stage_weight", ""),
        "rollout_candidate_samples": tags.get("rollout_candidate_samples", ""),
        "rollout_control_samples": tags.get("rollout_control_samples", ""),
        "rollout_required_samples": tags.get("rollout_required_samples", ""),
        "rollout_error": tags.get("rollout_error", ""),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch MLflow candidate tags and drive Jenkins model rollout stages")
    subparsers = parser.add_subparsers(dest="command", required=True)
    watch_parser = subparsers.add_parser("watch")
    watch_parser.add_argument("--once", action="store_true")
    mark_parser = subparsers.add_parser("mark")
    mark_parser.add_argument("--version", required=True)
    mark_parser.add_argument("--manifest-uri", default="")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--version", required=True)
    stage_parser = subparsers.add_parser("stage")
    stage_parser.add_argument("--version", required=True)
    stage_parser.add_argument("--stage", required=True, choices=ROLLOUT_STAGES)
    stage_parser.add_argument("--weight", type=int, default=0)
    subparsers.add_parser("config")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = RolloutConfig.from_env()
    if args.command == "config":
        safe = {**asdict(config), "jenkins_token": "***" if config.jenkins_token else ""}
        print(json.dumps(safe, indent=2, sort_keys=True))
        return 0
    client = mlflow_client()
    if args.command == "mark":
        version = get_version(client, config, args.version)
        if args.manifest_uri:
            set_tag(client, config, args.version, "promotion_manifest_uri", args.manifest_uri)
            version = get_version(client, config, args.version)
        candidate_manifest_uri(version)
        set_tag(client, config, args.version, config.candidate_tag_key, config.pending_value)
        set_tag(client, config, args.version, "rollout_status", "pending")
        print(json.dumps(status_payload(get_version(client, config, args.version), config), sort_keys=True))
        return 0
    if args.command == "status":
        print(json.dumps(status_payload(get_version(client, config, args.version), config), sort_keys=True))
        return 0
    if args.command == "stage":
        version = get_version(client, config, args.version)
        result = trigger_stage(client, config, version, stage=args.stage, weight=args.weight)
        print(json.dumps({"status": status_payload(get_version(client, config, args.version), config), "jenkins": result}, sort_keys=True))
        return 0
    if args.once:
        print(json.dumps(watch_once(client, config), sort_keys=True))
        return 0
    while True:
        try:
            result = watch_once(client, config)
            if result.get("processed") or result.get("reason") == "awaiting_prometheus_samples":
                print(json.dumps(result, sort_keys=True), flush=True)
        except Exception as exc:
            print(json.dumps({"watch_error": str(exc)}, sort_keys=True), flush=True)
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

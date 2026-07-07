from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


TERMINAL_SUCCESS = {
    "SUCCEEDED",
    "PIPELINE_STATE_SUCCEEDED",
    "RuntimeState.SUCCEEDED",
    "PipelineState.PIPELINE_STATE_SUCCEEDED",
}
TERMINAL_FAILURE = {
    "FAILED",
    "CANCELLED",
    "SKIPPED",
    "ERROR",
    "PIPELINE_STATE_FAILED",
    "PIPELINE_STATE_CANCELED",
    "PipelineState.PIPELINE_STATE_FAILED",
    "PipelineState.PIPELINE_STATE_CANCELED",
}


def _attr_or_key(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def extract_run_id(run: Any) -> str:
    for candidate in (run, _attr_or_key(run, "run")):
        for field in ("run_id", "id", "resource_id"):
            value = _attr_or_key(candidate, field)
            if value:
                return str(value)
    raise RuntimeError(f"Could not determine Kubeflow run id from {run!r}")


def normalize_state(value: Any) -> str:
    if value is None:
        return ""
    raw = getattr(value, "name", value)
    text = str(raw).strip()
    if "." in text:
        text = text.split(".")[-1]
    return text.upper()


def extract_state(run_details: Any) -> str:
    for candidate in (run_details, _attr_or_key(run_details, "run")):
        for field in ("state", "status", "runtime_state"):
            state = normalize_state(_attr_or_key(candidate, field))
            if state:
                return state
    return ""


def load_arguments(path: str) -> dict[str, Any]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("--arguments-json must contain a JSON object")
    return payload


def parse_argument_overrides(items: list[str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for item in items:
        key, sep, raw_value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--argument must be in key=value form, got: {item!r}")
        value: Any
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        overrides[key.strip()] = value
    return overrides


def create_run(client: Any, package_path: str, experiment_name: str, run_name: str, arguments: dict[str, Any]) -> Any:
    experiment = client.create_experiment(name=experiment_name)
    experiment_id = _attr_or_key(experiment, "experiment_id") or _attr_or_key(experiment, "id")
    try:
        return client.create_run_from_pipeline_package(
            pipeline_file=package_path,
            arguments=arguments,
            experiment_id=experiment_id,
            run_name=run_name,
        )
    except TypeError:
        return client.create_run_from_pipeline_package(
            pipeline_file=package_path,
            arguments=arguments,
            experiment_name=experiment_name,
            run_name=run_name,
        )


def get_run(client: Any, run_id: str) -> Any:
    try:
        return client.get_run(run_id=run_id)
    except TypeError:
        return client.get_run(run_id)


def wait_for_run(client: Any, run_id: str, timeout_seconds: int, poll_seconds: int) -> str:
    deadline = time.time() + timeout_seconds
    last_state = ""
    while time.time() <= deadline:
        details = get_run(client, run_id)
        state = extract_state(details)
        if state and state != last_state:
            print(json.dumps({"run_id": run_id, "state": state}, sort_keys=True))
            last_state = state
        if state in TERMINAL_SUCCESS:
            return state
        if state in TERMINAL_FAILURE:
            raise RuntimeError(f"Kubeflow run {run_id} finished with state {state}")
        time.sleep(poll_seconds)
    raise TimeoutError(f"Timed out waiting for Kubeflow run {run_id} after {timeout_seconds}s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit and optionally wait for a Kubeflow pipeline package.")
    parser.add_argument("--host", default="http://127.0.0.1:8888")
    parser.add_argument("--package-path", default="infra/kubeflow/compiled/bst_training_pipeline.yaml")
    parser.add_argument("--experiment-name", default="recsys-bst-ranking")
    parser.add_argument("--run-name", default=f"recsys-bst-e2e-{int(time.time())}")
    parser.add_argument("--arguments-json", default="")
    parser.add_argument("--argument", action="append", default=[], help="Pipeline argument override as key=value")
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--no-wait", action="store_true")
    args = parser.parse_args()

    import kfp

    client = kfp.Client(host=args.host)
    run = create_run(
        client,
        package_path=args.package_path,
        experiment_name=args.experiment_name,
        run_name=args.run_name,
        arguments={**load_arguments(args.arguments_json), **parse_argument_overrides(args.argument)},
    )
    run_id = extract_run_id(run)
    result = {"run_id": run_id, "run_name": args.run_name}
    if not args.no_wait:
        result["state"] = wait_for_run(client, run_id, args.timeout_seconds, args.poll_seconds)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

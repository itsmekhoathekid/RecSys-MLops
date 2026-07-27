from __future__ import annotations

import argparse
import json
from pathlib import Path


def _assert_deleted(fetch: object, resource_id: str) -> None:
    try:
        fetch(resource_id)  # type: ignore[operator]
    except Exception as error:
        status = getattr(error, "status", None)
        if status == 404 or "not found" in str(error).lower():
            return
        raise
    raise RuntimeError(f"KFP resource still exists after rollback: {resource_id}")


def delete_uploaded_resource(client: object, state: dict[str, str]) -> None:
    action = state.get("action", "")
    try:
        if action == "uploaded_pipeline_version":
            version_id = state.get("pipeline_version_id", "")
            if not version_id:
                raise RuntimeError("KFP rollback state is missing pipeline_version_id")
            client.delete_pipeline_version(version_id)  # type: ignore[attr-defined]
            _assert_deleted(client.get_pipeline_version, version_id)  # type: ignore[attr-defined]
            return
        if action == "uploaded_pipeline":
            pipeline_id = state.get("pipeline_id", "")
            if not pipeline_id:
                raise RuntimeError("KFP rollback state is missing pipeline_id")
            client.delete_pipeline(pipeline_id)  # type: ignore[attr-defined]
            _assert_deleted(client.get_pipeline, pipeline_id)  # type: ignore[attr-defined]
            return
        raise RuntimeError(f"unsupported KFP rollback action: {action}")
    except Exception as error:
        status = getattr(error, "status", None)
        if status == 404 or "not found" in str(error).lower():
            return
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete the KFP resource created by a deployment transaction.")
    parser.add_argument("--state-path", required=True)
    args = parser.parse_args()
    state_path = Path(args.state_path)
    if not state_path.exists():
        return 0
    state = json.loads(state_path.read_text(encoding="utf-8"))

    import kfp

    client = kfp.Client(host=state["host"])
    delete_uploaded_resource(client, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

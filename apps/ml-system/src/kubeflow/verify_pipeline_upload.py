from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def verify_uploaded_pipeline(client: Any, state: dict[str, str]) -> dict[str, str]:
    action = state.get("action", "")
    pipeline_id = state.get("pipeline_id", "")
    if action not in {"uploaded_pipeline", "uploaded_pipeline_version"}:
        raise RuntimeError(f"unsupported KFP upload action: {action}")
    if not pipeline_id:
        raise RuntimeError("KFP upload result is missing pipeline_id")
    version_id = state.get("pipeline_version_id", "")
    if action == "uploaded_pipeline_version" and not version_id:
        raise RuntimeError("KFP upload result is missing pipeline_version_id")

    client.get_pipeline(pipeline_id)
    result = {
        "action": action,
        "pipeline_id": pipeline_id,
        "status": "available",
    }
    if action == "uploaded_pipeline_version":
        client.get_pipeline_version(pipeline_id, version_id)
        result["pipeline_version_id"] = version_id
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify an uploaded KFP package without creating a pipeline run."
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--state-path", required=True)
    args = parser.parse_args()

    state = json.loads(Path(args.state_path).read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("KFP upload state must be a JSON object")

    import kfp

    result = verify_uploaded_pipeline(kfp.Client(host=args.host), state)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

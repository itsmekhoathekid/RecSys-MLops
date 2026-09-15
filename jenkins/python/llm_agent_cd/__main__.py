from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .driver import Driver
from .engine import Engine, TERMINAL
from .release import DEFAULT_POLICY, digest, release
from .state import StateStore
from jenkins.python.model_cd.storage import parse_s3_uri, s3_client


def load(path):
    if path.startswith("s3://"):
        bucket, key = parse_s3_uri(path)
        return json.loads(s3_client().get_object(Bucket=bucket, Key=key)["Body"].read())
    return json.loads(Path(path).read_text())


def main():
    if os.environ.get("AB_ENV_FILE"):
        settings = load(os.environ["AB_ENV_FILE"])
        allowed = {
            "AB_DATABASE_URL",
            "AB_INTERNAL_TOKEN",
            "AB_ROUTER_URL",
            "AB_NAMESPACE",
            "AB_SECRET_NAME",
            "AB_CASE_TICKET_KEY",
            "AB_EXTERNAL_A2A_URL",
            "MODEL_STORE_ENDPOINT",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_DEFAULT_REGION",
        }
        if set(settings) - allowed or not all(
            isinstance(v, str) for v in settings.values()
        ):
            raise ValueError("invalid credentials JSON fields")
        os.environ.update(settings)
    parser = argparse.ArgumentParser(
        description="Restartable LLM Agent CD; one tick per invocation"
    )
    parser.add_argument(
        "action",
        choices=[
            "run",
            "resume",
            "rollback",
            "monitor",
            "bootstrap",
            "activate",
            "status",
            "restore-baseline",
            "cleanup",
        ],
    )
    parser.add_argument("--candidate")
    parser.add_argument("--champion")
    parser.add_argument("--mode", choices=["config_only", "llm_only", "combined"])
    parser.add_argument("--experiment-id")
    parser.add_argument("--policy", default="configs/llm-ab/policy.json")
    parser.add_argument("--fixtures", default="configs/llm-ab/cases.json")
    parser.add_argument("--state-uri", default=os.environ.get("AB_STATE_URI"))
    args = parser.parse_args()
    store = StateStore(args.state_uri)
    if args.action == "bootstrap":
        if not args.champion:
            parser.error(
                "bootstrap requires --champion with a verified immutable manifest"
            )
        champion = release(load(args.champion))
        # Create-only: never replace an existing experiment/champion.
        store.write(
            {
                "phase": "IDLE",
                "champion": champion,
                "previous": None,
                "baseline": champion,
                "pending": champion,
                "disabled": [],
                "releases": {champion["release_id"]: champion},
                "events": [],
                "experiment_ids": [],
                "policy": DEFAULT_POLICY,
                "policy_checksum": digest(DEFAULT_POLICY),
            },
            None,
        )
        print(
            "State created. Install runtime, deploy bootstrap release, then verify its 100% baseline route before cutover."
        )
        return 0
    driver = Driver()
    if args.action == "cleanup":
        from .cleanup import TerminalCleanup

        report = TerminalCleanup(store, driver, time.time).run()
        output = Path(".llm-agent-cd")
        output.mkdir(exist_ok=True)
        (output / "cleanup.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
        state, _ = store.read()
        (output / "status.json").write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n"
        )
        print(json.dumps({"phase": state["phase"], "cleanup": report["status"]}))
        return 0
    engine = Engine(store, driver, time.time)
    if args.action == "activate":
        if engine.state["phase"] != "IDLE":
            parser.error("activate only supports the initial IDLE bootstrap")
        driver.deploy(engine.state["champion"])
        driver.verify_release(engine.state["champion"])
        revision = driver.route(engine.state, 0)
        deadline = time.monotonic() + 60
        while (
            not driver.verify_route(engine.state, 0, revision)
            and time.monotonic() < deadline
        ):
            time.sleep(2)  # Read-only propagation checks; no inference retry.
        if not driver.verify_route(engine.state, 0, revision):
            raise RuntimeError(
                "bootstrap route not yet verified; retry activate (no model invocation)"
            )
        engine.save(route_revision=revision, verified_weight=0, activated=True)
        driver.kube(
            "apply",
            "-f",
            "-",
            stdin=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": "recsys-workflow-activation" if driver.scope == "workflow" else "recsys-ab-activation",
                        "namespace": driver.namespace,
                    },
                    "data": {"enabled": "true"},
                }
            ),
        )
    elif args.action == "run":
        if not args.candidate or not args.mode or not args.experiment_id:
            parser.error("run requires --candidate, --mode and --experiment-id")
        engine.start(
            load(args.candidate),
            args.mode,
            load(args.policy),
            load(args.fixtures),
            args.experiment_id,
        )
        engine.save(build_url=os.environ.get("BUILD_URL", ""))
    elif args.action == "rollback":
        engine.rollback("operator requested rollback")
    elif args.action == "restore-baseline":
        engine.restore_baseline()
    elif (
        args.action == "monitor" and engine.state["phase"] not in {"MONITOR"} | TERMINAL
    ):
        parser.error("monitor requires a promoted experiment")
    if args.action in {"run", "resume", "monitor"}:
        try:
            engine.tick()
        except Exception as exc:
            # Refresh ownership before compensating. A concurrent writer must win.
            _, current_etag = store.read()
            if current_etag != engine.etag:
                raise RuntimeError(
                    "state ownership lost; no rollback mutation attempted"
                ) from exc
            engine.rollback("execution error: " + type(exc).__name__)
            # Keep ticking compensation until Envoy acknowledges it. The terminal
            # ROLLED_BACK/ROLLBACK_FAILED exit code then fails the Jenkins build.
    output = Path(".llm-agent-cd")
    output.mkdir(exist_ok=True)
    (output / "status.json").write_text(
        json.dumps(engine.state, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps({"phase": engine.state["phase"], "gate": engine.state.get("gate")})
    )
    return 1 if engine.state["phase"] in {"ROLLED_BACK", "ROLLBACK_FAILED"} else 0


if __name__ == "__main__":
    raise SystemExit(main())

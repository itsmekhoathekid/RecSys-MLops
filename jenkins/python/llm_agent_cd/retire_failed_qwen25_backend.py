"""Scale one exact unrouted Qwen2.5 backend to zero after two quarantines.

The immutable Deployment, Service, ModelConfigs, specialist agents, TaskStore
history and MinIO evidence remain. This reversible capacity action runs under
the production/state locks and cannot touch a protected LLM identity.
"""
import argparse
import json
import os
from pathlib import Path
import time

from .driver import Driver, command
from .provision import kube
from .release_guard import check
from .state import StateStore


REVIEW = "reviewed-retire-failed-qwen25-v2-backend-v1"
BASELINE = "2458bfb95a8e3310adeea29ab39fe2f5ebf963d4f48060356fdc8c8fb84385c6"
LLM_VERSION = "f13caa253669cf1b22e8741fce5f69fe4c6f116deb20d6a2e4adb7a514d59c7f"
FAILED_RELEASES = (
    "eca258c774d4222caa3745c223d03b0f6d27a2602832881a692509e54ac89bb4",
    "c6bf33cb156b4ff28cf322e7db87315b78e6f6d77f5f966eff20d737659b411c",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    if parser.parse_args().image != REVIEW:
        raise ValueError("unreviewed failed-backend capacity action")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")

    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = json.loads(kube(
        "kagent", "get", "deployment", "recsys-workflow-router", "-o", "json"))
    os.environ.update(
        AB_SCOPE="workflow",
        AB_SECRET_NAME="recsys-workflow-runtime",
        AB_ALLOW_LIVE_TEST="true",
        AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0]["image"],
    )
    check("workflow")
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    if (state.get("phase") != "IDLE" or not state.get("activated")
            or state.get("experiment_id")
            or state.get("champion", {}).get("release_id") != BASELINE):
        raise ValueError("exact activated IDLE baseline required")
    protected = [state.get(key) for key in ("champion", "previous", "baseline", "pending")]
    protected.extend(state.get("releases", {}).values())
    if any(value and value.get("llm_version_id") == LLM_VERSION for value in protected):
        raise ValueError("failed Qwen2.5 backend is referenced by workflow state")

    driver = Driver()
    if not driver.verify_route(state, 0, state["route_revision"]):
        raise ValueError("workflow route is not the verified baseline route")
    route = json.loads(kube(
        "kagent", "get", "virtualservice", "recsys-workflow-ab", "-o", "json"))
    if any(release_id in json.dumps(route["spec"]) for release_id in FAILED_RELEASES):
        raise ValueError("failed release remains in Istio route")

    with driver.db.connect() as connection:
        counts = {}
        for release_id in FAILED_RELEASES:
            counts[release_id] = {
                "sessions": connection.execute(
                    "SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s",
                    (release_id,),).fetchone()["n"],
                "invocations": connection.execute(
                    "SELECT count(*) n FROM recsys_ab.invocations WHERE release_id=%s",
                    (release_id,),).fetchone()["n"],
            }
        active = connection.execute(
            "SELECT count(*) n FROM recsys_ab.invocations WHERE finished_at IS NULL"
        ).fetchone()["n"]
        dispatches = connection.execute(
            "SELECT count(*) n FROM recsys_ab.trigger_requests "
            "WHERE status IN ('QUEUED','WAITING','DISPATCHING','SUBMITTED','RUNNING')"
        ).fetchone()["n"]
    if active or dispatches or any(any(value.values()) for value in counts.values()):
        raise ValueError("routing activity blocks failed-backend retirement")

    for release_id in FAILED_RELEASES:
        base = "rec-ab-" + release_id[:20]
        for name in (base, base + "-cpu"):
            adapter = json.loads(kube(
                "kagent", "get", "deployment", name, "-o", "json"))
            if (adapter["metadata"].get("labels", {}).get("recsys.ai/owner")
                    != "llm-agent-cd" or adapter["spec"].get("replicas") != 0):
                raise ValueError("failed release adapter is not quarantined: " + name)
        if kube("kagent", "get", "sandboxagent", base,
                "--ignore-not-found", "-o", "name").strip():
            raise ValueError("failed release Coordinator still exists: " + base)

    name = "rec-llm-" + LLM_VERSION[:20]
    backend = json.loads(kube("kagent", "get", "deployment", name, "-o", "json"))
    if (backend["metadata"].get("labels", {}).get("recsys.ai/owner")
            != "llm-agent-cd" or backend["spec"].get("replicas") != 1
            or backend.get("status", {}).get("readyReplicas") != 1):
        raise ValueError("exact failed Qwen2.5 backend is not Ready")
    journal = ("workflow/capacity-windows/retire-failed-qwen25-v2-"
               + os.environ["BUILD_NUMBER"] + ".json")
    store.client.put_object(
        Bucket=store.bucket,
        Key=journal,
        Body=json.dumps({
            "stage": "retire_failed_qwen25_backend_intent",
            "build_url": os.environ["BUILD_URL"],
            "state_etag": etag,
            "baseline_release_id": BASELINE,
            "llm_version_id": LLM_VERSION,
            "failed_releases": FAILED_RELEASES,
            "routing_counts": counts,
            "backend": backend,
        }, sort_keys=True).encode(),
        ContentType="application/json",
        IfNoneMatch="*",
    )
    kube("kagent", "patch", "deployment", name, "--type=json", "-p", json.dumps([
        {"op": "test", "path": "/metadata/resourceVersion",
         "value": backend["metadata"]["resourceVersion"]},
        {"op": "test", "path": "/spec/replicas", "value": 1},
        {"op": "replace", "path": "/spec/replicas", "value": 0},
    ]))
    selector = backend["spec"]["selector"]["matchLabels"]
    deadline = time.monotonic() + 180
    while True:
        pods = json.loads(command(
            "kubectl", "-n", "kagent", "get", "pods", "-o", "json"))["items"]
        admitted = [pod for pod in pods
                    if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
                    and all(pod["metadata"].get("labels", {}).get(key) == value
                            for key, value in selector.items())]
        if not admitted:
            break
        if time.monotonic() > deadline:
            raise ValueError("HOLD failed backend did not terminate; no force delete")
        time.sleep(3)
    after, after_etag = store.read()
    if (after != state or after_etag != etag
            or not driver.verify_route(after, 0, after["route_revision"])):
        raise ValueError("state/route changed during backend capacity action")
    report = {
        "stage": "retire_failed_qwen25_backend",
        "llm_version_id": LLM_VERSION,
        "deployment_scaled_to_zero": name,
        "immutable_resources_and_evidence_retained": True,
        "state_unchanged": True,
        "route_unchanged": True,
        "inference_requests": 0,
        "journal_key": journal,
    }
    target = Path(".llm-agent-cd/evaluation-preparation.json")
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()

"""Deploy the reviewed nullable Context tool contract through shared locks."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path

import yaml

from .capacity import verify_capacity
from .driver import Driver, command
from .provision import kube
from .release import digest
from .release_guard import check
from .state import StateStore


ONLINE_NAME = "recsys-online-feature-api"
ONLINE_NAMESPACE = "api-serving"
ONLINE_CHART = "infra/helm/recsys-online-feature-api"
ONLINE_IMAGE = (
    "asia-southeast1-docker.pkg.dev/recsys-mlops-506406/recsys/"
    "recsys-online-feature-api@sha256:"
    "306d4ffd3ba08b15a7953481c743199320553df9f6c6e521ab87377c7395aabc"
)
MCP_NAME = "recsys-feature-rag-mcp"
MCP_NAMESPACE = "kagent"
MCP_CHART = "infra/helm/recsys-feature-rag-mcp"
MCP_AUTH_VERSIONS = Path("configs/agentic/mcp-auth-versions.yaml")
MCP_IMAGE = (
    "asia-southeast1-docker.pkg.dev/recsys-mlops-506406/recsys/"
    "recsys-feature-rag-mcp@sha256:"
    "7fcafb87cee009a20589907a3d9164f7b8e5e8fd8a8ca6a9db1a644d72c32659"
)
CONTRACT_VERSION = "2"
CONTRACT_FILE_SHA256 = (
    "3cf0423cce68a4b5984980398b9e1f630f548c9d5064cf7ddf3576246ab14576"
)


def _objects(text: str) -> dict[tuple[str, str], dict]:
    return {
        (item["kind"], item["metadata"]["name"]): item
        for item in yaml.safe_load_all(text)
        if item
    }


def _online_image_only(before: str, after: str) -> None:
    old, new = _objects(before), _objects(after)
    if old.keys() != new.keys():
        raise ValueError("online feature resource inventory drift")
    deployment = old[("Deployment", ONLINE_NAME)]
    api = next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == "api"
    )
    api["image"] = ONLINE_IMAGE
    next(entry for entry in api["env"] if entry["name"] == "IMAGE_REFERENCE")[
        "value"
    ] = ONLINE_IMAGE
    if old != new:
        raise ValueError("online feature deploy changes more than the reviewed image")


def _mcp_contract_only(before: str, after: str, workload: str) -> None:
    old, new = _objects(before), _objects(after)
    if old.keys() != new.keys():
        raise ValueError("MCP resource inventory drift")
    deployment = old[("Deployment", workload)]
    desired_deployment = new[("Deployment", workload)]
    mcp = next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == "mcp"
    )
    mcp["image"] = MCP_IMAGE
    deployment["spec"]["template"]["metadata"]["annotations"]["checksum/config"] = (
        desired_deployment["spec"]["template"]["metadata"]["annotations"][
            "checksum/config"
        ]
    )
    config = old[("ConfigMap", workload)]["data"]
    config.update(
        RECSYS_IMAGE_REFERENCE=MCP_IMAGE,
        TOOL_CONTRACT_VERSION=CONTRACT_VERSION,
        TOOL_CONTRACT_SHA256=CONTRACT_FILE_SHA256,
    )
    if old != new:
        raise ValueError("MCP deploy changes more than image and contract attestation")


def _surge_reservation(nodes: list[dict], pods: list[dict], live: dict) -> dict:
    namespace = live["metadata"]["namespace"]
    selector = live["spec"]["selector"]["matchLabels"]
    selected = [
        pod
        for pod in pods
        if pod["metadata"].get("namespace") == namespace
        and pod.get("spec", {}).get("nodeName")
        and pod.get("status", {}).get("phase") == "Running"
        and all(
            pod["metadata"].get("labels", {}).get(key) == value
            for key, value in selector.items()
        )
    ]
    if live["spec"].get("replicas") != 1 or live["status"].get("readyReplicas") != 1:
        raise ValueError("deployment must be stable at one Ready replica")
    if len(selected) != 1:
        raise ValueError("unexpected admitted deployment pod inventory")
    spec = deepcopy(selected[0]["spec"])
    spec.pop("nodeName", None)
    spreads = spec.get("topologySpreadConstraints", [])
    if spreads and not all(
        spread.get("whenUnsatisfiable") == "ScheduleAnyway" for spread in spreads
    ):
        raise ValueError("hard topology spread requires explicit capacity review")
    spec.pop("topologySpreadConstraints", None)
    reservation = {
        "metadata": {
            "namespace": namespace,
            "name": live["metadata"]["name"] + "-surge",
        },
        "spec": {"replicas": 1, "template": {"spec": spec}},
    }
    verify_capacity(
        nodes,
        pods,
        [reservation],
        {},
        headroom={"cpu": "200m", "memory": "128Mi"},
    )
    return reservation


def _verify_online_semantics() -> None:
    code = """import json,urllib.request
body=json.dumps({'user_id':218,'candidate_item_ids':[],'top_k':2}).encode()
request=urllib.request.Request('http://127.0.0.1:8080/online-features',data=body,headers={'Content-Type':'application/json'},method='POST')
with urllib.request.urlopen(request,timeout=20) as response: value=json.load(response)
assert value['user_id']==218 and value['candidate_item_ids']==[] and value['item_features']=={}
"""
    kube(
        ONLINE_NAMESPACE,
        "exec",
        "deployment/" + ONLINE_NAME,
        "-c",
        "api",
        "--",
        "python",
        "-c",
        code,
    )


def _verify_mcp_contract(workload: str) -> None:
    code = f"""import json,os,urllib.request
version=json.load(urllib.request.urlopen('http://127.0.0.1:8080/version',timeout=10))
assert version['image_reference']=={MCP_IMAGE!r}
assert version['tool_contract_version']=={CONTRACT_VERSION!r}
assert version['tool_contract_sha256']=={CONTRACT_FILE_SHA256!r}
headers={{'Authorization':'Bearer '+os.environ['MCP_AUTH_TOKEN'],'Accept':'application/json, text/event-stream','Content-Type':'application/json','MCP-Protocol-Version':'2025-06-18'}}
request=urllib.request.Request('http://127.0.0.1:8080/mcp',data=json.dumps({{'jsonrpc':'2.0','id':1,'method':'tools/list','params':{{}}}}).encode(),headers=headers,method='POST')
with urllib.request.urlopen(request,timeout=20) as response: payload=json.load(response)
tools={{item['name']:item for item in payload['result']['tools']}}
schema=tools['get_user_online_features']['inputSchema']
assert schema['required']==['user_id','candidate_item_ids','top_k']
field=schema['properties']['candidate_item_ids']
assert field['anyOf']==[{{'items':{{'type':'integer'}},'maxItems':100,'type':'array'}},{{'type':'null'}}]
assert 'null means resolve candidates' in field['description']
"""
    kube(
        MCP_NAMESPACE,
        "exec",
        "deployment/" + workload,
        "-c",
        "mcp",
        "--",
        "python",
        "-c",
        code,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if args.image != MCP_IMAGE:
        raise ValueError("exact reviewed MCP image digest required")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")
    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router = json.loads(
        kube("kagent", "get", "deployment", "recsys-workflow-router", "-o", "json")
    )
    os.environ.update(
        AB_SCOPE="workflow",
        AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0][
            "image"
        ],
    )
    check()
    store = StateStore("s3://recsys-llm-ab/workflow/state.json")
    state, etag = store.read()
    driver = Driver()
    if state.get("phase") not in {"IDLE", "ROLLED_BACK", "COMPLETED"}:
        raise ValueError("workflow experiment is not terminal")
    if not driver.verify_route(
        state, state["verified_weight"], state["route_revision"]
    ):
        raise ValueError("current workflow route is not verified")

    nodes = json.loads(command("kubectl", "get", "nodes", "-o", "json"))["items"]
    pods = json.loads(command("kubectl", "get", "pods", "-A", "-o", "json"))["items"]
    rotation = json.loads(MCP_AUTH_VERSIONS.read_text(encoding="utf-8"))
    feature_rotation = rotation["services"]["featureRag"]
    deployed_revisions = [
        revision
        for revision in feature_rotation["revisions"].values()
        if revision["deploy"]
    ]
    if len(deployed_revisions) != 1:
        raise ValueError(
            "context-contract deploy requires retirement to leave exactly one MCP slot"
        )
    mcp_workload = deployed_revisions[0]["workloadName"]
    if mcp_workload != feature_rotation["revisions"][
        feature_rotation["activeRevision"]
    ]["workloadName"]:
        raise ValueError("the sole deployed MCP slot must be active")
    live_online = json.loads(kube(ONLINE_NAMESPACE, "get", "deployment", ONLINE_NAME, "-o", "json"))
    live_mcp = json.loads(kube(MCP_NAMESPACE, "get", "deployment", mcp_workload, "-o", "json"))
    reservations = [
        _surge_reservation(nodes, pods, live_online),
        _surge_reservation(nodes, pods, live_mcp),
    ]

    online_values = json.loads(
        command("helm", "get", "values", ONLINE_NAME, "-n", ONLINE_NAMESPACE, "-a", "-o", "json")
    )
    online_before = command("helm", "get", "manifest", ONLINE_NAME, "-n", ONLINE_NAMESPACE)
    online_values["image"] = ONLINE_IMAGE
    online_after = command(
        "helm", "template", ONLINE_NAME, ONLINE_CHART, "-n", ONLINE_NAMESPACE,
        "--is-upgrade", "--dry-run=server", "-f", "-", stdin=json.dumps(online_values),
    )
    _online_image_only(online_before, online_after)

    mcp_values = json.loads(
        command("helm", "get", "values", MCP_NAME, "-n", MCP_NAMESPACE, "-a", "-o", "json")
    )
    mcp_before = command("helm", "get", "manifest", MCP_NAME, "-n", MCP_NAMESPACE)
    mcp_values["image"] = MCP_IMAGE
    mcp_values["version"] = rotation["version"]
    mcp_values["services"] = rotation["services"]
    mcp_values.setdefault("config", {}).update(
        toolContractVersion=CONTRACT_VERSION,
        toolContractSha256=CONTRACT_FILE_SHA256,
    )
    mcp_after = command(
        "helm", "template", MCP_NAME, MCP_CHART, "-n", MCP_NAMESPACE,
        "--is-upgrade", "--dry-run=server", "-f", "-", "-f",
        str(MCP_AUTH_VERSIONS), stdin=json.dumps(mcp_values),
    )
    _mcp_contract_only(mcp_before, mcp_after, mcp_workload)

    previous = {
        name: json.loads(command("helm", "status", name, "-n", namespace, "-o", "json"))["version"]
        for name, namespace in (
            (ONLINE_NAME, ONLINE_NAMESPACE),
            (MCP_NAME, MCP_NAMESPACE),
        )
    }
    journal = {
        "stage": "context_contract_v2_intent",
        "build_url": os.environ["BUILD_URL"],
        "state_etag": etag,
        "online_image": ONLINE_IMAGE,
        "mcp_image": MCP_IMAGE,
        "contract_version": CONTRACT_VERSION,
        "contract_file_sha256": CONTRACT_FILE_SHA256,
        "previous_helm_revisions": previous,
        "surge_reservations": [item["metadata"] for item in reservations],
    }
    journal_key = "workflow/deployments/context-contract-v2-" + digest(journal) + ".json"
    store.client.put_object(
        Bucket=store.bucket,
        Key=journal_key,
        Body=json.dumps(journal, sort_keys=True).encode(),
        ContentType="application/json",
        IfNoneMatch="*",
    )

    command(
        "helm", "upgrade", ONLINE_NAME, ONLINE_CHART, "-n", ONLINE_NAMESPACE,
        "--reuse-values", "--set-string", "image=" + ONLINE_IMAGE,
        "--atomic", "--wait", "--timeout", "8m",
    )
    kube(ONLINE_NAMESPACE, "rollout", "status", "deployment/" + ONLINE_NAME, "--timeout=180s")
    _verify_online_semantics()
    command(
        "helm", "upgrade", MCP_NAME, MCP_CHART, "-n", MCP_NAMESPACE,
        "--reuse-values", "-f", str(MCP_AUTH_VERSIONS),
        "--set-string", "image=" + MCP_IMAGE,
        "--set-string", "config.toolContractVersion=" + CONTRACT_VERSION,
        "--set-string", "config.toolContractSha256=" + CONTRACT_FILE_SHA256,
        "--atomic", "--wait", "--timeout", "8m",
    )
    kube(MCP_NAMESPACE, "rollout", "status", "deployment/" + mcp_workload, "--timeout=180s")
    _verify_mcp_contract(mcp_workload)

    after, after_etag = store.read()
    if after != state or after_etag != etag:
        raise ValueError("workflow state changed during dependency deployment")
    if not driver.verify_route(
        after, after["verified_weight"], after["route_revision"]
    ):
        raise ValueError("workflow route changed during dependency deployment")
    report = {
        **journal,
        "stage": "context_contract_v2_deployed",
        "journal_key": journal_key,
        "state_unchanged": True,
        "route_unchanged": True,
        "online_explicit_empty_verified": True,
        "mcp_schema_round_trip_verified": True,
        "inference_requests": 0,
    }
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()

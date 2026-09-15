"""Retain the previous v4 workflow on E2 and release its CPU duplicate.

The v6 baseline cutover left the immediately previous workflow available on
both node pools.  One Ready endpoint is sufficient for its pinned sessions;
the duplicate CPU placement is released only after state, route, session and
unfinished-invocation checks pass under the common Jenkins locks.  The legacy
Recommendation-only router is retained at one Ready replica because workflow
traffic uses ``recsys-workflow-router``; this releases E2 CPU without removing
the recovery endpoint for old Recommendation sessions.
"""
import argparse
import json
import os
from pathlib import Path
import time

from .capacity import verify_capacity, workflow_job_reservations
from .capacity_window import native_reservations
from .driver import command, Driver
from .provision import kube
from .release_guard import check
from .state import StateStore

REVIEW = "reviewed-previous-v4-and-legacy-router-v3"
CHAMPION = "a9aa3fb196d3410894dbf369804d5831f2f71133409e3ad585087b87ff8e7c04"
PREVIOUS = "a1d9a9e1edaac299008dcae1a8e7b9ef4ee5a303cbecdab9f864afebfac914a5"
TARGET = "rec-ab-a1d9a9e1edaac299008d-cpu"
PEER = "rec-ab-a1d9a9e1edaac299008d"
LEGACY_ROUTER = "recsys-ab-router"


def main():
    p=argparse.ArgumentParser(); p.add_argument("--image", required=True)
    if p.parse_args().image != REVIEW: raise ValueError("unreviewed previous placement action")
    if not os.environ.get("BUILD_URL") or not os.environ.get("JENKINS_URL"):
        raise ValueError("common-lock Jenkins job required")
    os.environ.update(json.loads(Path(os.environ["AB_ENV_FILE"]).read_text()))
    router=json.loads(kube("kagent","get","deployment","recsys-workflow-router","-o","json"))
    os.environ.update(AB_SCOPE="workflow",AB_ROUTER_IMAGE=router["spec"]["template"]["spec"]["containers"][0]["image"])
    check(); store=StateStore("s3://recsys-llm-ab/workflow/state.json"); state,etag=store.read(); driver=Driver()
    if (state.get("phase")!="IDLE" or state.get("champion",{}).get("release_id")!=CHAMPION
            or state.get("previous",{}).get("release_id")!=PREVIOUS or state.get("experiment_id")):
        raise ValueError("exact idle champion/previous state required")
    if not driver.verify_route(state,0,state["route_revision"]): raise ValueError("baseline route not verified")
    def deployment(name,pool):
        obj=json.loads(kube("kagent","get","deployment",name,"-o","json"))
        env=[e for c in obj["spec"]["template"]["spec"]["containers"] for e in c.get("env",[]) if e["name"]=="RELEASE_ID"]
        if (obj["metadata"].get("labels",{}).get("recsys.ai/owner")!="llm-agent-cd"
                or obj["spec"].get("replicas") not in {0,1}
                or obj["spec"]["template"]["spec"].get("nodeSelector")!={"recsys.ai/pool":pool}
                or env!=[{"name":"RELEASE_ID","value":PREVIOUS}]):
            raise ValueError("previous adapter identity drift: "+name)
        return obj
    target=deployment(TARGET,"cpu-services"); peer=deployment(PEER,"ml-system")
    if peer["spec"]["replicas"]!=1 or peer["status"].get("readyReplicas")!=1:
        raise ValueError("retained previous E2 adapter not Ready")
    legacy=json.loads(kube("kagent","get","deployment",LEGACY_ROUTER,"-o","json"))
    if (legacy["metadata"].get("labels",{}).get("app.kubernetes.io/managed-by")!="Helm"
            or legacy["spec"].get("replicas") not in {1,2}
            or legacy["status"].get("readyReplicas")!=legacy["spec"].get("replicas")
            or legacy["spec"]["selector"].get("matchLabels")!={"app":LEGACY_ROUTER}):
        raise ValueError("legacy Recommendation router identity/readiness drift")
    with driver.db.connect() as c:
        sessions=c.execute("SELECT count(*) n FROM recsys_ab.sessions WHERE release_id=%s",(PREVIOUS,)).fetchone()["n"]
        unfinished=c.execute("SELECT count(*) n FROM recsys_ab.invocations WHERE release_id=%s AND finished_at IS NULL",(PREVIOUS,)).fetchone()["n"]
    if unfinished: raise ValueError("previous release has unfinished invocation")
    nodes=json.loads(command("kubectl","get","nodes","-o","json"))["items"]
    pods=json.loads(command("kubectl","get","pods","-A","-o","json"))["items"]
    selector=target["spec"]["selector"]["matchLabels"]
    def target_pod(pod):
        return pod["metadata"]["namespace"]=="kagent" and pod["status"]["phase"] not in {"Succeeded","Failed"} and all(
            pod["metadata"].get("labels",{}).get(k)==v for k,v in selector.items())
    legacy_selector=legacy["spec"]["selector"]["matchLabels"]
    legacy_pods=sorted((pod for pod in pods if pod["metadata"]["namespace"]=="kagent"
        and pod["status"]["phase"] not in {"Succeeded","Failed"}
        and all(pod["metadata"].get("labels",{}).get(k)==v for k,v in legacy_selector.items())),
        key=lambda pod:pod["metadata"]["uid"])
    if len(legacy_pods)!=legacy["spec"]["replicas"]:
        raise ValueError("legacy Recommendation router admitted pod count drift")
    removed_legacy_uid=legacy_pods[-1]["metadata"]["uid"] if len(legacy_pods)==2 else None
    planned=[pod for pod in pods if not target_pod(pod)
        and pod["metadata"].get("uid")!=removed_legacy_uid]
    reserve=workflow_job_reservations(planned,probe=True)+native_reservations(include_backend=False)
    projected=verify_capacity(nodes,planned,reserve,{},headroom={"cpu":"200m","memory":"128Mi"})
    key="workflow/capacity-windows/previous-v4-single-placement-"+os.environ["BUILD_NUMBER"]+".json"
    store.client.put_object(Bucket=store.bucket,Key=key,Body=json.dumps({"stage":"previous_single_placement",
        "build_url":os.environ["BUILD_URL"],"state_etag":etag,"target":target,"retained_peer":peer,
        "legacy_recommendation_router":legacy,
        "sessions_retained":sessions,"projected_headroom":{n:{k:str(v) for k,v in x.items()} for n,x in projected.items()}}).encode(),
        ContentType="application/json",IfNoneMatch="*")
    if target["spec"]["replicas"]:
        kube("kagent","patch","deployment",TARGET,"--type=json","-p",json.dumps([
            {"op":"test","path":"/metadata/resourceVersion","value":target["metadata"]["resourceVersion"]},
            {"op":"test","path":"/spec/replicas","value":1},
            {"op":"replace","path":"/spec/replicas","value":0}]))
    if legacy["spec"]["replicas"]==2:
        kube("kagent","patch","deployment",LEGACY_ROUTER,"--type=json","-p",json.dumps([
            {"op":"test","path":"/metadata/resourceVersion","value":legacy["metadata"]["resourceVersion"]},
            {"op":"test","path":"/spec/replicas","value":2},
            {"op":"replace","path":"/spec/replicas","value":1}]))
    deadline=time.monotonic()+180
    while True:
        observed=json.loads(command("kubectl","get","pods","-A","-o","json"))["items"]
        active_legacy=[pod for pod in observed if pod["metadata"]["namespace"]=="kagent"
            and pod["status"]["phase"] not in {"Succeeded","Failed"}
            and all(pod["metadata"].get("labels",{}).get(k)==v for k,v in legacy_selector.items())]
        if not any(target_pod(pod) for pod in observed) and len(active_legacy)==1:
            break
        if time.monotonic()>deadline: raise ValueError("HOLD graceful previous adapter scale-down; no force delete")
        time.sleep(3)
    current=json.loads(command("kubectl","get","pods","-A","-o","json"))["items"]
    final=verify_capacity(nodes,current,workflow_job_reservations(current,probe=True)+native_reservations(include_backend=False),{},
        headroom={"cpu":"200m","memory":"128Mi"})
    peer=deployment(PEER,"ml-system")
    slices=json.loads(kube("kagent","get","endpointslice","-l","kubernetes.io/service-name="+PEER,"-o","json"))["items"]
    if peer["status"].get("readyReplicas")!=1 or not any(e.get("conditions",{}).get("ready") is True for x in slices for e in x.get("endpoints",[])):
        raise ValueError("retained previous endpoint lost readiness")
    legacy=json.loads(kube("kagent","get","deployment",LEGACY_ROUTER,"-o","json"))
    legacy_slices=json.loads(kube("kagent","get","endpointslice","-l","kubernetes.io/service-name="+LEGACY_ROUTER,"-o","json"))["items"]
    if (legacy["spec"].get("replicas")!=1 or legacy["status"].get("readyReplicas")!=1
            or not any(e.get("conditions",{}).get("ready") is True
                for x in legacy_slices for e in x.get("endpoints",[]))):
        raise ValueError("legacy Recommendation router lost its retained endpoint")
    after,after_etag=store.read()
    if after!=state or after_etag!=etag or not driver.verify_route(after,0,after["route_revision"]):
        raise ValueError("route/state preservation failure")
    report={"stage":"previous_single_placement","snapshot_key":key,"target_replicas":0,"retained_peer_replicas":1,
        "sessions_retained":sessions,"legacy_recommendation_router_replicas":1,
        "state_unchanged":True,"route_unchanged":True,"full_experiment_capacity_verified":True,
        "projected_headroom":{n:{k:str(v) for k,v in x.items()} for n,x in final.items()}}
    Path(".llm-agent-cd").mkdir(exist_ok=True)
    Path(".llm-agent-cd/evaluation-preparation.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report))

if __name__=="__main__": main()

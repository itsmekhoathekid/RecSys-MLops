"""Conservative scheduling preflight; requests, not observed utilization.

This is not a scheduler reservation. Readiness remains mandatory after deploy.
Unsupported required affinity fails closed instead of promising capacity.
"""
from decimal import Decimal
import re


def quantity(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([a-zA-Z]*)", str(value))
    if not match:
        raise ValueError("unsupported Kubernetes quantity")
    suffix = match[2]
    scales = {"": 1, "m": Decimal(".001"), "u": Decimal(".000001"), "n": Decimal(".000000001"),
              **{k: 1024 ** i for i, k in enumerate(["Ki", "Mi", "Gi", "Ti", "Pi", "Ei"], 1)},
              **{k: 1000 ** i for i, k in enumerate(["k", "M", "G", "T", "P", "E"], 1)}}
    if suffix not in scales:
        raise ValueError("unsupported Kubernetes quantity suffix")
    return Decimal(match[1]) * scales[suffix]


def requests(spec):
    def req(container, key):
        return quantity(container.get("resources", {}).get("requests", {}).get(key, "0"))
    result = {}
    for key in ("cpu", "memory", "ephemeral-storage"):
        regular = sum(req(c, key) for c in spec.get("containers", []))
        sidecars, peak = Decimal(0), Decimal(0)
        for init in spec.get("initContainers", []):
            if init.get("restartPolicy") == "Always":
                sidecars += req(init, key)
                peak = max(peak, sidecars)
            else:
                peak = max(peak, sidecars + req(init, key))
        result[key] = max(regular + sidecars, peak,
                          quantity(spec.get("resources", {}).get("requests", {}).get(key, "0")))
        result[key] += quantity(spec.get("overhead", {}).get(key, "0"))
    result["pods"] = Decimal(1)
    return result


def eligible(spec, node):
    if node.get("spec", {}).get("unschedulable"):
        return False
    if not any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"].get("conditions", [])):
        return False
    if any(c["type"] in {"MemoryPressure", "DiskPressure", "PIDPressure"} and c["status"] == "True" for c in node["status"].get("conditions", [])):
        return False
    if any(node["metadata"].get("labels", {}).get(k) != v for k, v in spec.get("nodeSelector", {}).items()):
        return False
    for taint in node.get("spec", {}).get("taints", []):
        if taint["effect"] not in {"NoSchedule", "NoExecute"}:
            continue
        if not any((not t.get("effect") or t["effect"] == taint["effect"]) and
                   ((t.get("operator") == "Exists" and (not t.get("key") or t["key"] == taint["key"])) or
                    (t.get("operator", "Equal") == "Equal" and t.get("key") == taint["key"] and t.get("value", "") == taint.get("value", "")))
                   for t in spec.get("tolerations", [])):
            return False
    return True


def workflow_job_reservations(pods, worker=True, probe=False):
    """Reserve finite slots without charging already admitted Jobs twice.

    The pipeline waits for dispatch completion before offline, and offline
    completion before live load. These share one execution slot, while the
    evaluator and dispatch recovery each have their own Forbid CronJob slot.
    """
    slots=[('evaluation',('recsys-workflow-evaluation-',)),
           ('recovery',('recsys-workflow-dispatch-recovery-',))]
    if worker: slots.append(('execution',('ab-offline-','ab-load-','ab-dispatch-')))
    if probe: slots.append(('telemetry-probe',('recsys-llm-observability-probe-',)))
    output=[]
    for name,prefixes in slots:
        namespace='observability' if name=='telemetry-probe' else 'kagent'
        active=[p for p in pods if p.get('metadata',{}).get('namespace')==namespace
            and p.get('spec',{}).get('nodeName') and p.get('status',{}).get('phase') not in {'Succeeded','Failed'}
            and any(o.get('kind')=='Job' and o.get('name','').startswith(prefixes)
                for o in p.get('metadata',{}).get('ownerReferences',[]))]
        if len(active)>1: raise ValueError('finite '+name+' slot overlap; wait for termination')
        if active: continue
        resources={'cpu':'110m','memory':'160Mi'} if name=='telemetry-probe' else {'cpu':'50m','memory':'128Mi'}
        output.append({'metadata':{'namespace':namespace,'name':'reserved-'+name},'spec':{'replicas':1,
            'template':{'spec':{'nodeSelector':{'recsys.ai/workload':'ml-system'},
            'tolerations':[{'key':'recsys.ai/workload','operator':'Equal','value':'ml-system','effect':'NoSchedule'}],
            'containers':[{'resources':{'requests':resources}}]}}}})
    return output


def verify_capacity(nodes, pods, deployments, existing_deployments, *, headroom=None):
    free = {n["metadata"]["name"]: {k: quantity(n["status"]["allocatable"].get(k, "0"))
                                  for k in ("cpu", "memory", "ephemeral-storage", "pods")} for n in nodes}
    for pod in pods:
        node = pod.get("spec", {}).get("nodeName")
        if node not in free or pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        for key, value in requests(pod["spec"]).items():
            free[node][key] -= value
    pending = []
    for deploy in deployments:
        identity = (deploy["metadata"]["namespace"], deploy["metadata"]["name"])
        # Existing deployments still undergo rollout/readiness verification.
        if identity in existing_deployments and not isinstance(existing_deployments, dict):
            continue
        spec = deploy["spec"]["template"]["spec"]
        if spec.get("affinity") or spec.get("nodeName"):
            raise ValueError("capacity preflight requires explicit placement review")
        for spread in spec.get('topologySpreadConstraints', []):
            # With Honor + one selector-eligible topology domain and minDomains
            # 1, adding a pod cannot violate maxSkew: that domain is its own
            # global minimum. Multi-domain constraints still need full review.
            domains={n['metadata'].get('labels',{}).get(spread.get('topologyKey')) for n in nodes
                if all(n['metadata'].get('labels',{}).get(k)==v for k,v in spec.get('nodeSelector',{}).items())}
            if (spread.get('nodeAffinityPolicy','Honor')!='Honor'
                    or spread.get('minDomains',1)!=1 or len(domains)!=1 or None in domains
                    or spread.get('maxSkew',0)<1):
                raise ValueError('capacity preflight requires explicit topology placement review')
        replicas = deploy['spec'].get('replicas',1)
        if identity in existing_deployments:
            selector = deploy['spec']['selector']['matchLabels']
            allocated = sum(p.get('metadata',{}).get('namespace')==identity[0]
                and p.get('spec',{}).get('nodeName') in free
                and p.get('status',{}).get('phase') not in {'Succeeded','Failed'}
                and all(p.get('metadata',{}).get('labels',{}).get(k)==v for k,v in selector.items()) for p in pods)
            replicas = max(0,replicas-allocated)
        pending.extend((identity[1], spec) for _ in range(replicas))
    pending.sort(key=lambda item: (requests(item[1])["cpu"], requests(item[1])["memory"]), reverse=True)
    for name, spec in pending:
        demand = requests(spec)
        chosen = next((n for n in nodes if eligible(spec, n) and all(free[n["metadata"]["name"]][k] >= v for k, v in demand.items())), None)
        if chosen is None:
            raise ValueError("insufficient schedulable capacity for " + name + "; no candidate deployed")
        for key, value in demand.items():
            free[chosen["metadata"]["name"]][key] -= value
    for node, remaining in free.items():
        for key, value in (headroom or {}).items():
            required = quantity(value)
            if remaining[key] < required:
                # Quantities and node names are operational evidence, not
                # credentials. Include the exact failed dimension so an
                # operator can release only the capacity that is actually
                # needed without weakening the approved safety margin.
                raise ValueError(
                    'capacity headroom below approved safety margin: '
                    f'node={node} resource={key} remaining={remaining[key]} '
                    f'required={required}'
                )
    return free

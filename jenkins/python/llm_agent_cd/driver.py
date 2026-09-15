from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import time
from copy import deepcopy

import httpx

from apps.agentic.llm_ab_router.database import Database

from .manifests import backend_resources, name, resources, virtual_service


def verify_tool_template_props(props, serving):
    expected = serving.get('chatTemplateSha256')
    if not expected:
        return
    selected = props.get('chat_template_tool_use') or props.get('chat_template')
    # llama.cpp removes one POSIX trailing newline when it loads
    # --chat-template-file and exposes the effective value via /props. Accept
    # only that exact normalization; arbitrary template changes still fail.
    actual = set() if not selected else {
        hashlib.sha256(selected.encode()).hexdigest(),
        hashlib.sha256((selected + '\n').encode()).hexdigest(),
    }
    if expected not in actual:
        raise ValueError('serving tool-use template attestation mismatch')
    caps = props.get('chat_template_caps') or {}
    if caps.get('supports_tools') is not True or caps.get('supports_tool_calls') is not True:
        raise ValueError('serving template lacks native tool-call capabilities')


def wait_ready_get(url, timeout_seconds=120):
    """Retry only an idempotent readiness GET while Service endpoints converge."""
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=10)
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(1)
    raise TimeoutError("readiness endpoint did not converge") from last_error


def command(*args, stdin=None):
    result = subprocess.run(
        args, input=stdin, capture_output=True, text=True, check=False, timeout=660
    )
    if result.returncode:
        # Kubernetes may echo manifests containing references. Never print env or Secret contents.
        raise RuntimeError(f"{args[0]} {args[1]} failed: {result.stderr[-1500:]}")
    return result.stdout


def contains_spec(live, desired):
    """Compare explicitly managed fields while tolerating API-defaulted fields."""
    if isinstance(desired, dict):
        return isinstance(live, dict) and all(
            k in live and contains_spec(live[k], v) for k, v in desired.items()
        )
    if isinstance(desired, list):
        return (
            isinstance(live, list)
            and len(live) == len(desired)
            and all(contains_spec(a, b) for a, b in zip(live, desired))
        )
    return live == desired


def normalized_live_spec(kind, spec):
    spec = deepcopy(spec)
    if kind == "SandboxAgent":
        # Pinned kagent serializes these zero values with omitempty. They remain
        # semantically false/empty and must not make an immutable release look
        # drifted after the API server round trip.
        spec.setdefault("description", "")
        if "declarative" in spec:
            spec["declarative"].setdefault("stream", False)
    return spec


def operator_paused_deployment(desired, live):
    """Recognize the sole mutable field written by the reviewed capacity job."""
    return (
        desired.get("kind") == "Deployment"
        and desired.get("metadata", {}).get("name", "").startswith(("rec-llm-", "rec-ab-"))
        and live.get("metadata", {}).get("labels", {}).get("recsys.ai/owner") == "llm-agent-cd"
        and live.get("spec", {}).get("replicas") == 0
        and desired.get("spec", {}).get("replicas") == 1
    )


def operator_single_replica_deployment(desired, live):
    """Accept a reviewed 2->1 capacity override without rewriting identity."""
    if not (
        desired.get("kind") == "Deployment"
        and desired.get("metadata", {}).get("name", "").startswith(("rec-llm-", "rec-ab-"))
        and live.get("metadata", {}).get("labels", {}).get("recsys.ai/owner") == "llm-agent-cd"
        and live.get("spec", {}).get("replicas") == 1
        and desired.get("spec", {}).get("replicas") == 2
        and live.get("metadata", {}).get("annotations", {}).get("recsys.ai/immutable-spec")
        == desired.get("metadata", {}).get("annotations", {}).get("recsys.ai/immutable-spec")
    ):
        return False
    comparable = deepcopy(live.get("spec", {}))
    comparable["replicas"] = 2
    return contains_spec(
        normalized_live_spec("Deployment", comparable), desired["spec"]
    )


def envoy_allocation_verified(dump, desired):
    """Check active RDS destinations/weights, not merely a revision in a name."""
    allocation = next(r for r in desired["spec"]["http"] if r["name"].startswith("allocate-"))
    expected = {f'outbound|{r["destination"]["port"]["number"]}||{r["destination"]["host"]}': r["weight"]
                for r in allocation["route"]}
    matches = []
    for block in dump.get("configs", []):
        for entry in block.get("dynamic_route_configs", block.get("dynamicRouteConfigs", [])):
            config = entry.get("route_config", entry.get("routeConfig", {}))
            for host in config.get("virtual_hosts", config.get("virtualHosts", [])):
                for route in host.get("routes", []):
                    # Istio may suffix the matched route with its match index.
                    if re.fullmatch(re.escape(allocation["name"]) + r"(?:\.\d+)?", route.get("name", "")):
                        matches.append(route)
    if not matches:
        return False
    for route in matches:
        if route.get("match", {}).get("path") != "/allocate":
            return False
        action = route.get("route", {})
        if action.get("request_mirror_policies", action.get("requestMirrorPolicies")):
            return False
        retry = action.get("retry_policy", action.get("retryPolicy", {}))
        if retry and retry.get("num_retries", retry.get("numRetries", 1)) != 0:
            return False
        if "cluster" in action:
            actual = {action["cluster"]: 100}
        else:
            weighted = action.get("weighted_clusters", action.get("weightedClusters", {}))
            clusters = weighted.get("clusters", [])
            actual = {c.get("name"): c.get("weight") for c in clusters}
            if len(actual) != len(clusters) or weighted.get("total_weight", weighted.get("totalWeight", 100)) != 100:
                return False
        if actual != expected:
            return False
    return True


class Driver:
    def live_load(self, state):
        if state['policy'].get('sample_source')!='live_test':
            return
        from .live_test import job
        from apps.agentic.llm_ab_router.trigger import dispatch_job_matches
        desired=job(state['experiment_id'],self.image,self.secret,self.url)
        raw=self.kube('get','job',desired['metadata']['name'],'--ignore-not-found','-o','json')
        if not raw:
            # A Job is dispatched only once per experiment. TTL expiry or an
            # operator deletion is not permission for another 360 requests.
            if state.get('live_load_dispatched'):
                raise ValueError('live load Job missing after dispatch; no replay')
            self.kube('create','-f','-',stdin=json.dumps(desired))
        elif not dispatch_job_matches(json.loads(raw),desired):
            raise ValueError('live load Job identity conflict')

    def source_inflight(self, state, source):
        return self.db.source_inflight(state["experiment_id"], source)

    def retire_terminal_sessions(self, disabled_release_ids=()):
        return self.db.retire_terminal_sessions(disabled_release_ids)

    def _scale_owned_deployment_to_zero(self, deployment, *, release_id=None):
        raw = self.kube(
            "get", "deployment", deployment, "--ignore-not-found", "-o", "json"
        )
        if not raw:
            return "ABSENT"
        value = json.loads(raw)
        if value["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd":
            raise ValueError("refusing to scale foreign deployment: " + deployment)
        if release_id is not None:
            identities = [
                entry.get("value")
                for container in value["spec"]["template"]["spec"].get("containers", [])
                for entry in container.get("env", [])
                if entry.get("name") == "RELEASE_ID"
            ]
            if identities != [release_id]:
                raise ValueError("adapter release identity drift: " + deployment)
        replicas = value["spec"].get("replicas", 1)
        if replicas < 0 or replicas > 2:
            raise ValueError("unexpected deployment replica count: " + deployment)
        if replicas:
            self.kube(
                "patch",
                "deployment",
                deployment,
                "--type=json",
                "--patch-file=/dev/stdin",
                stdin=json.dumps(
                    [
                        {
                            "op": "test",
                            "path": "/metadata/resourceVersion",
                            "value": value["metadata"]["resourceVersion"],
                        },
                        {"op": "test", "path": "/spec/replicas", "value": replicas},
                        {"op": "replace", "path": "/spec/replicas", "value": 0},
                    ]
                ),
            )
        deadline = time.monotonic() + 180
        while True:
            current = json.loads(
                self.kube("get", "deployment", deployment, "-o", "json")
            )
            status = current.get("status", {})
            if (
                current["spec"].get("replicas") == 0
                and status.get("replicas", 0) == 0
                and status.get("readyReplicas", 0) == 0
            ):
                return "SCALED_TO_ZERO" if replicas else "ALREADY_ZERO"
            if time.monotonic() >= deadline:
                raise ValueError("deployment did not scale to zero: " + deployment)
            time.sleep(2)

    def _backend_has_live_consumer(self, backend_name):
        model_configs = json.loads(
            self.kube("get", "modelconfigs", "-o", "json")
        ).get("items", [])
        deployment_items = json.loads(
            self.kube("get", "deployments", "-o", "json")
        ).get("items", [])
        deployments = {
            row["metadata"]["name"]: row for row in deployment_items
        }
        expected_host = "http://" + backend_name + "." + self.namespace + ".svc.cluster.local:8000/v1"
        for config in model_configs:
            if config.get("spec", {}).get("openAI", {}).get("baseUrl") != expected_host:
                continue
            if config["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd":
                return True
            workflow_id = config["metadata"].get("labels", {}).get(
                "recsys.ai/workflow"
            )
            # Recommendation cleanup never owns workflow retention. Preserve
            # every backend referenced by a workflow ModelConfig; the workflow
            # pipeline may retire it under its own state/session policy later.
            if workflow_id:
                return True
            adapter = deployments.get(config["metadata"]["name"])
            if adapter and adapter.get("spec", {}).get("replicas", 1) > 0:
                return True
        return False

    def recommendation_adapter_inventory(self):
        """Return exact release/deployment identities, excluding workflow adapters."""
        deployments = json.loads(
            self.kube(
                "get",
                "deployments",
                "-l",
                "recsys.ai/owner=llm-agent-cd",
                "-o",
                "json",
            )
        ).get("items", [])
        inventory = {}
        for value in deployments:
            metadata = value.get("metadata", {})
            labels = metadata.get("labels", {})
            if (
                not metadata.get("name", "").startswith("rec-ab-")
                or labels.get("recsys.ai/workflow")
            ):
                continue
            identities = [
                entry.get("value")
                for container in value["spec"]["template"]["spec"].get("containers", [])
                for entry in container.get("env", [])
                if entry.get("name") == "RELEASE_ID"
            ]
            if len(identities) != 1 or not re.fullmatch(r"[0-9a-f]{64}", identities[0] or ""):
                raise ValueError("Recommendation adapter release identity drift")
            if identities[0] in inventory:
                raise ValueError("duplicate Recommendation adapter release identity")
            inventory[identities[0]] = metadata["name"]
        return inventory

    def _adapter_backend_name(self, adapter_name):
        raw = self.kube(
            "get", "modelconfig", adapter_name, "--ignore-not-found", "-o", "json"
        )
        if not raw:
            return None
        config = json.loads(raw)
        if config["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd":
            raise ValueError("refusing foreign ModelConfig: " + adapter_name)
        base_url = config.get("spec", {}).get("openAI", {}).get("baseUrl", "")
        match = re.fullmatch(
            r"http://(rec-llm-[0-9a-f]{20})\."
            + re.escape(self.namespace)
            + r"\.svc\.cluster\.local:8000/v1",
            base_url,
        )
        return match.group(1) if match else None

    def retire_release_capacity(self, retired_release_ids, protected_llm_version_ids):
        """Scale only unreachable, controller-owned serving Deployments to zero."""
        if self.scope != "recommendation":
            raise ValueError("generic terminal cleanup is currently Recommendation-only")
        inventory = self.recommendation_adapter_inventory()
        adapter_results = {}
        backend_candidates = set()
        for release_id in sorted(set(retired_release_ids)):
            adapter_name = inventory.get(release_id)
            if not adapter_name:
                adapter_results[release_id] = "ABSENT"
                continue
            backend = self._adapter_backend_name(adapter_name)
            if backend:
                backend_candidates.add(backend)
            adapter_results[adapter_name] = self._scale_owned_deployment_to_zero(
                adapter_name, release_id=release_id
            )

        backend_results = {}
        protected_backends = {
            "rec-llm-" + llm_version_id[:20]
            for llm_version_id in protected_llm_version_ids
        }
        for backend in sorted(backend_candidates):
            if backend in protected_backends:
                backend_results[backend] = "PROTECTED_BY_RELEASE"
            elif self._backend_has_live_consumer(backend):
                backend_results[backend] = "PROTECTED_BY_LIVE_CONSUMER"
            else:
                backend_results[backend] = self._scale_owned_deployment_to_zero(
                    backend
                )
        return {"adapters": adapter_results, "backends": backend_results}

    def offline(self, state):
        from .offline import job, observation
        from apps.agentic.llm_ab_router.trigger import dispatch_job_matches
        desired = job(state, self.image, self.namespace)
        raw = self.kube('get','job',desired['metadata']['name'],'--ignore-not-found','-o','json')
        if not raw:
            self.kube('create','-f','-',stdin=json.dumps(desired))
        elif not dispatch_job_matches(json.loads(raw),desired):
            raise ValueError('offline Job immutable manifest conflict')
        observed=observation(self.db,state)
        if observed['verdict']=='PASS' and (not raw or not any(c.get('type')=='Complete' and c.get('status')=='True'
                for c in json.loads(raw).get('status',{}).get('conditions',[]))):
            return {**observed,'verdict':'HOLD','reason':'offline execution finished; waiting for Job termination'}
        return observed

    def __init__(self):
        self.namespace = os.environ.get("AB_NAMESPACE", "kagent")
        self.image = os.environ["AB_ROUTER_IMAGE"]
        if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", self.image):
            raise ValueError("AB_ROUTER_IMAGE must be digest pinned")
        self.secret = os.environ.get("AB_SECRET_NAME", "recsys-llm-ab-runtime")
        self.url = os.environ.get(
            "AB_ROUTER_URL",
            f"http://recsys-ab-router.{self.namespace}.svc.cluster.local",
        )
        self.token = os.environ["AB_INTERNAL_TOKEN"]
        self.db = Database(os.environ["AB_DATABASE_URL"])
        self.scope = os.environ.get("AB_SCOPE", "recommendation")
        self.gateway_name = "recsys-workflow-gateway" if self.scope == "workflow" else "recsys-ab-gateway"
        self.router_name = "recsys-workflow-router" if self.scope == "workflow" else "recsys-ab-router"

    def kube(self, *args, stdin=None):
        return command("kubectl", "-n", self.namespace, *args, stdin=stdin)

    def preflight(self, champion, candidate, fixtures, *, diagnostic=False):
        from .release_guard import check
        check(self.scope)
        if champion.get("scope") == "workflow":
            from .workflow import members
            from .capacity import verify_capacity, workflow_job_reservations
            desired_deployments = {}
            # Healthy router pods alone do not prove the underlying A2A runtime
            # can serve. Substrate/Valkey outages must block before deployment.
            self.kube("rollout", "status", "deployment/kagent-controller", "--timeout=30s")
            self.kube("rollout", "status", "deployment/" + self.router_name, "--timeout=30s")
            self.kube("rollout", "status", "deployment/" + self.gateway_name, "--timeout=30s")
            for r in (champion, candidate):
                for member in members(r).values():
                    self.kube("get", "workerpool", member["binding"]["worker_pool"], "-o", "name")
                manifests = backend_resources(r, self.namespace, self.image) + resources(r, self.namespace, self.image, self.secret)
                for obj in manifests:
                    if obj["kind"] == "Deployment":
                        desired_deployments[obj["metadata"]["name"]] = obj
                self.kube("apply", "--dry-run=server", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": manifests}))
                if not r["binding"].get("managed_backend"):
                    att = json.loads(self.kube("get", "configmap", r["binding"]["attestation_configmap"], "-o", "json"))["data"]
                    if any(att.get(k) != v for k, v in {"llm_version_id": r["llm_version_id"], "artifact_sha256": r["llm"]["artifact_sha256"]}.items()):
                        raise ValueError("shared workflow backend attestation mismatch")
            if len(fixtures) != 20 or not all(c.get("expected", {}).get("scope") == "workflow" for c in fixtures):
                raise ValueError("workflow requires twenty workflow assertions")
            nodes = json.loads(self.kube("get", "nodes", "-o", "json"))["items"]
            pods = json.loads(self.kube("get", "pods", "-A", "-o", "json"))["items"]
            existing = json.loads(self.kube("get", "deployments", "-o", "json"))["items"]
            if not diagnostic:
                for reservation in workflow_job_reservations(pods,probe=True):
                    desired_deployments[reservation['metadata']['name']]=reservation
                pool=json.loads(self.kube('get','workerpool','recsys-workflow-router-pool','-o','json'))
                missing_facades=max(0,1-pool['spec'].get('replicas',0))
                if missing_facades:
                    template=pool['spec']['template']
                    desired_deployments['reserved-facade']={'metadata':{'name':'reserved-facade','namespace':self.namespace},
                        'spec':{'replicas':missing_facades,'template':{'spec':{
                            'nodeSelector':template.get('nodeSelector',{}),'tolerations':template.get('tolerations',[]),
                            'containers':[{'resources':template['resources']}]}}}}
                if not self.kube('get','deployment','recsys-workflow-trigger','--ignore-not-found','-o','name').strip():
                    desired_deployments['reserved-receiver']={'metadata':{'name':'reserved-receiver','namespace':self.namespace},
                        'spec':{'replicas':2,'template':{'spec':{'nodeSelector':{'recsys.ai/pool':'ml-system'},
                            'tolerations':[{'key':'recsys.ai/workload','operator':'Equal','value':'ml-system','effect':'NoSchedule'}],
                            'containers':[{'resources':{'requests':{'cpu':'50m','memory':'128Mi'}}}]}}}}
            verify_capacity(nodes, pods, list(desired_deployments.values()),
                            {(d["metadata"]["namespace"], d["metadata"]["name"]):d for d in existing},
                            headroom={'cpu':'200m','memory':'128Mi'})
            return
        # Recommendation-only acceptance calls its dedicated router directly.
        # Coordinator and Context are intentionally outside this experiment and
        # therefore must not be a deployment or readiness dependency here.
        self.kube("rollout", "status", "deployment/recsys-ab-router", "--timeout=30s")
        self.kube("rollout", "status", "deployment/recsys-ab-gateway", "--timeout=30s")
        for r in (champion, candidate):
            self.kube("get", "workerpool", r["binding"]["worker_pool"], "-o", "name")
            manifests = backend_resources(r, self.namespace, self.image) + resources(
                r, self.namespace, self.image, self.secret
            )
            self.kube(
                "apply",
                "--dry-run=server",
                "-f",
                "-",
                stdin=json.dumps(
                    {"apiVersion": "v1", "kind": "List", "items": manifests}
                ),
            )
            if not r["binding"].get("managed_backend"):
                # Shared Terraform-owned backend needs an independently verified attestation.
                attestation = json.loads(
                    self.kube(
                        "get",
                        "configmap",
                        r["binding"]["attestation_configmap"],
                        "-o",
                        "json",
                    )
                )["data"]
                if (
                    attestation.get("llm_version_id") != r["llm_version_id"]
                    or attestation.get("artifact_sha256") != r["llm"]["artifact_sha256"]
                ):
                    raise ValueError(
                        "shared backend is not attested to this immutable LLM version"
                    )
        if not all(
            c.get("prompt") and isinstance(c.get("expected"), dict) for c in fixtures
        ):
            raise ValueError("fixture prompt/assertions missing")

    def snapshot_route(self):
        return json.loads(
            self.kube("get", "virtualservice", "recsys-workflow-ab" if self.scope == "workflow" else "recsys-ab", "-o", "json")
        )["spec"]

    def deploy(self, release):
        manifests = backend_resources(release, self.namespace, self.image) + resources(
            release, self.namespace, self.image, self.secret
        )
        for obj in manifests:
            current = self.kube(
                "get",
                obj["kind"],
                obj["metadata"]["name"],
                "--ignore-not-found",
                "-o",
                "json",
            )
            if current:
                live = json.loads(current)
                # A capacity-window pause changes only replicas, not the
                # immutable model/template identity. Reconcile that exact
                # operational field after all identity checks, never rewrite
                # a drifted backend or replay an invocation.
                paused = operator_paused_deployment(obj, live)
                single_replica = operator_single_replica_deployment(obj, live)
                comparable = deepcopy(live.get('spec'))
                if paused:
                    comparable['replicas']=1
                elif single_replica:
                    comparable['replicas']=2
                if obj["kind"] == "ConfigMap":
                    # ConfigMaps have data, not spec. Resume must verify the
                    # pinned template rather than overwrite or ignore it.
                    if (live["metadata"].get("labels", {}).get("recsys.ai/owner") != "llm-agent-cd"
                            or live.get("immutable") is not True
                            or live.get("data") != obj.get("data")
                            or live.get("binaryData", {})):
                        raise ValueError("immutable serving template drift detected")
                    continue
                if live["metadata"].get("labels", {}).get(
                    "recsys.ai/owner"
                ) != "llm-agent-cd" or not contains_spec(
                    normalized_live_spec(obj["kind"], comparable), obj["spec"]
                ):
                    raise ValueError(
                        "immutable release drift or foreign resource detected: "
                        + obj["kind"]
                        + "/"
                        + obj["metadata"]["name"]
                    )
                if not contains_spec(live['metadata'].get('annotations',{}),obj['metadata'].get('annotations',{})):
                    raise ValueError('immutable runtime policy annotation drift')
                expected = (
                    obj["metadata"]
                    .get("annotations", {})
                    .get("recsys.ai/immutable-spec")
                )
                if (
                    expected
                    and live["metadata"]
                    .get("annotations", {})
                    .get("recsys.ai/immutable-spec")
                    != expected
                ):
                    raise ValueError(
                        "refusing to overwrite an immutable or foreign release"
                    )
                if paused:
                    self.kube('patch','deployment',obj['metadata']['name'],'--type=json','--patch-file=/dev/stdin',
                        stdin=json.dumps([
                            {'op':'test','path':'/metadata/resourceVersion','value':live['metadata']['resourceVersion']},
                            {'op':'test','path':'/spec/replicas','value':0},
                            {'op':'replace','path':'/spec/replicas','value':1}]))
                continue
            self.kube("create", "-f", "-", stdin=json.dumps(obj))
        for obj in manifests:
            if obj["kind"] == "Deployment":
                # Scheduling/capacity/startup failure occurs before traffic can shift.
                self.kube(
                    "rollout",
                    "status",
                    "deployment/" + obj["metadata"]["name"],
                    "--timeout=600s",
                )

    def verify_release(self, release):
        if release.get("scope") == "workflow":
            from .workflow import members
            for desired in resources(release,self.namespace,self.image,self.secret):
                if desired['kind'] not in {'SandboxAgent','ModelConfig'}:
                    continue
                live=json.loads(self.kube('get',desired['kind'],desired['metadata']['name'],'-o','json'))
                if not contains_spec(normalized_live_spec(desired['kind'],live['spec']),desired['spec']) or not contains_spec(live['metadata'].get('annotations',{}),desired['metadata'].get('annotations',{})):
                    raise ValueError('workflow spec or execution policy drift')
            variants=members(release)
            for role in ('context','recommendation','coordinator'):
                member=variants[role]
                n = name(member)
                self.kube("wait", "--for=condition=Accepted", "modelconfig/" + n, "--timeout=120s")
                if role=='coordinator':
                    live=json.loads(self.kube('get','sandboxagent',n,'-o','json'))
                    # Recover an earlier dependency-order compilation failure.
                    # All referenced specialists were just verified Ready.
                    # The pinned controller watches label changes, not annotations.
                    failed=any(c.get('type')=='Accepted' and c.get('status')=='False'
                        and c.get('reason')=='ReconcileFailed' and 'SandboxAgent.kagent.dev' in c.get('message','')
                        and 'not found' in c.get('message','') for c in live.get('status',{}).get('conditions',[]))
                    marker=release['release_id'][:63]
                    if failed and live['metadata'].get('labels',{}).get('recsys.ai/dependencies-ready')!=marker:
                        self.kube('patch','sandboxagent',n,'--type=json','--patch-file=/dev/stdin',stdin=json.dumps([
                            {'op':'test','path':'/metadata/resourceVersion','value':live['metadata']['resourceVersion']},
                            {'op':'add','path':'/metadata/labels/recsys.ai~1dependencies-ready','value':marker}]))
                self.kube("wait", "--for=condition=Ready", "sandboxagent/" + n, "--timeout=120s")
                runtime = release.get("runtime")
                if runtime:
                    live = json.loads(self.kube("get", "sandboxagent", n, "-o", "json"))
                    templates = json.loads(self.kube("get", "actortemplates", "-l",
                        "kagent.dev/sandbox-agent=" + n, "-o", "json"))["items"]
                    desired_generation = str(live["metadata"]["generation"])
                    active = [item for item in templates
                        if not item["metadata"].get("deletionTimestamp")
                        and item["metadata"].get("annotations", {}).get(
                            "kagent.dev/desired-generation") == desired_generation
                        and item.get("status", {}).get("phase") == "Ready"
                        and item["spec"]["containers"][0]["image"]
                            == runtime["go_adk_image"]]
                    if len(active) != 1:
                        raise ValueError("immutable workflow Go ADK runtime attestation failed for " + n)
                card = httpx.get(f"http://kagent-controller.{self.namespace}.svc.cluster.local:8083/api/a2a-sandboxes/{self.namespace}/{n}/.well-known/agent-card.json", timeout=15)
                card.raise_for_status()
                if not card.json().get("skills"):
                    raise ValueError("workflow member card incomplete")
        n = name(release)
        self.kube(
            "wait", "--for=condition=Accepted", "modelconfig/" + n, "--timeout=120s"
        )
        # Adapter readiness and the controller-hosted agent card do not prove
        # that Substrate can start this immutable native agent.  Require the
        # ActorTemplate golden snapshot through SandboxAgent Ready before any
        # route can reference the release.
        self.kube(
            "wait", "--for=condition=Ready", "sandboxagent/" + n,
            "--timeout=180s",
        )
        self.kube("rollout", "status", "deployment/" + n, "--timeout=120s")
        # No synthetic model generation during readiness verification.
        endpoint = f"http://kagent-controller.{self.namespace}.svc.cluster.local:8083/api/a2a-sandboxes/{self.namespace}/{n}/.well-known/agent-card.json"
        result = httpx.get(endpoint, timeout=15)
        result.raise_for_status()
        if release["binding"].get("managed_backend"):
            props = wait_ready_get(
                release["binding"]["backend_url"].removesuffix("/v1").rstrip("/") + "/props",
            )
            verify_tool_template_props(props.json(), release["llm"]["serving"])
        if not result.json().get("skills"):
            raise ValueError("release agent card is incomplete")
        result = wait_ready_get(
            release["binding"].get("health_url")
            or (
                release["binding"]["backend_url"].removesuffix("/v1").rstrip("/")
                + "/health"
            ),
        )

    def route(self, state, weight):
        obj = virtual_service(state, weight, self.namespace)
        self.kube("apply", "-f", "-", stdin=json.dumps(obj))
        return obj["metadata"]["annotations"]["recsys.ai/route-revision"]

    def verify_route(self, state, weight, revision):
        desired = virtual_service(state, weight, self.namespace)
        if desired["metadata"]["annotations"]["recsys.ai/route-revision"] != revision:
            return False
        pods = json.loads(
            self.kube("get", "pods", "-l", "istio=" + self.gateway_name, "-o", "json")
        )["items"]
        ready = [
            p
            for p in pods
            if not p["metadata"].get("deletionTimestamp")
            and any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in p.get("status", {}).get("conditions", [])
            )
        ]
        if len(ready) != len(pods) or len(ready) < 2:
            return False
        for pod in ready:
            dump = json.loads(
                self.kube(
                    "exec",
                    pod["metadata"]["name"],
                    "-c",
                    "istio-proxy",
                    "--",
                    "pilot-agent",
                    "request",
                    "GET",
                    "config_dump",
                )
            )
            if not envoy_allocation_verified(dump, desired):
                return False
        # Prove a real data-plane request reaches the pinned immutable release.
        for r in (
            [state["baseline"]]
            if weight == 0
            else [state["baseline"], state["pending"]]
        ):
            response = httpx.get(
                self.url + "/internal/verify",
                params={"release_id": r["release_id"]},
                headers={"authorization": "Bearer " + self.token},
                timeout=20,
            )
            if (
                response.status_code != 200
                or response.json().get("release_id") != r["release_id"]
            ):
                return False
        return True

    def observe(self, state, start, end):
        try:
            observation = self.db.observe(state, start, end)
            if state["policy"].get("sample_source") == "live_test":
                observation["organic"] = self.db.observe(state, start, end, source="production")
            return observation
        except Exception:
            return {"healthy": False}

    def send_case(self, state, case, request_id):
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": request_id,
                    "contextId": request_id,
                    "role": "ROLE_USER",
                    "parts": [{"kind": "text", "text": case["prompt"]}],
                }
            },
        }
        try:
            # Exactly one SendMessage. Transport errors are ambiguous and MUST NOT retry.
            httpx.post(
                self.url + "/",
                json=body,
                headers={
                    "authorization": "Bearer " + self.token,
                    "x-recsys-source": "synthetic",
                    "x-user-id": "llm-ab-suite",
                    "a2a-version": "1.0",
                },
                timeout=state["policy"]["request_timeout_seconds"],
            )
        except httpx.HTTPError:
            pass  # Persisted STARTED is recovered through the DB; otherwise HOLD.

    def send_external_suite(self, state):
        if self.scope != "recommendation":
            raise ValueError("public A2A suite is Recommendation-only")
        from .external_cases import run

        return run(state, database=self.db)

    def external_suite_evidence(self, state):
        return self.db.external_suite(state["experiment_id"])

    def case_result(self, request_id):
        from .release import digest
        return self.db.result(digest([self.scope, request_id]) if self.scope == "workflow" else request_id)

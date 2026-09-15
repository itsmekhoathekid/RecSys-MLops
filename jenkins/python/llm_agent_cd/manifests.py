from __future__ import annotations

from copy import deepcopy
from .release import digest


def name(release):
    return release.get("resource_name") or "rec-ab-" + release["release_id"][:20]


def resources(release, namespace, image, secret):
    if release.get("scope") == "workflow":
        from .workflow import members
        result = []
        variants = members(release)
        # The pinned compiler treats absent specialist references as a failed
        # compilation; publish dependencies before their Coordinator.
        for role in ('context','recommendation','coordinator'):
            member = variants[role]
            for obj in resources(member, namespace, image, secret):
                if role != "coordinator" and obj["kind"] not in {"ModelConfig", "SandboxAgent"}:
                    continue
                obj["metadata"]["labels"].update({"recsys.ai/workflow": release["release_id"][:63],
                                                   "recsys.ai/agent-role": role})
                if obj["kind"] == "Deployment":
                    obj["spec"]["selector"]["matchLabels"]["app"] = "recsys-workflow-adapter"
                    obj["spec"]["template"]["metadata"]["labels"]["app"] = "recsys-workflow-adapter"
                    obj['spec']['template']['spec']['nodeSelector'] = {'recsys.ai/pool':'ml-system'}
                    if role == 'coordinator':
                        # Preserve two stock-runtime adapters while placing one
                        # on each node pool. This is part of the immutable
                        # workflow release spec, not a live scheduling override.
                        # The base adapter deliberately shares its common label
                        # mapping with the Service. Copy the Deployment mappings
                        # before adding placement so the Service selects both
                        # immutable replicas rather than only the ml-system one.
                        obj['spec']['selector']['matchLabels'] = dict(obj['spec']['selector']['matchLabels'])
                        obj['spec']['template']['metadata']['labels'] = dict(obj['spec']['template']['metadata']['labels'])
                        obj['spec']['replicas'] = 1
                        obj['spec']['selector']['matchLabels']['recsys.ai/adapter-placement'] = 'ml-system'
                        obj['spec']['template']['metadata']['labels']['recsys.ai/adapter-placement'] = 'ml-system'
                        obj["metadata"]["annotations"]["recsys.ai/immutable-spec"] = digest(obj["spec"])
                        cpu = deepcopy(obj)
                        cpu['metadata']['name'] += '-cpu'
                        cpu['spec']['selector']['matchLabels']['recsys.ai/adapter-placement'] = 'cpu-services'
                        cpu['spec']['template']['metadata']['labels']['recsys.ai/adapter-placement'] = 'cpu-services'
                        cpu['spec']['template']['spec']['nodeSelector'] = {'recsys.ai/pool':'cpu-services'}
                        cpu['spec']['template']['spec'].pop('tolerations',None)
                        cpu["metadata"]["annotations"]["recsys.ai/immutable-spec"] = digest(cpu["spec"])
                        result.extend((obj,cpu))
                        continue
                elif obj["kind"] == "Service":
                    obj["spec"]["selector"]["app"] = "recsys-workflow-adapter"
                obj["metadata"]["annotations"]["recsys.ai/immutable-spec"] = digest(obj["spec"])
                result.append(obj)
        return result
    rid, n = release["release_id"], name(release)
    binding = release["binding"]
    # Transport image is pinned independently of the CLI/router implementation.
    image = binding.get("adapter_image", image)
    labels = {"recsys.ai/release": rid[:63], "recsys.ai/owner": "llm-agent-cd"}

    def obj(kind, resource_name, spec, api="v1"):
        return {
            "apiVersion": api,
            "kind": kind,
            "metadata": {
                "name": resource_name,
                "namespace": namespace,
                "labels": labels,
                "annotations": {"recsys.ai/immutable-spec": digest(spec)},
            },
            "spec": spec,
        }

    model = obj(
        "ModelConfig",
        n,
        {
            "provider": "OpenAI",
            "model": binding["model_alias"],
            "apiKeySecret": binding["api_key_secret"],
            "apiKeySecretKey": binding["api_key_secret_key"],
            "defaultHeaders": binding.get("default_headers", {}),
            "openAI": {
                "baseUrl": binding["backend_url"],
                **release["config"],
            },
        },
        "kagent.dev/v1alpha2",
    )
    workflow_member = bool(release.get("workflow_release_id"))
    descriptions = {
        "coordinator": "A2A workflow coordinator with Context and Recommendation specialist tools.",
        "context": "Context specialist A2A agent for feature and RAG context requests.",
        "recommendation": "Recommendation specialist A2A agent for personalized recommendation requests.",
    }
    terminal_descriptions = {
        "coordinator": "A2A workflow coordinator with exactly Context and Recommendation specialist tools.",
        "context": "Context specialist. Call once with the original Context request. Its returned response is terminal completed data; never send it to this or another specialist.",
        "recommendation": "Recommendation specialist. Call once with the original Recommendation request. Its returned response is terminal completed data; never send it to this or another specialist.",
    }
    description = "Immutable A/B Recommendation Agent release " + rid
    if workflow_member:
        profile = release.get("runtime", {}).get("a2a_description_profile")
        description = (terminal_descriptions[release["role"]]
                       if profile == "role-terminal-v2" else
                       descriptions[release["role"]] if profile == "role-v1" else "")
    agent = obj(
        "SandboxAgent",
        n,
        {
            "type": "Declarative",
            # ADK appends this description to every model prompt. Workflow
            # identity is carried by immutable resource names, so omit the
            # long opaque release hash for small models.
            "description": description,
            "sandbox": {"network": {"allowedDomains": binding["allowed_domains"]}},
            "substrate": {
                "workerPoolRef": {
                    "apiGroup": "ate.dev",
                    "kind": "WorkerPool",
                    "name": binding["worker_pool"],
                }
            },
            "declarative": {**release["agent"], "modelConfig": n, "stream": False},
        },
        "kagent.dev/v1alpha2",
    )
    pod_labels = {"app": "recsys-ab-adapter", "release": n}
    adapter_env = [
        {"name": "MODE", "value": "adapter"},
        {"name": "PORT", "value": "8080"},
        {"name": "RELEASE_ID", "value": rid},
        {
            "name": "A2A_UPSTREAM",
            "value": f"http://kagent-controller.{namespace}.svc.cluster.local:8083/api/a2a-sandboxes/{namespace}/{n}/",
        },
    ]
    if workflow_member or binding.get("grpc_transport"):
        adapter_env.append(
            {
                "name": "AB_KAGENT_GRPC_TARGET",
                "value": f"kagent-controller.{namespace}.svc.cluster.local:8084",
            }
        )
    if binding.get("recommendation_output_profile"):
        if binding["recommendation_output_profile"] != "trusted-tool-result-v1":
            raise ValueError("unknown Recommendation output profile")
        adapter_env.append(
            {
                "name": "RECOMMENDATION_OUTPUT_PROFILE",
                "value": binding["recommendation_output_profile"],
            }
        )
    output_profile = release.get("runtime", {}).get("deterministic_output")
    if (release.get("role") == "coordinator" and output_profile in {
            "a2a-results-v1", "trusted-child-tool-results-v2"}):
        role_names = {role: next((tool["agent"]["name"] for tool in
            release["agent"]["tools"] if "-" + role + "-" in tool["agent"]["name"]), None)
            for role in ("context", "recommendation")}
        if not all(role_names.values()):
            raise ValueError("role-readable A2A names required for deterministic output")
        adapter_env.extend([
            {"name": "WORKFLOW_OUTPUT_PROFILE", "value": output_profile},
            {"name": "WORKFLOW_CONTEXT_TOOL", "value":
             "kagent__NS__" + role_names["context"].replace("-", "_")},
            {"name": "WORKFLOW_RECOMMENDATION_TOOL", "value":
             "kagent__NS__" + role_names["recommendation"].replace("-", "_")},
        ])
    adapter = obj(
        "Deployment",
        n,
        {
            # Schema-v1 releases were originally rendered with two replicas;
            # production may retain an audited 2->1 operational override.
            "replicas": binding.get("adapter_replicas", 2),
            "selector": {"matchLabels": pod_labels},
            "template": {
                "metadata": {
                    "labels": pod_labels,
                    "annotations": {"sidecar.istio.io/inject": "false"},
                },
                "spec": {
                    "automountServiceAccountToken": False,
                    "tolerations": [
                        {
                            "key": "recsys.ai/workload",
                            "operator": "Equal",
                            "value": "ml-system",
                            "effect": "NoSchedule",
                        }
                    ],
                    "containers": [
                        {
                            "name": "adapter",
                            "image": image,
                            "envFrom": [{"secretRef": {"name": secret}}],
                            "env": adapter_env,
                            "ports": [{"name": "http", "containerPort": 8080}],
                            "resources": {
                                "requests": {"cpu": "25m", "memory": "64Mi"},
                                "limits": {"cpu": "1", "memory": "256Mi"},
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/healthz", "port": "http"}
                            },
                        }
                    ],
                },
            },
        },
        "apps/v1",
    )
    service = obj(
        "Service",
        n,
        {
            "selector": pod_labels,
            "ports": [{"name": "http", "port": 80, "targetPort": "http"}],
        },
    )
    return [model, agent, adapter, service]


def virtual_service(state, weight, namespace):
    baseline, candidate = state["baseline"], state["pending"]
    releases = {
        **state.get("releases", {}),
        baseline["release_id"]: baseline,
        candidate["release_id"]: candidate,
    }

    def destination(r):
        return {
            "host": f"{name(r)}.{namespace}.svc.cluster.local",
            "port": {"number": 80},
        }

    gateway = "recsys-workflow-gateway" if baseline.get("scope") == "workflow" else "recsys-ab-gateway"
    spec = {
        "hosts": [f"{gateway}.{namespace}.svc.cluster.local"],
        "gateways": [gateway],
        "http": [],
    }
    for rid, r in sorted(releases.items()):
        if rid in state.get("disabled", []):
            continue
        spec["http"].append(
            {
                "name": "pin-" + rid[:20],
                "match": [
                    {
                        "uri": {"exact": path},
                        "headers": {"x-recsys-release": {"exact": rid}},
                    }
                    for path in ("/", "/identity")
                ],
                "route": [{"destination": destination(r)}],
                "retries": {"attempts": 0},
                "timeout": "610s",
            }
        )
    weighted = [{"destination": destination(baseline), "weight": 100 - weight}]
    if baseline["release_id"] != candidate["release_id"]:
        weighted.append({"destination": destination(candidate), "weight": weight})
    spec["http"].append(
        {
            "name": "allocate",
            "match": [{"uri": {"exact": "/allocate"}}],
            "route": [r for r in weighted if r["weight"] > 0],
            "retries": {"attempts": 0},
            "timeout": "10s",
        }
    )
    spec["http"].append(
        {
            "name": "deny-unassigned",
            "directResponse": {
                "status": 403,
                "body": {"string": "trusted assignment required"},
            },
        }
    )
    revision = digest(spec)
    for route in spec["http"]:
        route["name"] += "-" + revision[:16]
    return {
        "apiVersion": "networking.istio.io/v1beta1",
        "kind": "VirtualService",
        "metadata": {
            "name": "recsys-workflow-ab" if baseline.get("scope") == "workflow" else "recsys-ab",
            "namespace": namespace,
            "labels": {"recsys.ai/owner": "llm-agent-cd"},
            "annotations": {"recsys.ai/route-revision": revision},
        },
        "spec": spec,
    }


def backend_resources(release, namespace, downloader_image):
    """Dedicated backend only for a new LLM ID. Existing shared backends are never modified."""
    if not release["binding"].get("managed_backend"):
        return []
    llm = release["llm"]
    n = "rec-llm-" + release["llm_version_id"][:20]
    expected_url = f"http://{n}.{namespace}.svc.cluster.local:8000/v1"
    if release["binding"]["backend_url"] != expected_url:
        raise ValueError("managed backend URL does not match immutable LLM ID")
    serving = llm["serving"]
    from .serving_profiles import (backend_profile, SMALL, SMALL_V2, SMALL_B8646,
                                   SMALL_B8646_TERMINAL, QWEN35_NATIVE,
                                   QWEN35_NATIVE_V2, QWEN35_NATIVE_V3,
                                   QWEN35_B8646_STOCK, QWEN35_B8646_STOCK_CACHE, tool_template,
                                   terminal_tool_template,
                                   qwen35_tool_template, V2_FIELDS)
    backend_limits, extra_args = backend_profile(llm)
    generic_v2 = serving.get("schemaVersion") == 2
    if generic_v2:
        from .model_onboarding import load_policy
        downloader_image = load_policy()["runtime"]["downloader_image"]
    if serving.get('resourceProfile') in {SMALL_V2, SMALL_B8646, SMALL_B8646_TERMINAL,
                                          QWEN35_NATIVE, QWEN35_NATIVE_V2,
                                          QWEN35_NATIVE_V3, QWEN35_B8646_STOCK,
                                          QWEN35_B8646_STOCK_CACHE}:
        # Preserve the attested v2 backend's infrastructure identity across
        # evaluator/router upgrades. This downloader verifies the same SHA256;
        # it is not a serving runtime or a Langfuse-configurable image.
        downloader_image = ('asia-southeast1-docker.pkg.dev/recsys-mlops-506406/recsys/'
            'recsys-llm-ab-router@sha256:918bcd199c8c89f0ab2c667d065506a00cd6178d85052d4511ae26de8ec13803')
    flags = {
        "contextSize": "ctx-size",
        "maxPredictedTokens": "n-predict",
        "reasoningBudget": "reasoning-budget",
        "parallel": "parallel",
        "threads": "threads",
        "threadsBatch": "threads-batch",
        "batchSize": "batch-size",
        "ubatchSize": "ubatch-size",
    }
    if generic_v2 or serving.get("resourceProfile") in {SMALL, SMALL_V2, SMALL_B8646,
                                           SMALL_B8646_TERMINAL, QWEN35_NATIVE, QWEN35_NATIVE_V2,
                                           QWEN35_NATIVE_V3, QWEN35_B8646_STOCK,
                                           QWEN35_B8646_STOCK_CACHE}:
        flags.pop("reasoningBudget")
    legacy_metadata = {
        "resourceProfile",
        "chatTemplateSha256",
        "chatTemplateSourceRevision",
        "chatTemplateSource",
        "reasoningMode",
        "reasoningBudgetMessage",
        "llamaCppBuild",
        "llamaCppRevision",
        "cacheRamMiB",
    }
    allowed_metadata = V2_FIELDS if generic_v2 else legacy_metadata
    if not set(flags) <= set(serving) or set(serving) - set(flags) - allowed_metadata:
        raise ValueError("serving settings must explicitly define all supported flags")
    args = [
        "--model",
        "/models/model.gguf",
        "--alias",
        release["binding"]["model_alias"],
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--metrics",
        "--no-mmproj",
    ]
    for key, flag in flags.items():
        args.extend(["--" + flag, str(serving[key])])
    args.extend(extra_args)
    if "reasoningBudgetMessage" in serving:
        args.extend(["--reasoning-budget-message", serving["reasoningBudgetMessage"]])
    labels = {"app": n, "recsys.ai/owner": "llm-agent-cd"}
    container = {
        "name": "llama",
        "image": llm["image"],
        "args": args,
        "ports": [{"name": "http", "containerPort": 8000}],
        "resources": backend_limits,
        "volumeMounts": [{"name": "model", "mountPath": "/models", "readOnly": True}],
        "readinessProbe": {"httpGet": {"path": "/health", "port": "http"}},
        "startupProbe": {
            "httpGet": {"path": "/health", "port": "http"},
            "failureThreshold": 120,
            "periodSeconds": 10,
        },
    }
    spec = {
        "replicas": 1,
        "selector": {"matchLabels": {"app": n}},
        "template": {
            "metadata": {
                "labels": labels,
                "annotations": {"sidecar.istio.io/inject": "false"},
            },
            "spec": {
                "automountServiceAccountToken": False,
                "volumes": [{"name": "model", "emptyDir": {"sizeLimit": "5Gi"}}],
                "initContainers": [
                    {
                        "name": "verified-artifact",
                        "image": downloader_image,
                        "command": [
                            "python",
                            "-m",
                            "jenkins.python.llm_agent_cd.artifact",
                            llm["artifact_uri"],
                            llm["artifact_sha256"],
                            "/models/model.gguf",
                        ],
                        "volumeMounts": [{"name": "model", "mountPath": "/models"}],
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "64Mi"},
                            "limits": {"cpu": "1", "memory": "256Mi"},
                        },
                    }
                ],
                "containers": [container],
            },
        },
    }
    template_resources = []
    if serving.get("resourceProfile") == QWEN35_B8646_STOCK:
        # Live request accounting places this temporary backend on ml-system;
        # cpu-services is currently memory saturated.
        spec['template']['spec']['nodeSelector']={'recsys.ai/pool':'ml-system'}
        spec['template']['spec']['tolerations']=[{
            'key':'recsys.ai/workload','operator':'Equal','value':'ml-system','effect':'NoSchedule'}]
    elif serving.get("resourceProfile") == QWEN35_B8646_STOCK_CACHE:
        # The bounded-cache revision fits on N2 without co-locating with the
        # memory-saturated E2 control. It is a new immutable LLM identity.
        spec['template']['spec']['nodeSelector']={'recsys.ai/pool':'cpu-services'}
    elif serving.get("resourceProfile") in {QWEN35_NATIVE, QWEN35_NATIVE_V2, QWEN35_NATIVE_V3}:
        # Keep the already-ready small challenger on the memory-tight N2
        # pool. The reviewed native control fits E2 and tolerates its
        # dedicated taint, allowing both immutable backends to coexist.
        spec['template']['spec']['nodeSelector']={'recsys.ai/pool':'ml-system'}
        spec['template']['spec']['tolerations']=[{
            'key':'recsys.ai/workload','operator':'Equal','value':'ml-system','effect':'NoSchedule'}]
    elif serving.get("resourceProfile") in {SMALL_V2, SMALL_B8646,
                                              SMALL_B8646_TERMINAL}:
        spec['template']['spec']['nodeSelector']={'recsys.ai/pool':'cpu-services'}
    if serving.get("resourceProfile") in {SMALL_V2, SMALL_B8646,
                                           SMALL_B8646_TERMINAL,
                                           QWEN35_NATIVE, QWEN35_NATIVE_V2}:
        template_name = n + "-template"
        spec["template"]["spec"]["volumes"].append({"name": "chat-template", "configMap": {"name": template_name}})
        container["volumeMounts"].append({"name": "chat-template", "mountPath": "/chat-template", "readOnly": True})
        template = (terminal_tool_template() if serving.get("resourceProfile")
                    == SMALL_B8646_TERMINAL else tool_template()
                    if serving.get("resourceProfile") in {SMALL_V2, SMALL_B8646}
                    else qwen35_tool_template())
        template_resources.append({"apiVersion": "v1", "kind": "ConfigMap",
                                   "metadata": {"name": template_name, "namespace": namespace, "labels": labels},
                                   "immutable": True, "data": {"template.jinja": template}})
    metadata = {
        "name": n,
        "namespace": namespace,
        "labels": labels,
        "annotations": {"recsys.ai/immutable-spec": digest(spec)},
    }
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": metadata,
            "spec": spec,
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": n, "namespace": namespace, "labels": labels},
            "spec": {
                "selector": {"app": n},
                "ports": [{"name": "http", "port": 8000, "targetPort": "http"}],
            },
        },
    ] + template_resources

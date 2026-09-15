from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from urllib.parse import urlparse


MANAGED_BINDING_SCHEMA = 3


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def managed_backend_binding(
    binding: dict,
    llm_version_id: str,
    namespace: str = "kagent",
    *,
    adapter_image: str | None = None,
) -> dict:
    """Return the minimal immutable binding for a managed LLM backend.

    A candidate agent needs network access to its own backend, not every LLM
    backend inherited from the champion.  Schema v3 binds all fields rendered
    into ModelConfig/SandboxAgent/adapter resources, so a network-policy change
    can never reuse an existing release name with different immutable content.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", llm_version_id):
        raise ValueError("managed backend requires a SHA256 LLM identity")
    if not re.fullmatch(r"[a-z0-9-]+", namespace):
        raise ValueError("invalid managed backend namespace")

    value = deepcopy(binding)
    backend = "rec-llm-" + llm_version_id[:20]
    host = f"{backend}.{namespace}.svc.cluster.local"
    previous_host = urlparse(value.get("backend_url", "")).hostname
    managed_host = re.compile(
        rf"rec-llm-[0-9a-f]{{20}}\.{re.escape(namespace)}\.svc\.cluster\.local"
    )
    value["allowed_domains"] = sorted(
        {
            domain
            for domain in value.get("allowed_domains", [])
            if domain != previous_host and not managed_host.fullmatch(domain)
        }
        | {host}
    )
    value.update(
        backend_url=f"http://{host}:8000/v1",
        default_headers={},
        managed_backend=True,
        release_schema_version=MANAGED_BINDING_SCHEMA,
        adapter_replicas=1,
        grpc_transport=True,
    )
    if adapter_image is not None:
        if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", adapter_image):
            raise ValueError("candidate adapter image must be digest pinned")
        value["adapter_image"] = adapter_image
    value.pop("health_url", None)
    value.pop("attestation_configmap", None)
    return value


def release(manifest: dict) -> dict:
    """IDs depend on effective semantics, never aliases, endpoint URLs or credentials."""
    if manifest.get("scope") == "workflow":
        from .workflow import workflow
        return workflow(manifest)
    value = deepcopy(manifest)
    if set(value) - {
        "config",
        "llm",
        "agent",
        "binding",
        "config_id",
        "llm_version_id",
        "release_id",
    }:
        raise ValueError("unknown release fields")
    config, llm, agent = value["config"], value["llm"], value["agent"]
    if set(config) - {
        "temperature",
        "maxTokens",
        "seed",
        "topP",
        "frequencyPenalty",
        "presencePenalty",
    }:
        raise ValueError(
            "unsupported generation config; endpoint/model are not config fields"
        )
    if (
        not 0 <= float(config["temperature"]) <= 2
        or not 0 < config["maxTokens"] <= llm["serving"]["contextSize"]
    ):
        raise ValueError("invalid/incompatible generation config")
    config["temperature"] = str(float(config["temperature"]))
    if set(llm) != {
        "artifact_uri",
        "artifact_sha256",
        "quantization",
        "image",
        "serving",
    }:
        raise ValueError(
            "LLM identity requires artifact, quantization, image and serving settings"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", llm["artifact_sha256"]):
        raise ValueError("model checksum must be SHA256")
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", llm["image"]):
        raise ValueError("serving image must be digest pinned")
    from .serving_profiles import validate_profile
    validate_profile(llm)
    if (
        not agent.get("systemMessage")
        or not agent.get("tools")
        or agent.get("runtime") != "go"
    ):
        raise ValueError("agent requires fixed Go runtime, prompt and tools")
    if set(agent) - {"runtime", "systemMessage", "tools", "a2aConfig"}:
        raise ValueError("unsupported agent setting")
    binding = value["binding"]
    if not binding["backend_url"].startswith(("http://", "https://")):
        raise ValueError("invalid backend URL")
    if not re.fullmatch(r"[a-z0-9-]+", binding["worker_pool"]):
        raise ValueError("invalid worker pool")
    calculated = {"config_id": digest(config), "llm_version_id": digest(llm)}
    release_material = {**calculated, "agent": agent}
    binding_schema = binding.get("release_schema_version", 1)
    if binding_schema not in {1, 2, 3}:
        raise ValueError("unsupported release binding schema")
    if binding_schema in {2, 3}:
        # Legacy releases intentionally excluded mutable endpoint aliases from
        # release_id.  New releases bind the non-secret serving/transport
        # identity too, so correcting an alias or adapter profile can never
        # collide with an already-published immutable manifest.
        binding_identity = {
            key: binding.get(key)
            for key in (
                "adapter_image",
                "adapter_replicas",
                "backend_url",
                "grpc_transport",
                "managed_backend",
                "model_alias",
                "recommendation_output_profile",
                "worker_pool",
            )
        }
        if binding_schema == 3:
            binding_identity.update(
                schema=3,
                allowed_domains=binding.get("allowed_domains", []),
                api_key_secret=binding.get("api_key_secret"),
                api_key_secret_key=binding.get("api_key_secret_key"),
                default_headers=binding.get("default_headers", {}),
            )
        release_material["binding_id"] = digest(binding_identity)
    calculated["release_id"] = digest(release_material)
    for key, expected in calculated.items():
        if key in value and value[key] != expected:
            raise ValueError(f"{key} checksum mismatch")
    value.update(calculated)
    return value


def validate_experiment(champion: dict, candidate: dict, mode: str) -> None:
    if champion.get("scope") == "workflow" or candidate.get("scope") == "workflow":
        from .workflow import validate_workflow
        validate_workflow(champion, candidate, mode)
        return
    a, b = release(champion), release(candidate)
    expected = {
        "config_only": (True, False),
        "llm_only": (False, True),
        "combined": (True, True),
    }
    changed = (
        a["config_id"] != b["config_id"],
        a["llm_version_id"] != b["llm_version_id"],
    )
    if mode not in expected or changed != expected[mode]:
        raise ValueError(
            f"{mode}: config/LLM changes {changed} do not match experiment"
        )
    if a["agent"] != b["agent"]:
        raise ValueError("prompt, tools and agent runtime must remain identical")
    if mode == "config_only" and a["binding"] != b["binding"]:
        raise ValueError("config_only must reuse the exact backend and worker pool")


DEFAULT_POLICY = {
    "case_count": 20,
    "min_samples": 5,
    "window_seconds": 600,
    "stage_timeout_seconds": 3600,
    "latency_ratio": 1.2,
    "monitor_seconds": 86400,
    "monitor_interval_seconds": 300,
    "telemetry_max_age_seconds": 120,
    "request_timeout_seconds": 600,
}


def policy(value: dict) -> dict:
    optional = {"sample_source", "evaluation_version", "compatibility_suite", "synthetic_entrypoint"}
    if set(value) - optional != set(DEFAULT_POLICY) or value["case_count"] != 20:
        raise ValueError("policy must define all supported fields and exactly 20 cases")
    if value.get("sample_source", "production") not in {"production", "live_test"}:
        raise ValueError("invalid gate sample source")
    if value.get("synthetic_entrypoint", "internal") not in {"internal", "public_a2a"}:
        raise ValueError("invalid synthetic entrypoint")
    for key, number in value.items():
        if key == 'compatibility_suite':
            if number not in {
                'compatibility-smoke-v1',
                'recommendation-compatibility-smoke-v1',
                'recommendation-compatibility-smoke-v2',
            }:
                raise ValueError('unknown compatibility suite')
            continue
        if key == "evaluation_version":
            if number not in {"workflow-code-v1", "workflow-code-v2", "workflow-code-v3",
                              "workflow-code-v4", "recommendation-code-v1",
                              "recommendation-code-v2"}:
                raise ValueError("unknown evaluation version")
            continue
        if key == "sample_source":
            continue
        if key == "synthetic_entrypoint":
            continue
        if (
            not isinstance(number, (float, int))
            or isinstance(number, bool)
            or (number < 0 if key == "monitor_seconds" else number <= 0)
        ):
            raise ValueError(f"invalid policy field {key}")
    if value["min_samples"] < 5 or value["min_samples"] > 10:
        raise ValueError("min_samples must be between 5 and 10")
    if value["stage_timeout_seconds"] < value["window_seconds"]:
        raise ValueError("stage timeout is shorter than observation window")
    digest(value)  # Reject NaN/infinity.
    return deepcopy(value)

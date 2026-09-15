"""Read-only live snapshot; the only output is a local candidate manifest.

Model bytes are deliberately not guessed from an alias. Supply their verified
public URL and SHA256; the managed backend checks the bytes again at startup.
"""

import argparse
import json
from pathlib import Path

from .driver import command
from .release import release


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="kagent")
    parser.add_argument("--agent", default="recsys-recommendation-agent-sandbox")
    parser.add_argument("--serving-namespace", default="llm-inference")
    parser.add_argument("--serving-deployment", default="qwen35-gguf")
    parser.add_argument("--artifact-url", required=True)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--quantization", default="Q4_0")
    parser.add_argument("--output", required=True)
    parser.add_argument("--reuse-existing-backend", action="store_true")
    parser.add_argument(
        "--attestation-configmap", default="recsys-ab-champion-attestation"
    )
    args = parser.parse_args()
    spec = json.loads(
        command(
            "kubectl",
            "-n",
            args.namespace,
            "get",
            "sandboxagent",
            args.agent,
            "-o",
            "json",
        )
    )["spec"]
    mc = json.loads(
        command(
            "kubectl",
            "-n",
            args.namespace,
            "get",
            "modelconfig",
            spec["declarative"]["modelConfig"],
            "-o",
            "json",
        )
    )["spec"]
    deployment = json.loads(
        command(
            "kubectl",
            "-n",
            args.serving_namespace,
            "get",
            "deployment",
            args.serving_deployment,
            "-o",
            "json",
        )
    )
    container = next(
        c
        for c in deployment["spec"]["template"]["spec"]["containers"]
        if c["name"] == "llama-server"
    )
    flags = container["args"]
    flag_map = {
        "contextSize": "--ctx-size",
        "maxPredictedTokens": "--n-predict",
        "reasoningBudget": "--reasoning-budget",
        "parallel": "--parallel",
        "threads": "--threads",
        "threadsBatch": "--threads-batch",
        "batchSize": "--batch-size",
        "ubatchSize": "--ubatch-size",
    }
    manifest = {
        "config": {
            k: v for k, v in mc["openAI"].items() if k not in {"baseUrl", "apiFormat"}
        },
        "llm": {
            "artifact_uri": args.artifact_url,
            "artifact_sha256": args.artifact_sha256,
            "quantization": args.quantization,
            "image": container["image"],
            "serving": {
                k: int(flags[flags.index(flag) + 1]) for k, flag in flag_map.items()
            },
        },
        "agent": {
            k: v
            for k, v in spec["declarative"].items()
            if k in {"runtime", "systemMessage", "tools", "a2aConfig"}
        },
        "binding": {
            "managed_backend": True,
            "backend_url": "http://unbound/v1",
            "model_alias": mc["model"],
            "api_key_secret": mc["apiKeySecret"],
            "api_key_secret_key": mc["apiKeySecretKey"],
            "default_headers": {},
            "worker_pool": spec["substrate"]["workerPoolRef"]["name"],
            "allowed_domains": spec["sandbox"]["network"]["allowedDomains"],
        },
    }
    if "--reasoning-budget-message" in flags:
        manifest["llm"]["serving"]["reasoningBudgetMessage"] = flags[
            flags.index("--reasoning-budget-message") + 1
        ]
    if args.reuse_existing_backend:
        manifest["binding"].update(
            {
                "managed_backend": False,
                "backend_url": mc["openAI"]["baseUrl"],
                "default_headers": mc.get("defaultHeaders", {}),
                "health_url": f"http://{args.serving_deployment}.{args.serving_namespace}.svc.cluster.local:8000/health",
                "attestation_configmap": args.attestation_configmap,
            }
        )
    result = release(manifest)
    host = f"rec-llm-{result['llm_version_id'][:20]}.{args.namespace}.svc.cluster.local"
    if not args.reuse_existing_backend:
        result["binding"]["backend_url"] = f"http://{host}:8000/v1"
        result["binding"]["allowed_domains"] = sorted(
            set(result["binding"]["allowed_domains"]) | {host}
        )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(result["release_id"])


if __name__ == "__main__":
    main()

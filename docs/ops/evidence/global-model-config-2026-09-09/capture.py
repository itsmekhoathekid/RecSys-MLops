"""Read-only collection of global ModelConfig evidence. Run from repository root."""
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
STAMP = datetime.now(timezone.utc).isoformat()


def command(args):
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=True)
    return result.stdout


def save(name, value):
    (OUT / name).write_text(value)


def kube(kind, name):
    return json.loads(command(["kubectl", "--request-timeout=15s", "-n", "kagent", "get", kind, name, "-o", "json"]))


source = ROOT / "infra/helm/recsys-global-model-config/values.yaml"
save("source.txt", "Source file: " + str(source.relative_to(ROOT)) + "\nSHA256: " + hashlib.sha256(source.read_bytes()).hexdigest() + "\n\n" + "\n".join(f"{n:3} | {line}" for n, line in enumerate(source.read_text().splitlines(), 1)) + "\n")
save("rendered.yaml", command(["helm", "template", "recsys-global-model-config", "infra/helm/recsys-global-model-config", "-n", "kagent"]))

mc = kube("modelconfig", "recsys-global-model-config")
digest = hashlib.sha256(json.dumps(mc["spec"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
projected = {"captured_at_utc": STAMP, "apiVersion": mc["apiVersion"], "kind": mc["kind"], "metadata": {k: mc["metadata"][k] for k in ("name", "namespace", "uid", "generation", "creationTimestamp", "annotations")}, "spec": mc["spec"]}
save("modelconfig.json", json.dumps(projected, indent=2) + "\n")
save("modelconfig.txt", "Command: kubectl -n kagent get modelconfig recsys-global-model-config -o json\nProjection: name, namespace, Helm owner and spec; Secret values are not read.\n\n" + json.dumps({"name": mc["metadata"]["name"], "namespace": mc["metadata"]["namespace"], "helm_release": mc["metadata"]["annotations"]["meta.helm.sh/release-name"], "spec": mc["spec"]}, indent=2) + "\n")

agents = []
for role in ("context", "recommendation", "coordinator"):
    obj = kube("sandboxagent", f"recsys-{role}-agent-sandbox")
    config = obj["spec"]["declarative"]
    ready = next((c["status"] for c in obj.get("status", {}).get("conditions", []) if c["type"] == "Ready"), "Unknown")
    revision = obj["metadata"]["annotations"]["recsys.ai/model-config-revision"]
    agents.append({"name": obj["metadata"]["name"], "modelConfig": config["modelConfig"], "runtime": config["runtime"], "ready": ready, "generation": obj["metadata"]["generation"], "revision": revision, "digest_in_revision": digest in revision, "digest_in_prompt_marker": f"-{digest}." in config["systemMessage"]})
save("agents.json", json.dumps({"captured_at_utc": STAMP, "agents": agents}, indent=2) + "\n")
save("agents.txt", "Commands: kubectl -n kagent get sandboxagent <each named agent> -o json\nProjection: modelConfig, runtime, Ready condition and generation.\n\n" + "\n\n".join("\n".join(f"{k}: {a[k]}" for k in ("name", "modelConfig", "runtime", "ready", "generation")) for a in agents) + "\n")

templates = json.loads(command(["kubectl", "--request-timeout=15s", "-n", "kagent", "get", "actortemplates", "-o", "json"]))
selected = [{"name": t["metadata"]["name"], "uid": t["metadata"]["uid"], "created": t["metadata"]["creationTimestamp"], "ready": next((c["status"] for c in t.get("status", {}).get("conditions", []) if c["type"] == "Ready"), "Unknown")} for t in templates["items"] if any(t["metadata"]["name"].startswith(a["name"] + "-") for a in agents)]
save("snapshots.json", json.dumps({"captured_at_utc": STAMP, "spec_sha256": digest, "templates": selected}, indent=2) + "\n")
save("snapshots.txt", "Commands: kubectl get modelconfig / sandboxagent / actortemplates -n kagent -o json\nDerived checks: SHA256(canonical ModelConfig spec), revision and prompt marker membership.\n\n" + "ModelConfig spec SHA256:\n" + digest + "\n\n" + "\n".join(f"{a['name']}: revision_has_digest={a['digest_in_revision']}; prompt_has_digest={a['digest_in_prompt_marker']}" for a in agents) + "\n\nCurrent ActorTemplates (name / Ready / creation time):\n" + "\n".join(f"{t['name']}\n  Ready={t['ready']}  created={t['created']}" for t in selected) + "\n")

history = command(["helm", "history", "recsys-global-model-config", "-n", "kagent", "-o", "json"])
save("helm-history.json", history)
args = [str(ROOT / ".venv/bin/pytest"), "-q", "tests/unit/jenkins/test_global_model_config.py", "tests/unit/jenkins/test_cicd_configuration.py", "tests/unit/jenkins/test_detect_changed_components.py", "tests/contract/test_coordinator_agentic_contracts.py", "--disable-warnings"]
save("validation.txt", "Command: .venv/bin/pytest -q " + " ".join(args[2:]) + "\n\n" + command(args) + "\nCommand: helm lint infra/helm/recsys-global-model-config\n\n" + command(["helm", "lint", "infra/helm/recsys-global-model-config"]))
save("capture.json", json.dumps({"captured_at_utc": STAMP, "collector": str(Path(__file__).relative_to(ROOT)), "scope": "Read-only source, Helm rendering, live resource projections and local tests; no inference request."}, indent=2) + "\n")
print("Evidence saved to " + str(OUT))

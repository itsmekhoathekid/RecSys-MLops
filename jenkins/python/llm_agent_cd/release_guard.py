"""Cross-scope guard, called while holding the common Jenkins release lock."""
import argparse
import json
import subprocess

IDLE = {"IDLE", "COMPLETED", "ROLLED_BACK"}
ROUTERS = {"workflow": "recsys-workflow-router", "recommendation": "recsys-ab-router"}


def check(own_scope=None, run=subprocess.check_output):
    for scope, deployment in ROUTERS.items():
        if scope == own_scope:
            continue
        exists = run(["kubectl", "-n", "kagent", "get", "deployment", deployment,
                      "--ignore-not-found", "-o", "name"], text=True, timeout=20).strip()
        if not exists:
            continue  # A scope never provisioned cannot own a release operation.
        # Use the runtime's read-only state credentials; never expose secrets or
        # authorize a deploy from a cached ConfigMap/metric which may be stale.
        code = "import os,json; from jenkins.python.llm_agent_cd.state import StateStore; s,_=StateStore(os.environ['AB_STATE_URI']).read(); print(json.dumps({'phase':s['phase']}))"
        result = run(["kubectl", "-n", "kagent", "exec", "deployment/" + deployment,
                      "--", "python", "-c", code], text=True, timeout=30)
        phase = json.loads(result)["phase"]
        if phase not in IDLE:
            raise RuntimeError(scope + " experiment is " + phase + "; reconcile it before another deployment")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--own-scope", choices=list(ROUTERS))
    check(parser.parse_args().own_scope)

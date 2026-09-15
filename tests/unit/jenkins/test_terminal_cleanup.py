from copy import deepcopy
import json
from pathlib import Path

import pytest

from jenkins.python.llm_agent_cd.cleanup import TerminalCleanup
from jenkins.python.llm_agent_cd.driver import Driver as ProductionDriver


ROOT = Path(__file__).resolve().parents[3]


def release(release_id, llm_id):
    return {"release_id": release_id, "llm_version_id": llm_id}


class Store:
    def __init__(self, value):
        self.value = deepcopy(value)
        self.etag = 0
        self.writes = []

    def read(self):
        return deepcopy(self.value), self.etag

    def write(self, value, etag):
        assert etag == self.etag
        self.etag += 1
        self.value = deepcopy(value)
        self.writes.append(deepcopy(value))
        return self.etag


class Driver:
    scope = "recommendation"

    def __init__(self, inventory, sessions, route_ok=True):
        self.inventory = inventory
        self.sessions = sessions
        self.route_ok = route_ok
        self.routes = []
        self.retired = []

    def recommendation_adapter_inventory(self):
        return deepcopy(self.inventory)

    def retire_terminal_sessions(self, disabled):
        self.disabled = list(disabled)
        return deepcopy(self.sessions)

    def route(self, state, weight):
        self.routes.append((deepcopy(state), weight))
        return "route-clean"

    def verify_route(self, state, weight, revision):
        return self.route_ok and revision == "route-clean"

    def retire_release_capacity(self, releases, protected_llms):
        self.retired.append((list(releases), set(protected_llms)))
        return {
            "adapters": {release_id: "SCALED_TO_ZERO" for release_id in releases},
            "backends": {},
        }


def state(phase="COMPLETED"):
    control = release("a" * 64, "1" * 64)
    candidate = release("b" * 64, "2" * 64)
    stale = release("c" * 64, "3" * 64)
    production = release("d" * 64, "4" * 64)
    return {
        "phase": phase,
        "experiment_id": "rec-test",
        "verified_weight": 100 if phase == "COMPLETED" else 0,
        "champion": candidate if phase == "COMPLETED" else control,
        "previous": control,
        "baseline": control,
        "pending": candidate,
        "disabled": [] if phase == "COMPLETED" else [candidate["release_id"]],
        "releases": {
            item["release_id"]: item
            for item in (control, candidate, stale, production)
        },
        "events": [],
    }


def session_report(*protected):
    return {
        "closed": 7,
        "closed_total": 107,
        "closed_by_reason": {"terminal_test_session": 7},
        "active_sessions": len(protected),
        "unfinished_invocations": 0,
        "protected_release_ids": list(protected),
    }


def test_cleanup_prunes_route_before_scaling_and_protects_production_session():
    value = state()
    production = "d" * 64
    stale = "c" * 64
    driver = Driver(
        {release_id: "rec-ab-" + release_id[:20] for release_id in value["releases"]},
        session_report(production),
    )
    store = Store(value)

    result = TerminalCleanup(store, driver, lambda: 1000).run()

    assert result["status"] == "CLEANED"
    assert result["closed_session_count"] == 107
    assert result["retired_release_ids"] == [stale]
    assert stale not in driver.routes[0][0]["releases"]
    assert driver.retired == [([stale], {"1" * 64, "2" * 64, "4" * 64})]
    assert store.writes[0]["cleanup"]["status"] == "INTENT"
    assert store.writes[-1]["cleanup"]["status"] == "CLEANED"


def test_rollback_retires_quarantined_candidate_not_pending_pointer():
    value = state("ROLLED_BACK")
    candidate = "b" * 64
    control = "a" * 64
    value["releases"] = {
        release_id: value["releases"][release_id]
        for release_id in (control, candidate)
    }
    driver = Driver(
        {
            control: "rec-ab-" + control[:20],
            candidate: "rec-ab-" + candidate[:20],
        },
        session_report(),
    )
    store = Store(value)

    result = TerminalCleanup(store, driver, lambda: 1000).run()

    assert result["retired_release_ids"] == [candidate]
    assert driver.disabled == [candidate]
    assert candidate not in store.value["releases"]
    assert store.value["phase"] == "ROLLED_BACK"


def test_route_verification_failure_never_scales_capacity():
    value = state()
    driver = Driver(
        {release_id: "rec-ab-" + release_id[:20] for release_id in value["releases"]},
        session_report(),
        route_ok=False,
    )
    store = Store(value)

    with pytest.raises(RuntimeError, match="cleanup route"):
        TerminalCleanup(store, driver, lambda: 1000).run()

    assert driver.retired == []
    assert store.value["cleanup"]["status"] == "ROUTING"


def test_cleanup_is_repeatable_without_deleting_evidence():
    value = state("ROLLED_BACK")
    candidate = "b" * 64
    control = "a" * 64
    value["releases"] = {
        release_id: value["releases"][release_id]
        for release_id in (control, candidate)
    }
    driver = Driver(
        {candidate: "rec-ab-" + candidate[:20]},
        session_report(),
    )
    store = Store(value)
    first = TerminalCleanup(store, driver, lambda: 1000).run()
    second = TerminalCleanup(store, driver, lambda: 1001).run()

    assert first["manifests_retained"] and second["evidence_retained"]
    assert second["attempt"] == 2
    assert second["retired_release_ids"] == [candidate]
    assert second["untracked_adapter_release_ids"] == []
    assert candidate in store.value["cleanup_managed_release_ids"]
    assert store.value["phase"] == "ROLLED_BACK"


def test_adapter_inventory_excludes_workflow_and_checks_full_release_identity():
    driver = ProductionDriver.__new__(ProductionDriver)
    driver.scope = "recommendation"
    release_id = "a" * 64
    workflow_release = "b" * 64
    items = [
        {
            "metadata": {
                "name": "rec-ab-" + release_id[:20],
                "labels": {"recsys.ai/owner": "llm-agent-cd"},
            },
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "env": [
                                    {"name": "RELEASE_ID", "value": release_id}
                                ]
                            }
                        ]
                    }
                }
            },
        },
        {
            "metadata": {
                "name": "rec-ab-" + workflow_release[:20],
                "labels": {
                    "recsys.ai/owner": "llm-agent-cd",
                    "recsys.ai/workflow": "workflow",
                },
            },
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "env": [
                                    {
                                        "name": "RELEASE_ID",
                                        "value": workflow_release,
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
        },
    ]
    driver.kube = lambda *args, **kwargs: json.dumps({"items": items})

    assert driver.recommendation_adapter_inventory() == {
        release_id: "rec-ab-" + release_id[:20]
    }


def test_backend_used_by_active_workflow_specialist_is_protected():
    driver = ProductionDriver.__new__(ProductionDriver)
    driver.namespace = "kagent"
    backend = "rec-llm-" + "f" * 20
    modelconfigs = {
        "items": [
            {
                "metadata": {
                    "name": "workflow-context",
                    "labels": {
                        "recsys.ai/owner": "llm-agent-cd",
                        "recsys.ai/workflow": "wf-active",
                    },
                },
                "spec": {
                    "openAI": {
                        "baseUrl": "http://"
                        + backend
                        + ".kagent.svc.cluster.local:8000/v1"
                    }
                },
            }
        ]
    }
    deployments = {
        "items": [
            {
                "metadata": {
                    "name": "workflow-coordinator",
                    "labels": {"recsys.ai/workflow": "wf-active"},
                },
                "spec": {"replicas": 1},
            }
        ]
    }

    def kube(*args, **kwargs):
        return json.dumps(modelconfigs if args[1] == "modelconfigs" else deployments)

    driver.kube = kube
    assert driver._backend_has_live_consumer(backend)


def test_jenkins_runs_only_controller_selected_cleanup_under_common_lock():
    source = (ROOT / "jenkins/LLMAgentCD.Jenkinsfile").read_text()
    assert "'cleanup'" in source.split("choice(name: 'ACTION'", 1)[1].split("\n", 1)[0]
    action = source.split("stage('Execute Controller Action')", 1)[1]
    assert "recommendation_action" in action
    assert "lock(resource: 'recsys-production-release')" in action
    assert "while (" not in source and "sleep(time:" not in source
    post = source.split("  post {", 1)[1]
    assert " cleanup" not in post
    assert "archiveArtifacts" in post


def test_jenkins_provision_refreshes_cleanup_choice_without_dropping_parameters():
    source = (ROOT / "jenkins/python/llm_agent_cd/provision.py").read_text()
    assert "job.getProperty(ParametersDefinitionProperty.class)" in source
    assert "parameters.parameterDefinitions.collect" in source
    assert "new StringParameterDefinition('ACTION_KEY'" in source
    assert "new StringParameterDefinition('EXPECTED_STATE_ETAG'" in source
    assert "['prepare', 'route', 'promote', 'rollback', 'cleanup']" in source
    assert "definition.name == 'ROUTER_IMAGE'" in source
    assert "new String('__IMAGE__'.decodeBase64(), 'UTF-8')" in source

import pytest

from apps.agentic.llm_ab_router.database import Database
from jenkins.python.llm_agent_cd.driver import (
    normalized_live_spec,
    operator_paused_deployment,
    operator_single_replica_deployment,
    contains_spec,
)
from jenkins.python.llm_agent_cd.manifests import resources


def test_omitted_stream_is_false_but_true_is_still_drift():
    desired = {"description": "", "declarative": {"stream": False}}
    assert contains_spec(
        normalized_live_spec("SandboxAgent", {"declarative": {}}), desired
    )
    assert not contains_spec(
        normalized_live_spec("SandboxAgent", {"declarative": {"stream": True}}), desired
    )


def test_modelconfig_renderer_uses_only_upstream_v1alpha2_openai_fields():
    value = {
        "release_id": "a" * 64,
        "config_id": "b" * 64,
        "llm_version_id": "c" * 64,
        "config": {"temperature": "0.0", "maxTokens": 384, "seed": 42},
        "llm": {},
        "agent": {"systemMessage": "native", "tools": []},
        "binding": {
            "model_alias": "model",
            "api_key_secret": "gateway",
            "api_key_secret_key": "API_KEY",
            "backend_url": "http://backend/v1",
            "allowed_domains": ["backend"],
            "worker_pool": "pool",
            "adapter_replicas": 1,
        },
    }
    model = next(
        obj
        for obj in resources(
            value,
            "kagent",
            "registry/router@sha256:" + "d" * 64,
            "runtime-secret",
        )
        if obj["kind"] == "ModelConfig"
    )
    assert model["apiVersion"] == "kagent.dev/v1alpha2"
    assert model["spec"]["openAI"] == {
        "baseUrl": "http://backend/v1",
        "temperature": "0.0",
        "maxTokens": 384,
        "seed": 42,
    }
    assert "apiFormat" not in model["spec"]["openAI"]


@pytest.mark.parametrize("name", ["rec-llm-deadbeef", "rec-ab-deadbeef", "rec-ab-deadbeef-cpu"])
def test_only_owned_capacity_paused_deployments_can_resume(name):
    desired = {"kind": "Deployment", "metadata": {"name": name}, "spec": {"replicas": 1}}
    live = {"metadata": {"labels": {"recsys.ai/owner": "llm-agent-cd"}}, "spec": {"replicas": 0}}
    assert operator_paused_deployment(desired, live)
    live["metadata"]["labels"]["recsys.ai/owner"] = "other"
    assert not operator_paused_deployment(desired, live)


def test_unreviewed_deployment_name_or_nonzero_replica_cannot_resume():
    desired = {"kind": "Deployment", "metadata": {"name": "foreign"}, "spec": {"replicas": 1}}
    live = {"metadata": {"labels": {"recsys.ai/owner": "llm-agent-cd"}}, "spec": {"replicas": 0}}
    assert not operator_paused_deployment(desired, live)


def test_only_exact_immutable_two_to_one_override_is_accepted():
    desired = {
        "kind": "Deployment",
        "metadata": {
            "name": "rec-ab-deadbeef",
            "annotations": {"recsys.ai/immutable-spec": "expected"},
        },
        "spec": {"replicas": 2, "selector": {"matchLabels": {"release": "x"}}},
    }
    live = {
        "metadata": {
            "labels": {"recsys.ai/owner": "llm-agent-cd"},
            "annotations": {"recsys.ai/immutable-spec": "expected"},
        },
        "spec": {"replicas": 1, "selector": {"matchLabels": {"release": "x"}}},
    }
    assert operator_single_replica_deployment(desired, live)
    live["metadata"]["annotations"]["recsys.ai/immutable-spec"] = "other"
    assert not operator_single_replica_deployment(desired, live)
    live["metadata"]["annotations"]["recsys.ai/immutable-spec"] = "expected"
    live["spec"]["selector"]["matchLabels"]["release"] = "drift"
    assert not operator_single_replica_deployment(desired, live)
    desired["metadata"]["name"] = "rec-ab-deadbeef"
    live["spec"]["replicas"] = 1
    assert not operator_paused_deployment(desired, live)


def test_migration_serialized_and_unlocks_on_error(monkeypatch):
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql):
            calls.append(sql)
            if "CREATE SCHEMA" in sql:
                raise RuntimeError("DDL failure")

    db = Database("unused")
    monkeypatch.setattr(db, "connect", Connection)
    with pytest.raises(RuntimeError, match="DDL failure"):
        db.migrate()
    assert "pg_advisory_lock(" in calls[0]
    assert "CREATE SCHEMA" in calls[1]
    assert "pg_advisory_unlock(" in calls[2]

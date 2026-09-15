from copy import deepcopy

import pytest

from jenkins.python.llm_agent_cd.recommendation_prepare import candidate
from jenkins.python.llm_agent_cd.recommendation_baseline_migration import (
    CANONICAL_RECOMMENDATION_PROMPT,
    LEGACY_MISSING_USER_TERMINAL_CONTRACT,
    LEGACY_NATIVE_MISSING_USER_CONTRACT,
    MISSING_USER_TERMINAL_CONTRACT,
    NULL_AND_OUTPUT_CONTRACT,
    migrated,
    reconcile_terminal_route,
)
from jenkins.python.llm_agent_cd.manifests import resources
from tests.unit.jenkins.test_llm_agent_cd import champion as champion_fixture
from tests.unit.jenkins.test_llm_agent_cd import MemoryStore


def test_recommendation_candidate_changes_only_llm_axis(champion_fixture):
    champion = champion_fixture
    llm = deepcopy(champion["llm"])
    llm["artifact_sha256"] = "c" * 64
    value = candidate(champion, llm, "router@sha256:" + "d" * 64)
    assert value["config_id"] == champion["config_id"]
    assert value["llm_version_id"] != champion["llm_version_id"]
    assert value["agent"] == champion["agent"]
    assert value["binding"]["managed_backend"] is True
    assert value["binding"]["backend_url"].endswith(".kagent.svc.cluster.local:8000/v1")
    assert value["binding"]["model_alias"] == champion["binding"]["model_alias"]
    assert value["binding"]["release_schema_version"] == 3
    assert value["binding"]["adapter_replicas"] == 1
    assert value["binding"]["grpc_transport"] is True
    assert value["binding"]["allowed_domains"] == [
        "rec-llm-" + value["llm_version_id"][:20] + ".kagent.svc.cluster.local"
    ]
    adapter = next(item for item in resources(value, "kagent", value["binding"]["adapter_image"], "runtime")
                   if item["kind"] == "Deployment")
    assert adapter["spec"]["replicas"] == 1
    assert "AB_KAGENT_GRPC_TARGET" in {
        item["name"] for item in adapter["spec"]["template"]["spec"]["containers"][0]["env"]
    }


def test_recommendation_candidate_requires_pinned_adapter(champion_fixture):
    with pytest.raises(ValueError, match="digest pinned"):
        candidate(champion_fixture, champion_fixture["llm"], "router:latest")


def test_recommendation_baseline_migration_freezes_null_and_structured_output(champion_fixture):
    value = migrated(champion_fixture, "router@sha256:" + "e" * 64)
    assert value["config_id"] == champion_fixture["config_id"]
    assert value["llm_version_id"] == champion_fixture["llm_version_id"]
    assert value["agent"]["systemMessage"] == CANONICAL_RECOMMENDATION_PROMPT
    assert "Recommend three items. I have not provided a user ID." in value["agent"]["systemMessage"]
    assert "A requested item count is never a user_id" in value["agent"]["systemMessage"]
    assert LEGACY_MISSING_USER_TERMINAL_CONTRACT.strip() not in value["agent"]["systemMessage"]
    assert LEGACY_NATIVE_MISSING_USER_CONTRACT.strip() not in value["agent"]["systemMessage"]
    assert '"questions":[{"question":"Please provide your user_id as an integer."}]' in value["agent"]["systemMessage"]
    assert "Never retry ask_user" in value["agent"]["systemMessage"]
    assert "reconsider the earlier user request" in value["agent"]["systemMessage"]
    assert "common value such as 1 or 1001" in value["agent"]["systemMessage"]
    assert value["binding"]["recommendation_output_profile"] == "trusted-tool-result-v1"
    assert value["binding"]["release_schema_version"] == 2
    assert value["release_id"] != champion_fixture["release_id"]


def test_recommendation_baseline_migration_removes_legacy_native_block_without_trailing_newline(champion_fixture):
    old = deepcopy(champion_fixture)
    old["agent"]["systemMessage"] = (
        old["agent"]["systemMessage"].rstrip()
        + "\n\n"
        + LEGACY_NATIVE_MISSING_USER_CONTRACT.strip()
    )
    for key in ("config_id", "llm_version_id", "release_id"):
        old.pop(key, None)
    value = migrated(old, "router@sha256:" + "f" * 64)
    assert LEGACY_NATIVE_MISSING_USER_CONTRACT.strip() not in value["agent"]["systemMessage"]
    assert value["agent"]["systemMessage"] == CANONICAL_RECOMMENDATION_PROMPT


def test_terminal_route_reconcile_is_narrow_and_cas_persists_revision(champion_fixture):
    store = MemoryStore(champion_fixture)
    store.value.update(
        phase="ROLLED_BACK",
        champion=champion_fixture,
        baseline=champion_fixture,
        pending=champion_fixture,
        verified_weight=0,
        route_revision="stale",
        events=[],
    )

    class Driver:
        def __init__(self):
            self.routed = 0

        def verify_route(self, _state, weight, revision):
            return weight == 0 and revision == "repaired"

        def route(self, _state, weight):
            assert weight == 0
            self.routed += 1
            return "repaired"

    driver = Driver()
    state, etag = reconcile_terminal_route(store, store.value, store.etag, driver)
    assert driver.routed == 1
    assert state["route_revision"] == "repaired"
    assert state["events"][-1]["action"] == "terminal_route_reconciled"
    assert etag == store.etag


def test_terminal_route_reconcile_refuses_mixed_release_pointers(champion_fixture):
    other = deepcopy(champion_fixture)
    other["release_id"] = "f" * 64
    store = MemoryStore(champion_fixture)
    store.value.update(
        phase="ROLLED_BACK",
        champion=champion_fixture,
        baseline=champion_fixture,
        pending=other,
        verified_weight=0,
        route_revision="stale",
    )

    class Driver:
        def verify_route(self, *_args):
            return False

    with pytest.raises(ValueError, match="route is not verified"):
        reconcile_terminal_route(store, store.value, store.etag, Driver())

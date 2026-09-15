from copy import deepcopy
import io
import json
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from jenkins.python.llm_agent_cd.state import StateStore
from jenkins.python.llm_agent_cd.engine import Engine
from jenkins.python.llm_agent_cd.release import DEFAULT_POLICY, digest
from apps.agentic.llm_ab_router.workflow_events import snapshot_events
from tests.unit.jenkins.test_llm_agent_cd import MemoryStore, FakeDriver, ROOT
from tests.unit.jenkins.test_llm_workflow import bundle, champion, candidate


class ObjectClient:
    meta = SimpleNamespace(service_model=SimpleNamespace(operation_model=lambda _: SimpleNamespace(
        input_shape=SimpleNamespace(members={"IfMatch": 1, "IfNoneMatch": 1}))))
    def __init__(self): self.objects = {}
    def get_bucket_versioning(self, **kwargs): return {"Status": "Enabled"}
    def put_object(self, **kwargs):
        if kwargs["Key"] in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
        assert kwargs["IfNoneMatch"] == "*"
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {"ETag": "1"}
    def get_object(self, **kwargs): return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}


def test_archive_create_only_integrity_and_scope():
    client = ObjectClient()
    store = StateStore("s3://bucket/workflow/state.json", client)
    state = {"phase": "COMPLETED", "experiment_id": "wf-old", "history": [{"old": True}], "cases": {"case1": {"verdict": "PASS"}}}
    ref = store.archive(state)
    assert store.archive(state) == ref
    assert len(client.objects) == 1
    assert "history" not in store.read_archive(ref)
    with pytest.raises(ValueError, match="outside scope"):
        store.read_archive({**ref, "key": "recommendation/foreign"})
    client.objects[ref["key"]] = b'{"experiment_id":"wf-old","phase":"COMPLETED","cases":{}}'
    with pytest.raises(ValueError, match="integrity"):
        store.read_archive(ref)
    with pytest.raises(ValueError, match="collision"):
        store.archive(state)
    with pytest.raises(ValueError, match="terminal"):
        store.archive({**state, "phase": "ROLLBACK_FAILED"})


def test_next_experiment_archives_before_reset_and_does_not_leak_gate(bundle):
    store = MemoryStore(bundle)
    old = {**store.value, "phase": "COMPLETED", "experiment_id": "wf-old", "gate_evidence": {"verdict": "PASS"},
           "gate_windows": [{"phase": "VERIFY"}], "events": [{"phase": "COMPLETED"}], "promoted_at": 42,
           "build_url": "old-build", "restored_baseline": True}
    store.value = deepcopy(old)
    archived = []
    def archive(s):
        assert store.value == old  # no new state committed until archive succeeds
        archived.append(deepcopy(s))
        return {"experiment_id": s["experiment_id"], "key": "archive", "checksum": digest(s)}
    store.archive = archive
    e = Engine(store, FakeDriver(), lambda: 1000)
    fixtures = json.loads((ROOT / "configs/llm-ab/workflow-cases.json").read_text())
    e.start(candidate(bundle, True), "config_only", {**DEFAULT_POLICY, "monitor_seconds": 0}, fixtures, "wf-new")
    assert archived == [old]
    assert len(e.state["history"]) == 1
    assert len(e.state["events"]) == 1 and e.state["events"][0]["phase"] == "DEPLOY"
    assert not e.state["gate_evidence"] and not e.state["gate_windows"]
    assert e.state["promoted_at"] is None and e.state["build_url"] == ""
    assert not e.state["restored_baseline"]


def test_archive_failure_does_not_start_candidate(bundle):
    store = MemoryStore(bundle)
    store.value.update(phase="COMPLETED", experiment_id="wf-old")
    before = deepcopy(store.value)
    def fail(s): raise RuntimeError("archive unavailable")
    store.archive = fail
    engine = Engine(store, FakeDriver(), lambda: 1000)
    fixtures = json.loads((ROOT / "configs/llm-ab/workflow-cases.json").read_text())
    with pytest.raises(RuntimeError, match="archive unavailable"):
        engine.start(candidate(bundle, True), "config_only", DEFAULT_POLICY, fixtures, "wf-new")
    assert store.value == before


def test_projection_preserves_all_closed_gate_windows():
    gates = [{"phase": p, "end": i, "verdict": "PASS"} for i, p in enumerate(["CANARY", "AB", "VERIFY"])]
    state = {"experiment_id": "wf-old", "gate_windows": gates, "gate_evidence": gates[-1]}
    events = [e for e in snapshot_events(state) if e["event"] == "ab.gate"]
    assert {e["phase"] for e in events} == {"CANARY", "AB", "VERIFY"}
    assert events[-1]["event_id"] == events[-2]["event_id"]  # HA/restart dedup stable

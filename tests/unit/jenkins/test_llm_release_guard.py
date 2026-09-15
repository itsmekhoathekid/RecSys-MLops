import json
import pytest
from jenkins.python.llm_agent_cd.release_guard import check


def test_unprovisioned_scope_is_safe():
    check(run=lambda *a, **k: "")


@pytest.mark.parametrize("phase", ["CANARY", "AB", "VERIFY", "ROLLBACK_FAILED", "ROUTING"])
def test_any_active_or_ambiguous_peer_blocks(phase):
    def run(args, **kw):
        return "deployment/existing" if "get" in args else json.dumps({"phase": phase})
    with pytest.raises(RuntimeError, match="reconcile"):
        check("workflow", run)


def test_only_other_scope_is_checked():
    calls = []
    def run(args, **kw):
        calls.append(args)
        return "deployment/existing" if "get" in args else '{"phase":"IDLE"}'
    check("workflow", run)
    assert all("recsys-workflow-router" not in " ".join(c) for c in calls)


def test_state_access_failure_is_not_treated_as_idle():
    def run(args, **kw):
        raise OSError("cluster unavailable")
    with pytest.raises(OSError):
        check(run=run)

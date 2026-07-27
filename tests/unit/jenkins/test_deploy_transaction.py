from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
JOURNAL_PATH = ROOT / "jenkins/python/deploy_transaction/journal.py"
SPEC = importlib.util.spec_from_file_location("deploy_transaction_journal", JOURNAL_PATH)
assert SPEC and SPEC.loader
journal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(journal)


def test_transaction_journal_lifecycle_and_reverse_compensation_order(tmp_path):
    path = tmp_path / "tx" / "transaction.json"
    journal.initialize(path, "job-1-api-deadbeef", "api", "deadbeef")
    journal.add_release(path, "recsys-security", "recsys-security", True, "3")
    journal.add_release(path, "recsys-demo-web", "api-serving", False, "")
    journal.add_external(path, "model-store", "/state/model-store.json")
    journal.transition(path, "SNAPSHOT")
    journal.transition(path, "APPLYING")
    journal.transition(path, "VERIFYING")

    payload = journal.load(path)
    assert payload["state"] == "VERIFYING"
    assert [item["release"] for item in journal.iter_records(path, "helm")] == [
        "recsys-demo-web",
        "recsys-security",
    ]
    assert journal.iter_records(path, "external")[0]["kind"] == "model-store"


def test_unfinished_and_rollback_failed_transactions_block_component(tmp_path):
    first = tmp_path / "first" / "transaction.json"
    second = tmp_path / "second" / "transaction.json"
    journal.initialize(first, "first", "training", "abc")
    journal.initialize(second, "second", "api", "def")
    journal.transition(second, "COMMITTED")

    assert journal.find_blocking(tmp_path, "training") == [first]
    assert journal.find_blocking(tmp_path, "api") == []

    journal.transition(first, "ROLLBACK_FAILED")
    assert journal.find_blocking(tmp_path, "training") == [first]


def test_committed_and_rolled_back_transactions_are_terminal(tmp_path):
    committed = tmp_path / "committed" / "transaction.json"
    rolled_back = tmp_path / "rolled-back" / "transaction.json"
    journal.initialize(committed, "committed", "api", "abc")
    journal.transition(committed, "COMMITTED")
    journal.initialize(rolled_back, "rolled-back", "api", "def")
    journal.transition(rolled_back, "ROLLING_BACK")
    journal.transition(rolled_back, "ROLLED_BACK")
    assert journal.find_blocking(tmp_path, "api") == []


def test_journal_contains_no_command_string_field(tmp_path):
    path = tmp_path / "tx" / "transaction.json"
    journal.initialize(path, "tx", "api", "abc")
    journal.add_external(path, "kfp-version", "/state/kfp.json")
    encoded = json.dumps(journal.load(path))
    assert '"command"' not in encoded

from __future__ import annotations

import validate.governance_contracts as governance_contracts
from validate.governance_contracts import build_validation_report, dataset_result, read_redis_payload


class FakeRedis:
    def __init__(self, key_type: str, payload):
        self.key_type = key_type
        self.payload = payload

    def type(self, _key: str):
        return self.key_type

    def get(self, _key: str):
        return self.payload

    def hgetall(self, _key: str):
        return self.payload


def test_read_redis_payload_supports_feast_string_keys():
    assert read_redis_payload(FakeRedis("string", '{"feature": 1}'), "fs:item:1") == '{"feature": 1}'


def test_read_redis_payload_supports_flink_hash_keys():
    payload = {"feature": "1"}
    assert read_redis_payload(FakeRedis(b"hash", payload), "fs:user:1") == payload


def test_local_validation_report_propagates_failure_without_datahub():
    report = build_validation_report(
        "DP2",
        {
            "silver.good": dataset_result([{"status": "SUCCESS"}]),
            "silver.bad": dataset_result([{"status": "FAILURE"}]),
        },
    )
    assert report["status"] == "FAILURE"
    assert report["pipeline"] == "DP2"


def test_validation_cli_returns_failure_exit_code(monkeypatch):
    monkeypatch.setattr(
        governance_contracts,
        "validate_dp3_postgres",
        lambda: {"pipeline": "DP3", "status": "FAILURE", "datasets": {}},
    )
    monkeypatch.setattr("sys.argv", ["governance-contracts", "dp3-postgres"])
    assert governance_contracts.main() == 1

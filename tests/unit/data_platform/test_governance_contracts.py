from __future__ import annotations

from validate.governance_contracts import read_redis_payload


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

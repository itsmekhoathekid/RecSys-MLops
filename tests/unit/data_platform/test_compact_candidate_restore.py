from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path("ops/gcp/restore_candidate_pools.py")
SPEC = importlib.util.spec_from_file_location("restore_candidate_pools", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)

MAX_CANDIDATES_PER_POOL = restore.MAX_CANDIDATES_PER_POOL
candidate_pools = restore.candidate_pools
replace_aggregate_candidate_pools = restore.replace_aggregate_candidate_pools


class FakePipeline:
    def __init__(self) -> None:
        self.calls = []

    def delete(self, *keys):
        self.calls.append(("delete", keys))
        return self

    def zadd(self, key, values):
        self.calls.append(("zadd", key, values))
        return self

    def zremrangebyrank(self, key, start, stop):
        self.calls.append(("trim", key, start, stop))
        return self

    def execute(self):
        self.calls.append(("execute",))


class FakeRedis:
    def __init__(self) -> None:
        self.pipe = FakePipeline()

    def scan_iter(self, *, match):
        return {
            "candidate:popular:*": [b"candidate:popular:global"],
            "candidate:trending:*": ["candidate:trending:1h"],
        }[match]

    def pipeline(self, *, transaction):
        assert transaction is True
        return self.pipe


def test_candidate_recovery_uses_streaming_score_contract_and_replaces_aggregates():
    pools = candidate_pools(
        [
            {
                "product_id": 10,
                "category_id": 2,
                "views_1h": 3,
                "carts_1h": 2,
                "purchases_24h": 1,
                "popularity_score": 42.0,
            },
            {
                "product_id": 11,
                "category_id": 2,
                "views_1h": 1,
                "carts_1h": 0,
                "purchases_24h": 0,
                "popularity_score": 8.0,
            },
        ]
    )

    assert pools["candidate:popular:global"] == {"10": 42.0, "11": 8.0}
    assert pools["candidate:trending:1h"] == {"10": 19.0, "11": 1.0}
    assert pools["candidate:popular:category:2"] == {"10": 42.0, "11": 8.0}

    client = FakeRedis()
    result = replace_aggregate_candidate_pools(client, pools)

    assert result == {"items": 2, "pools": 4, "stale_pools_replaced": 2}
    assert client.pipe.calls[0] == (
        "delete",
        ("candidate:popular:global", "candidate:trending:1h"),
    )
    assert client.pipe.calls[-1] == ("execute",)
    assert all(
        call[3] == -MAX_CANDIDATES_PER_POOL - 1
        for call in client.pipe.calls
        if call[0] == "trim"
    )

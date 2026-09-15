from copy import deepcopy
import pytest
from jenkins.python.llm_agent_cd.driver import envoy_allocation_verified


def fixture(weight):
    destinations = [("control", 100 - weight), ("candidate", weight)]
    expected = {"spec": {"http": [{"name": "allocate-revision", "route": [
        {"destination": {"host": name, "port": {"number": 80}}, "weight": w}
        for name, w in destinations if w]}]}}
    action = {"weighted_clusters": {"clusters": [
        {"name": "outbound|80||" + name, "weight": w} for name, w in destinations if w]}}
    if weight in (0, 100):
        action = {"cluster": "outbound|80||" + ("candidate" if weight else "control")}
    route = {"name": "allocate-revision.0", "match": {"path": "/allocate"}, "route": action}
    dump = {"configs": [{"dynamic_route_configs": [{"route_config": {"virtual_hosts": [{"routes": [route]}]}}]}]}
    return expected, dump, route


@pytest.mark.parametrize("weight", [0, 10, 50, 100])
def test_active_weights(weight):
    expected, dump, route = fixture(weight)
    assert envoy_allocation_verified(dump, expected)


@pytest.mark.parametrize("mutation", ["weight", "cluster", "revision", "path", "retry", "mirror", "duplicate", "total"])
def test_revision_name_alone_cannot_pass(mutation):
    expected, dump, route = fixture(50)
    weighted = route["route"]["weighted_clusters"]
    if mutation == "weight": weighted["clusters"][0]["weight"] = 90
    if mutation == "cluster": weighted["clusters"][0]["name"] = "foreign"
    if mutation == "revision": route["name"] += "-stale"
    if mutation == "path": route["match"]["path"] = "/wrong"
    if mutation == "retry": route["route"]["retry_policy"] = {"num_retries": 1}
    if mutation == "mirror": route["route"]["request_mirror_policies"] = [{"cluster": "foreign"}]
    if mutation == "duplicate": weighted["clusters"].append(deepcopy(weighted["clusters"][0]))
    if mutation == "total": weighted["total_weight"] = 200
    assert not envoy_allocation_verified(dump, expected)

from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace

import pytest
import metadata.sync_datahub_catalog as sync_module
import metadata.datahub_client as client_module
from metadata.datahub_client import (
    DataHubCatalogClient,
    RemoteVerification,
    SyncSummary,
)
from metadata.governance_catalog import (
    ANALYTICS_STAGING_URNS,
    BRONZE_URNS,
    ICEBERG_FEATURE_URNS,
    POSTGRES_FEATURE_URNS,
    RAG_ACTIVE_POINTER_URN,
    RAG_MILVUS_URNS,
    REDIS_FEATURE_URNS,
    SILVER_URNS,
    analytics_product,
    assertion_urn,
    catalog_products,
    dp1_product,
    dp2_product,
    dp3_product,
    validate_catalog,
)
from metadata.sync_datahub_catalog import sync_catalog


def _by_urn(product):
    return {dataset.urn: dataset for dataset in product.datasets}


def test_static_catalog_has_expected_size_and_unique_ownership():
    products = catalog_products()
    assert validate_catalog(products) == {
        "mode": "dataset-only-static",
        "data_products": 5,
        "datasets": 44,
        "lineage_edges": 40,
        "assertions": 44,
        "data_contracts": 44,
        "verified": True,
    }
    urns = [dataset.urn for product in products for dataset in product.datasets]
    assert len(urns) == len(set(urns))
    assert not any(
        "source_postgres" in urn or "dataPlatform:kafka" in urn for urn in urns
    )
    assert all(not dataset.upstreams for dataset in dp1_product().datasets)


def test_all_40_direct_edges_match_the_static_design():
    actual = {
        (upstream, dataset.urn)
        for product in catalog_products()
        for dataset in product.datasets
        for upstream in dataset.upstreams
    }
    expected = {
        (BRONZE_URNS["behavior_events"], SILVER_URNS["clean_behavior_events"]),
        (BRONZE_URNS["behavior_events"], SILVER_URNS["rejected_behavior_events"]),
        (BRONZE_URNS["impressions"], SILVER_URNS["clean_impressions"]),
        (
            BRONZE_URNS["recommendation_requests"],
            SILVER_URNS["clean_recommendation_requests"],
        ),
        (BRONZE_URNS["product_snapshots"], SILVER_URNS["product_scd"]),
        (BRONZE_URNS["products"], SILVER_URNS["product_scd"]),
        (BRONZE_URNS["users"], SILVER_URNS["users"]),
        (BRONZE_URNS["products"], SILVER_URNS["products"]),
        (BRONZE_URNS["user_preferences"], SILVER_URNS["user_preferences"]),
        (
            SILVER_URNS["clean_behavior_events"],
            ICEBERG_FEATURE_URNS["user_sequence_features"],
        ),
        (
            SILVER_URNS["clean_behavior_events"],
            ICEBERG_FEATURE_URNS["user_aggregate_features"],
        ),
        (SILVER_URNS["clean_behavior_events"], ICEBERG_FEATURE_URNS["item_features"]),
        (SILVER_URNS["product_scd"], ICEBERG_FEATURE_URNS["item_features"]),
        (SILVER_URNS["clean_impressions"], ICEBERG_FEATURE_URNS["ml_ranking_labels"]),
        (
            SILVER_URNS["clean_behavior_events"],
            ICEBERG_FEATURE_URNS["ml_ranking_labels"],
        ),
        *{
            (ICEBERG_FEATURE_URNS[source], ICEBERG_FEATURE_URNS["ml_bst_training"])
            for source in (
                "ml_ranking_labels",
                "user_sequence_features",
                "user_aggregate_features",
                "item_features",
            )
        },
        *{
            (ICEBERG_FEATURE_URNS[table], POSTGRES_FEATURE_URNS[table])
            for table in POSTGRES_FEATURE_URNS
        },
        *{
            (POSTGRES_FEATURE_URNS[table], REDIS_FEATURE_URNS[table])
            for table in REDIS_FEATURE_URNS
        },
        *{(urn, RAG_ACTIVE_POINTER_URN) for urn in RAG_MILVUS_URNS.values()},
    }
    products = {product.id: product for product in catalog_products()}
    rag = _by_urn(products["RAG_ITEMS"])
    expected.update(
        (upstream, dataset.urn)
        for dataset in rag.values()
        for upstream in dataset.upstreams
        if dataset.urn != RAG_ACTIVE_POINTER_URN
    )
    expected.update(
        (
            SILVER_URNS[table] if table in SILVER_URNS else BRONZE_URNS[table],
            ANALYTICS_STAGING_URNS[table],
        )
        for table in ANALYTICS_STAGING_URNS
    )
    assert len(expected) == 40
    assert actual == expected


def test_dp2_lineage_matches_spark_transformations():
    datasets = _by_urn(dp2_product())
    assert datasets[SILVER_URNS["clean_behavior_events"]].upstreams == (
        BRONZE_URNS["behavior_events"],
    )
    assert datasets[SILVER_URNS["product_scd"]].upstreams == (
        BRONZE_URNS["product_snapshots"],
        BRONZE_URNS["products"],
    )


def test_dp3_owns_batch_feature_and_feast_lineage():
    datasets = _by_urn(dp3_product())
    assert datasets[ICEBERG_FEATURE_URNS["item_features"]].upstreams == (
        SILVER_URNS["clean_behavior_events"],
        SILVER_URNS["product_scd"],
    )
    assert datasets[POSTGRES_FEATURE_URNS["item_features"]].upstreams == (
        ICEBERG_FEATURE_URNS["item_features"],
    )
    assert datasets[REDIS_FEATURE_URNS["item_features"]].upstreams == (
        POSTGRES_FEATURE_URNS["item_features"],
    )


def test_rag_and_analytics_lineage_are_dataset_only():
    products = {product.id: product for product in catalog_products()}
    rag = _by_urn(products["RAG_ITEMS"])
    assert rag[RAG_ACTIVE_POINTER_URN].upstreams == tuple(RAG_MILVUS_URNS.values())
    analytics = _by_urn(analytics_product())
    assert analytics[ANALYTICS_STAGING_URNS["orders"]].upstreams == (
        BRONZE_URNS["orders"],
    )
    assert analytics[ANALYTICS_STAGING_URNS["products"]].upstreams == (
        SILVER_URNS["products"],
    )


def test_high_level_dataset_upsert_replaces_full_upstream_set():
    class Entities:
        def __init__(self):
            self.items = []

        def upsert(self, entity):
            self.items.append(entity)

    class Client:
        entities = Entities()

    adapter = DataHubCatalogClient.__new__(DataHubCatalogClient)
    adapter._client = Client()
    spec = dp2_product().datasets[0]
    adapter._upsert_dataset(spec, "urn:li:domain:recsys")
    entity = adapter._client.entities.items[-1]
    assert str(entity.urn) == spec.urn
    assert [str(item.dataset) for item in entity.upstreams.upstreams] == list(
        spec.upstreams
    )


def test_sync_output_has_the_stable_dataset_only_shape():
    class Client:
        def sync(self, _products):
            return SyncSummary(
                data_products=5,
                datasets=44,
                lineage_edges=40,
                assertions=44,
                data_contracts=44,
            )

        def verify_remote(self, _products, *, require_results=False):
            assert require_results is False
            return RemoteVerification(
                data_products=5,
                datasets=44,
                lineage_edges=40,
                assertions=44,
                data_contracts=44,
                assertions_with_results=0,
                verified=True,
            )

    assert sync_catalog(Client(), catalog_products()) == {
        "mode": "dataset-only-static",
        "data_products": 5,
        "datasets": 44,
        "lineage_edges": 40,
        "assertions": 44,
        "data_contracts": 44,
        "assertions_with_results": 0,
        "verified": True,
    }


def test_sync_retries_remote_verification_while_search_index_propagates(monkeypatch):
    class Client:
        attempts = 0

        def sync(self, _products):
            return SyncSummary(
                data_products=5,
                datasets=44,
                lineage_edges=40,
                assertions=44,
                data_contracts=44,
            )

        def verify_remote(self, _products):
            self.attempts += 1
            if self.attempts < 3:
                raise RuntimeError("Missing Data Product: ANALYTICS")
            return RemoteVerification(
                data_products=5,
                datasets=44,
                lineage_edges=40,
                assertions=44,
                data_contracts=44,
                assertions_with_results=0,
                verified=True,
            )

    client = Client()
    sleeps = []
    monkeypatch.setattr(sync_module.time, "sleep", sleeps.append)
    assert sync_catalog(client, catalog_products())["verified"] is True
    assert client.attempts == 3
    assert sleeps == [1, 2]


def test_verify_only_checks_remote_without_upserting(monkeypatch):
    class Client:
        synced = False
        closed = False

        def sync(self, _products):
            self.synced = True

        def verify_remote(self, _products, *, require_results=False):
            assert require_results is False
            return RemoteVerification(
                data_products=5,
                datasets=44,
                lineage_edges=40,
                assertions=44,
                data_contracts=44,
                assertions_with_results=0,
                verified=True,
            )

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(
        sync_module,
        "parse_args",
        lambda: Namespace(
            gms_url="http://datahub:8080",
            pushgateway_url="",
            strict=True,
            verify_only=True,
            require_results=False,
        ),
    )
    monkeypatch.setattr(
        sync_module.DataHubCatalogClient, "from_env", lambda _url: client
    )
    monkeypatch.setattr(sync_module, "push_sync_metrics", lambda *_args: None)
    assert sync_module.main() == 0
    assert client.synced is False
    assert client.closed is True


def test_field_type_and_schema_mapping_cover_supported_native_types():
    expected = {
        "ARRAY<STRING>": "ArrayTypeClass",
        "STRUCT<x:INT>": "RecordTypeClass",
        "DATE": "DateTypeClass",
        "TIMESTAMP": "TimeTypeClass",
        "BOOLEAN": "BooleanTypeClass",
        "DECIMAL(18,2)": "NumberTypeClass",
        "BINARY": "BytesTypeClass",
        "STRING": "StringTypeClass",
    }
    for native_type, class_name in expected.items():
        mapped = DataHubCatalogClient._field_type(native_type)
        assert type(mapped.type).__name__ == class_name
    schema = DataHubCatalogClient._schema_metadata(dp1_product().datasets[0])
    assert schema.platform == "urn:li:dataPlatform:iceberg"
    assert schema.primaryKeys


def test_from_env_builds_one_shared_graph_and_sdk_client(monkeypatch):
    captured = {}

    class Graph:
        def __init__(self, config):
            captured["config"] = config

    class SDKClient:
        def __init__(self, graph):
            captured["graph"] = graph

    monkeypatch.setenv("DATAHUB_TOKEN", " token ")
    monkeypatch.setattr(client_module, "DataHubGraph", Graph)
    monkeypatch.setattr(client_module, "DataHubClient", SDKClient)
    adapter = DataHubCatalogClient.from_env("http://datahub:8080/")
    assert captured["config"].server == "http://datahub:8080"
    assert captured["config"].token == "token"
    assert adapter._graph is captured["graph"]


class FakeGraph:
    def __init__(self):
        self.calls = []
        self.entities = {}
        self.existing = set()
        self.aspects = {}
        self.closed = False

    def execute_graphql(self, query, variables):
        self.calls.append((query, variables))
        if "searchAcrossEntities" in query:
            name = variables["input"]["query"]
            entity = self.entities.get(name)
            return {
                "searchAcrossEntities": {
                    "searchResults": [{"entity": entity}] if entity else []
                }
            }
        if "createDomain" in query:
            return {"createDomain": "urn:li:domain:recsys"}
        if "createDataProduct" in query:
            return {"createDataProduct": {"urn": "urn:li:dataProduct:new"}}
        return {}

    def exists(self, urn):
        return urn in self.existing

    def get_aspect(self, urn, _aspect):
        return self.aspects.get(urn)

    def close(self):
        self.closed = True


def _adapter(graph=None):
    adapter = DataHubCatalogClient.__new__(DataHubCatalogClient)
    adapter._graph = graph or FakeGraph()
    adapter._client = SimpleNamespace(
        entities=SimpleNamespace(upsert=lambda _entity: None)
    )
    return adapter


def test_domain_entity_search_and_creation_paths():
    graph = FakeGraph()
    graph.entities[client_module.DOMAIN_NAME] = {
        "urn": "urn:li:domain:existing",
        "properties": {"name": client_module.DOMAIN_NAME},
    }
    adapter = _adapter(graph)
    assert adapter._ensure_domain() == "urn:li:domain:existing"
    graph.entities.clear()
    assert adapter._ensure_domain() == "urn:li:domain:recsys"


def test_tag_and_data_product_upserts_reconcile_exact_resources():
    graph = FakeGraph()
    upserts = []
    adapter = _adapter(graph)
    adapter._client = SimpleNamespace(
        entities=SimpleNamespace(upsert=lambda entity: upserts.append(entity))
    )
    product = dp1_product()
    adapter._upsert_tags((product,))
    assert {str(tag.urn) for tag in upserts} >= {
        "urn:li:tag:DP1",
        "urn:li:tag:BatchPipeline",
    }

    graph.entities[product.name] = {
        "urn": "urn:li:dataProduct:existing",
        "properties": {"name": product.name},
        "domain": {"domain": {"urn": "urn:li:domain:recsys"}},
    }
    urns = tuple(dataset.urn for dataset in product.datasets)
    assert (
        adapter._upsert_data_product(product, "urn:li:domain:recsys", urns)
        == "urn:li:dataProduct:existing"
    )
    assert graph.calls[-1][1]["input"]["resourceUrns"] == list(urns)

    graph.entities.clear()
    assert (
        adapter._upsert_data_product(product, "urn:li:domain:recsys", urns)
        == "urn:li:dataProduct:new"
    )


def test_sync_walks_every_dataset_and_product(monkeypatch):
    adapter = _adapter()
    products = catalog_products()
    datasets = []
    data_products = []
    monkeypatch.setattr(adapter, "_ensure_domain", lambda: "urn:li:domain:recsys")
    monkeypatch.setattr(adapter, "_upsert_tags", lambda value: None)
    monkeypatch.setattr(
        adapter, "_upsert_dataset", lambda spec, _domain: datasets.append(spec.urn)
    )
    monkeypatch.setattr(
        adapter,
        "_upsert_data_product",
        lambda product, _domain, urns: data_products.append((product.id, urns)),
    )
    monkeypatch.setattr(
        adapter, "_upsert_custom_assertion", lambda spec: assertion_urn(spec.urn)
    )
    contracts = []
    monkeypatch.setattr(
        adapter,
        "_upsert_data_contract",
        lambda spec, assertion: contracts.append((spec.urn, assertion)),
    )
    summary = adapter.sync(products)
    assert (summary.data_products, summary.datasets, summary.lineage_edges) == (
        5,
        44,
        40,
    )
    assert len(datasets) == 44
    assert len(data_products) == 5
    assert len(contracts) == 44


def test_remote_verification_success_and_failure(monkeypatch):
    product = dp2_product()
    graph = FakeGraph()
    graph.existing = {dataset.urn for dataset in product.datasets}
    graph.aspects = {
        dataset.urn: SimpleNamespace(
            upstreams=[
                SimpleNamespace(dataset=upstream) for upstream in dataset.upstreams
            ]
        )
        for dataset in product.datasets
    }
    adapter = _adapter(graph)
    monkeypatch.setattr(adapter, "_find_entity", lambda *_args: {"urn": "urn:product"})
    monkeypatch.setattr(
        adapter,
        "_assertion_details",
        lambda urn: {
            "urn": urn,
            "info": {
                "type": "CUSTOM",
                "customAssertion": {
                    "entityUrn": next(
                        dataset.urn
                        for dataset in product.datasets
                        if assertion_urn(dataset.urn) == urn
                    )
                },
            },
        },
    )
    monkeypatch.setattr(adapter, "_latest_assertion_result", lambda _urn: None)
    monkeypatch.setattr(
        adapter,
        "_dataset_contract",
        lambda dataset_urn: f"urn:li:dataContract:{dataset_urn.rsplit(',', 1)[0]}",
    )
    graph.get_aspect = lambda urn, aspect: (
        graph.aspects.get(urn)
        if aspect is client_module.UpstreamLineageClass
        else SimpleNamespace(
            entity=next(
                dataset.urn
                for dataset in product.datasets
                if urn.endswith(dataset.urn.rsplit(",", 1)[0])
            ),
            dataQuality=[
                SimpleNamespace(
                    assertion=assertion_urn(
                        next(
                            dataset.urn
                            for dataset in product.datasets
                            if urn.endswith(dataset.urn.rsplit(",", 1)[0])
                        )
                    )
                )
            ],
        )
    )
    verified = adapter.verify_remote((product,))
    assert verified.verified and verified.lineage_edges == 9
    adapter.close()
    assert graph.closed

    missing = _adapter(FakeGraph())
    monkeypatch.setattr(missing, "_find_entity", lambda *_args: None)
    with pytest.raises(RuntimeError, match="Missing Data Product"):
        missing.verify_remote((product,))


def test_cli_argument_metrics_and_push_contract(monkeypatch):
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://env-gms:8080")
    monkeypatch.setattr("sys.argv", ["sync", "--strict", "--verify-only"])
    args = sync_module.parse_args()
    assert args.gms_url == "http://env-gms:8080"
    assert args.strict and args.verify_only

    samples = sync_module.metric_samples(
        {"verified": True, "datasets": 44, "lineage_edges": 40, "data_products": 5}
    )
    assert len(samples) == 13
    pushed = []
    monkeypatch.setattr(
        sync_module,
        "push_metrics",
        lambda *args, **kwargs: pushed.append((args, kwargs)),
    )
    sync_module.push_sync_metrics({"verified": False}, "http://pushgateway:9091")
    assert pushed[0][1]["gateway_url"] == "http://pushgateway:9091"


@pytest.mark.parametrize(("strict", "expected"), [(False, 0), (True, 1)])
def test_cli_sync_failure_respects_strict_mode(monkeypatch, strict, expected):
    client = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        sync_module,
        "parse_args",
        lambda: Namespace(
            gms_url="http://gms",
            pushgateway_url="",
            strict=strict,
            verify_only=False,
            require_results=False,
        ),
    )
    monkeypatch.setattr(
        sync_module.DataHubCatalogClient, "from_env", lambda _url: client
    )
    monkeypatch.setattr(
        sync_module,
        "sync_catalog",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(sync_module, "push_sync_metrics", lambda *_args: None)
    assert sync_module.main() == expected


def test_cli_sync_success_closes_client_and_pushes_metrics(monkeypatch):
    state = {"closed": False, "pushed": False}
    client = SimpleNamespace(close=lambda: state.update(closed=True))
    monkeypatch.setattr(
        sync_module,
        "parse_args",
        lambda: Namespace(
            gms_url="http://gms",
            pushgateway_url="http://push",
            strict=True,
            verify_only=False,
            require_results=False,
        ),
    )
    monkeypatch.setattr(
        sync_module.DataHubCatalogClient, "from_env", lambda _url: client
    )
    monkeypatch.setattr(
        sync_module,
        "sync_catalog",
        lambda *_args: {"mode": "dataset-only-static", "verified": True},
    )
    monkeypatch.setattr(
        sync_module, "push_sync_metrics", lambda *_args: state.update(pushed=True)
    )
    assert sync_module.main() == 0
    assert state == {"closed": True, "pushed": True}

from __future__ import annotations

from argparse import Namespace

import metadata.sync_datahub_catalog as sync_module
from metadata.datahub_client import DataHubCatalogClient, RemoteVerification, SyncSummary
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
        "verified": True,
    }
    urns = [dataset.urn for product in products for dataset in product.datasets]
    assert len(urns) == len(set(urns))
    assert not any("source_postgres" in urn or "dataPlatform:kafka" in urn for urn in urns)
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
        (BRONZE_URNS["recommendation_requests"], SILVER_URNS["clean_recommendation_requests"]),
        (BRONZE_URNS["product_snapshots"], SILVER_URNS["product_scd"]),
        (BRONZE_URNS["products"], SILVER_URNS["product_scd"]),
        (BRONZE_URNS["users"], SILVER_URNS["users"]),
        (BRONZE_URNS["products"], SILVER_URNS["products"]),
        (BRONZE_URNS["user_preferences"], SILVER_URNS["user_preferences"]),
        (SILVER_URNS["clean_behavior_events"], ICEBERG_FEATURE_URNS["user_sequence_features"]),
        (SILVER_URNS["clean_behavior_events"], ICEBERG_FEATURE_URNS["user_aggregate_features"]),
        (SILVER_URNS["clean_behavior_events"], ICEBERG_FEATURE_URNS["item_features"]),
        (SILVER_URNS["product_scd"], ICEBERG_FEATURE_URNS["item_features"]),
        (SILVER_URNS["clean_impressions"], ICEBERG_FEATURE_URNS["ml_ranking_labels"]),
        (SILVER_URNS["clean_behavior_events"], ICEBERG_FEATURE_URNS["ml_ranking_labels"]),
        *{
            (ICEBERG_FEATURE_URNS[source], ICEBERG_FEATURE_URNS["ml_bst_training"])
            for source in (
                "ml_ranking_labels", "user_sequence_features",
                "user_aggregate_features", "item_features",
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
    assert datasets[SILVER_URNS["clean_behavior_events"]].upstreams == (BRONZE_URNS["behavior_events"],)
    assert datasets[SILVER_URNS["product_scd"]].upstreams == (
        BRONZE_URNS["product_snapshots"], BRONZE_URNS["products"]
    )


def test_dp3_owns_batch_feature_and_feast_lineage():
    datasets = _by_urn(dp3_product())
    assert datasets[ICEBERG_FEATURE_URNS["item_features"]].upstreams == (
        SILVER_URNS["clean_behavior_events"], SILVER_URNS["product_scd"]
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
    assert analytics[ANALYTICS_STAGING_URNS["orders"]].upstreams == (BRONZE_URNS["orders"],)
    assert analytics[ANALYTICS_STAGING_URNS["products"]].upstreams == (SILVER_URNS["products"],)


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
    assert [str(item.dataset) for item in entity.upstreams.upstreams] == list(spec.upstreams)


def test_sync_output_has_the_stable_dataset_only_shape():
    class Client:
        def sync(self, _products):
            return SyncSummary(data_products=5, datasets=44, lineage_edges=40)

        def verify_remote(self, _products):
            return RemoteVerification(
                data_products=5, datasets=44, lineage_edges=40, verified=True
            )

    assert sync_catalog(Client(), catalog_products()) == {
        "mode": "dataset-only-static",
        "data_products": 5,
        "datasets": 44,
        "lineage_edges": 40,
        "verified": True,
    }


def test_verify_only_checks_remote_without_upserting(monkeypatch):
    class Client:
        synced = False
        closed = False

        def sync(self, _products):
            self.synced = True

        def verify_remote(self, _products):
            return RemoteVerification(
                data_products=5, datasets=44, lineage_edges=40, verified=True
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
        ),
    )
    monkeypatch.setattr(
        sync_module.DataHubCatalogClient, "from_env", lambda _url: client
    )
    monkeypatch.setattr(sync_module, "push_sync_metrics", lambda *_args: None)
    assert sync_module.main() == 0
    assert client.synced is False
    assert client.closed is True

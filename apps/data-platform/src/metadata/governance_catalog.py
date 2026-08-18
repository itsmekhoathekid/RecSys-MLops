from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Mapping

from lakehouse.iceberg import RAW_GENERATOR_TABLES, SILVER_LAKEHOUSE_TABLES
from metadata.governance_schemas import (
    FEATURE_PRIMARY_KEYS,
    RAW_PRIMARY_KEYS,
    SILVER_PRIMARY_KEYS,
    SchemaColumn,
    analytics_schema,
    bronze_schema,
    feature_schema,
    rag_schema,
    silver_schema,
)

ENV = "PROD"
FEATURE_TABLES = ("user_sequence_features", "user_aggregate_features", "item_features")
DP3_ICEBERG_TABLES = FEATURE_TABLES + ("ml_ranking_labels", "ml_bst_training")
DP3_POSTGRES_TABLES = FEATURE_TABLES + ("ml_ranking_labels",)
ANALYTICS_SILVER_TABLES = (
    "clean_behavior_events",
    "clean_impressions",
    "clean_recommendation_requests",
    "product_scd",
    "users",
    "products",
)
ANALYTICS_BRONZE_TABLES = ("orders", "order_items")
ASSERTION_NAMESPACE = uuid.UUID("bf296284-5f26-4b62-aa2b-0e3c25bfd495")
DATA_CONTRACT_NAMESPACE = uuid.UUID("a516b8d8-e261-4daa-a522-dbbc787909e6")


@dataclass(frozen=True)
class DatasetContractSpec:
    validation_key: str
    description: str


@dataclass(frozen=True)
class DatasetSpec:
    urn: str
    name: str
    description: str
    schema: tuple[SchemaColumn, ...]
    contract: DatasetContractSpec
    primary_keys: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    upstreams: tuple[str, ...] = ()
    custom_properties: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DataProductSpec:
    id: str
    name: str
    description: str
    tags: tuple[str, ...]
    datasets: tuple[DatasetSpec, ...]


def dataset_urn(platform: str, name: str, env: str = ENV) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{env})"


def dataset_urn_parts(urn: str) -> tuple[str, str, str]:
    match = re.fullmatch(
        r"urn:li:dataset:\(urn:li:dataPlatform:([^,]+),(.+),([^,]+)\)", urn
    )
    if not match:
        raise ValueError(f"Invalid DataHub Dataset URN: {urn}")
    return match.group(1), match.group(2), match.group(3)


def assertion_urn(dataset_urn: str) -> str:
    dataset_urn_parts(dataset_urn)
    return f"urn:li:assertion:{uuid.uuid5(ASSERTION_NAMESPACE, dataset_urn)}"


def fallback_contract_urn(dataset_urn: str) -> str:
    dataset_urn_parts(dataset_urn)
    return f"urn:li:dataContract:{uuid.uuid5(DATA_CONTRACT_NAMESPACE, dataset_urn)}"


def bronze_urn(table: str) -> str:
    return dataset_urn("iceberg", f"recsys.lakehouse.bronze_{table}")


def silver_urn(table: str) -> str:
    return dataset_urn("iceberg", f"recsys.lakehouse.silver_{table}")


def iceberg_feature_urn(table: str) -> str:
    return dataset_urn("iceberg", f"recsys_features.feature_store.{table}")


def postgres_feature_urn(table: str) -> str:
    return dataset_urn("postgres", f"feature-postgres.feature_store.{table}")


def redis_feature_urn(table: str) -> str:
    return dataset_urn(
        "redis", f"redis://redis.recsys-dataflow.svc.cluster.local:6379/{table}"
    )


def rag_s3_urn(path: str) -> str:
    return dataset_urn("s3", f"recsys-lakehouse/{path}")


def analytics_staging_urn(table: str) -> str:
    return dataset_urn("iceberg", f"analytics.staging.{table}")


BRONZE_URNS = {table: bronze_urn(table) for table in RAW_GENERATOR_TABLES}
SILVER_URNS = {table: silver_urn(table) for table in SILVER_LAKEHOUSE_TABLES}
ICEBERG_FEATURE_URNS = {
    table: iceberg_feature_urn(table) for table in DP3_ICEBERG_TABLES
}
POSTGRES_FEATURE_URNS = {
    table: postgres_feature_urn(table) for table in DP3_POSTGRES_TABLES
}
REDIS_FEATURE_URNS = {table: redis_feature_urn(table) for table in FEATURE_TABLES}
ANALYTICS_STAGING_URNS = {
    table: analytics_staging_urn(table)
    for table in ANALYTICS_SILVER_TABLES + ANALYTICS_BRONZE_TABLES
}
RAG_RAW_DOCUMENTS_URN = rag_s3_urn("raw/rag_item_documents")
RAG_SILVER_CHUNKS_URN = rag_s3_urn("silver/rag_item_chunks")
RAG_GOLD_EMBEDDINGS_URN = rag_s3_urn("gold/rag_item_embeddings")
RAG_ACTIVE_POINTER_URN = rag_s3_urn("gold/rag_item_embeddings/_active")
RAG_MILVUS_URNS = {
    slot: dataset_urn("milvus", f"recsys_rag.rag_item_chunks_{slot}")
    for slot in ("blue", "green")
}

VALIDATION_KEYS = {
    **{urn: f"bronze.{table}" for table, urn in BRONZE_URNS.items()},
    **{urn: f"silver.{table}" for table, urn in SILVER_URNS.items()},
    **{
        urn: f"iceberg.feature_store.{table}"
        for table, urn in ICEBERG_FEATURE_URNS.items()
    },
    **{
        urn: f"postgres.feature_store.{table}"
        for table, urn in POSTGRES_FEATURE_URNS.items()
    },
    **{urn: f"redis.{table}" for table, urn in REDIS_FEATURE_URNS.items()},
    **{
        urn: f"analytics.staging.{table}"
        for table, urn in ANALYTICS_STAGING_URNS.items()
    },
    RAG_RAW_DOCUMENTS_URN: "rag.raw_documents",
    RAG_SILVER_CHUNKS_URN: "rag.silver_chunks",
    RAG_GOLD_EMBEDDINGS_URN: "rag.gold_embeddings",
    RAG_MILVUS_URNS["blue"]: "rag.milvus.blue",
    RAG_MILVUS_URNS["green"]: "rag.milvus.green",
    RAG_ACTIVE_POINTER_URN: "rag.active_pointer",
}


def _dataset(
    urn: str,
    name: str,
    description: str,
    product: str,
    schema: tuple[SchemaColumn, ...],
    *,
    primary_keys: tuple[str, ...] = (),
    upstreams: tuple[str, ...] = (),
    extra_tags: tuple[str, ...] = (),
    custom_properties: Mapping[str, str] | None = None,
) -> DatasetSpec:
    return DatasetSpec(
        urn=urn,
        name=name,
        description=description,
        schema=schema,
        primary_keys=primary_keys,
        tags=(product, "BatchPipeline", *extra_tags),
        upstreams=upstreams,
        custom_properties={"data_product": product, **dict(custom_properties or {})},
        contract=DatasetContractSpec(
            validation_key=VALIDATION_KEYS[urn],
            description=f"Aggregate local data-quality validation for {name}.",
        ),
    )


def dp1_product() -> DataProductSpec:
    datasets = tuple(
        _dataset(
            BRONZE_URNS[table],
            f"recsys.lakehouse.bronze_{table}",
            "Canonical DP1 Bronze Iceberg table with ingestion audit metadata.",
            "DP1",
            bronze_schema(table),
            primary_keys=("source_run_id",) + RAW_PRIMARY_KEYS[table],
        )
        for table in RAW_GENERATOR_TABLES
    )
    return DataProductSpec(
        "DP1",
        "DP1 Batch Bronze Lakehouse",
        "Batch generator output persisted as canonical Bronze Iceberg datasets.",
        ("DP1", "BatchPipeline"),
        datasets,
    )


def dp2_product() -> DataProductSpec:
    upstream_tables = {
        "clean_behavior_events": ("behavior_events",),
        "rejected_behavior_events": ("behavior_events",),
        "clean_impressions": ("impressions",),
        "clean_recommendation_requests": ("recommendation_requests",),
        "product_scd": ("product_snapshots", "products"),
        "users": ("users",),
        "products": ("products",),
        "user_preferences": ("user_preferences",),
    }
    datasets = tuple(
        _dataset(
            SILVER_URNS[table],
            f"recsys.lakehouse.silver_{table}",
            "Curated DP2 Silver Iceberg dataset produced from canonical Bronze inputs.",
            "DP2",
            silver_schema(table),
            primary_keys=SILVER_PRIMARY_KEYS[table],
            upstreams=tuple(BRONZE_URNS[item] for item in upstream_tables[table]),
        )
        for table in SILVER_LAKEHOUSE_TABLES
    )
    return DataProductSpec(
        "DP2",
        "DP2 Curated Silver Lakehouse",
        "Normalized and deduplicated Silver Iceberg datasets.",
        ("DP2", "BatchPipeline"),
        datasets,
    )


def dp3_product() -> DataProductSpec:
    silver_upstreams = {
        "user_sequence_features": ("clean_behavior_events",),
        "user_aggregate_features": ("clean_behavior_events",),
        "item_features": ("clean_behavior_events", "product_scd"),
        "ml_ranking_labels": ("clean_impressions", "clean_behavior_events"),
    }
    iceberg: list[DatasetSpec] = []
    for table in DP3_ICEBERG_TABLES:
        if table == "ml_bst_training":
            upstreams = tuple(
                ICEBERG_FEATURE_URNS[item]
                for item in (
                    "ml_ranking_labels",
                    "user_sequence_features",
                    "user_aggregate_features",
                    "item_features",
                )
            )
        else:
            upstreams = tuple(SILVER_URNS[item] for item in silver_upstreams[table])
        iceberg.append(
            _dataset(
                ICEBERG_FEATURE_URNS[table],
                f"recsys_features.feature_store.{table}",
                "DP3 batch feature dataset stored in Iceberg.",
                "DP3",
                feature_schema(table),
                primary_keys=FEATURE_PRIMARY_KEYS[table],
                upstreams=upstreams,
            )
        )
    postgres = tuple(
        _dataset(
            POSTGRES_FEATURE_URNS[table],
            f"feature-postgres.feature_store.{table}",
            "Feast PostgreSQL offline dataset exported from the matching Iceberg feature.",
            "DP3",
            feature_schema(table),
            primary_keys=FEATURE_PRIMARY_KEYS[table],
            upstreams=(ICEBERG_FEATURE_URNS[table],),
        )
        for table in DP3_POSTGRES_TABLES
    )
    redis = tuple(
        _dataset(
            REDIS_FEATURE_URNS[table],
            f"redis_online.{table}",
            "Feast Redis online feature dataset materialized from PostgreSQL.",
            "DP3",
            feature_schema(table),
            primary_keys=FEATURE_PRIMARY_KEYS[table][:1],
            upstreams=(POSTGRES_FEATURE_URNS[table],),
        )
        for table in FEATURE_TABLES
    )
    return DataProductSpec(
        "DP3",
        "DP3 Batch Feature Store",
        "Silver data transformed into Iceberg, PostgreSQL, and Redis feature datasets.",
        ("DP3", "BatchPipeline"),
        tuple(iceberg) + postgres + redis,
    )


def rag_product() -> DataProductSpec:
    common = {
        "embedding_model": "intfloat/multilingual-e5-small",
        "embedding_dimension": "384",
    }
    datasets = (
        _dataset(
            RAG_RAW_DOCUMENTS_URN,
            "recsys-lakehouse.raw.rag_item_documents",
            "Canonical generated item documents.",
            "RAG_ITEMS",
            rag_schema("raw"),
            primary_keys=("item_id",),
            custom_properties=common,
        ),
        _dataset(
            RAG_SILVER_CHUNKS_URN,
            "recsys-lakehouse.silver.rag_item_chunks",
            "Structure-aware semantic item chunks.",
            "RAG_ITEMS",
            rag_schema("silver"),
            primary_keys=("chunk_id",),
            upstreams=(RAG_RAW_DOCUMENTS_URN,),
            extra_tags=("SemanticChunking",),
            custom_properties=common,
        ),
        _dataset(
            RAG_GOLD_EMBEDDINGS_URN,
            "recsys-lakehouse.gold.rag_item_embeddings",
            "Normalized multilingual item embeddings.",
            "RAG_ITEMS",
            rag_schema("gold"),
            primary_keys=("chunk_id",),
            upstreams=(RAG_SILVER_CHUNKS_URN,),
            extra_tags=("Embedding",),
            custom_properties=common,
        ),
        *tuple(
            _dataset(
                urn,
                f"recsys_rag.rag_item_chunks_{slot}",
                f"Milvus {slot} collection used for atomic index promotion.",
                "RAG_ITEMS",
                rag_schema("milvus"),
                primary_keys=("chunk_id",),
                upstreams=(RAG_GOLD_EMBEDDINGS_URN,),
                extra_tags=("VectorIndex",),
                custom_properties={**common, "slot": slot},
            )
            for slot, urn in RAG_MILVUS_URNS.items()
        ),
        _dataset(
            RAG_ACTIVE_POINTER_URN,
            "recsys-lakehouse.gold.rag_item_embeddings._active",
            "Atomic pointer selecting the active Milvus index slot.",
            "RAG_ITEMS",
            rag_schema("pointer"),
            primary_keys=("active_slot",),
            upstreams=tuple(RAG_MILVUS_URNS.values()),
            extra_tags=("VectorIndex",),
            custom_properties=common,
        ),
    )
    return DataProductSpec(
        "RAG_ITEMS",
        "RAG Item Index",
        "Item documents, chunks, embeddings, and blue/green Milvus indexes.",
        ("RAG_ITEMS", "BatchPipeline"),
        datasets,
    )


def analytics_product() -> DataProductSpec:
    datasets = []
    for table in ANALYTICS_SILVER_TABLES + ANALYTICS_BRONZE_TABLES:
        source = (
            SILVER_URNS[table]
            if table in ANALYTICS_SILVER_TABLES
            else BRONZE_URNS[table]
        )
        datasets.append(
            _dataset(
                ANALYTICS_STAGING_URNS[table],
                f"analytics.staging.{table}",
                "Analytics staging copy of the canonical lakehouse dataset.",
                "ANALYTICS",
                analytics_schema(table),
                upstreams=(source,),
            )
        )
    return DataProductSpec(
        "ANALYTICS",
        "Analytics Staging",
        "Batch-synchronized lakehouse datasets exposed to analytics workloads.",
        ("ANALYTICS", "BatchPipeline"),
        tuple(datasets),
    )


def catalog_products() -> tuple[DataProductSpec, ...]:
    return (
        dp1_product(),
        dp2_product(),
        dp3_product(),
        rag_product(),
        analytics_product(),
    )


def catalog_contracts(
    products: tuple[DataProductSpec, ...] | None = None,
) -> tuple[tuple[DatasetSpec, DatasetContractSpec], ...]:
    selected = products or catalog_products()
    return tuple(
        (dataset, dataset.contract)
        for product in selected
        for dataset in product.datasets
    )


def validate_catalog(
    products: tuple[DataProductSpec, ...] | None = None,
) -> dict[str, object]:
    products = products or catalog_products()
    datasets = [dataset for product in products for dataset in product.datasets]
    urns = {dataset.urn for dataset in datasets}
    contracts = catalog_contracts(products)
    validation_keys = [contract.validation_key for _, contract in contracts]
    errors: list[str] = []
    if len(urns) != len(datasets):
        errors.append("Dataset URNs must have exactly one Data Product owner")
    if len(validation_keys) != len(set(validation_keys)):
        errors.append("Dataset validation keys must be unique")
    for dataset in datasets:
        try:
            _, _, env = dataset_urn_parts(dataset.urn)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if env != ENV:
            errors.append(f"Dataset environment must be {ENV}: {dataset.urn}")
        missing = set(dataset.upstreams).difference(urns)
        if missing:
            errors.append(f"Unknown upstreams for {dataset.urn}: {sorted(missing)}")
        field_names = {column.name for column in dataset.schema}
        missing_keys = set(dataset.primary_keys).difference(field_names)
        if missing_keys:
            errors.append(
                f"Unknown primary keys for {dataset.urn}: {sorted(missing_keys)}"
            )
    if errors:
        raise RuntimeError("Catalog verification failed: " + " | ".join(errors))
    return {
        "mode": "dataset-only-static",
        "data_products": len(products),
        "datasets": len(datasets),
        "lineage_edges": sum(len(dataset.upstreams) for dataset in datasets),
        "assertions": len(contracts),
        "data_contracts": len(contracts),
        "verified": True,
    }

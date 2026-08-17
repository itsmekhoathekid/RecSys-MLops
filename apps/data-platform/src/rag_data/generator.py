"""Generate canonical raw-zone item documents from PostgreSQL products.

Each product is isolated: deterministic catalog fields are composed locally,
only textual fields come from the provider, and per-item failures are persisted.
The run checkpoints to MinIO and resumes by stable integer item ID.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, Sequence

from rag_data.catalog_mapping import CATALOG_MAPPING_VERSION, CatalogMapping
from rag_data.contracts import (
    CanonicalItemDocument,
    CanonicalReview,
    FailureRecord,
    GeneratedItemContent,
    ReviewsAndQna,
    RunManifest,
    UnstructuredText,
)
from rag_data.orcarouter_client import OrcaRouterClient
from rag_data.prompts import PROMPT_VERSION, user_prompt
from rag_data.storage import MinioRunStorage, RunState


@dataclass(frozen=True)
class ProductRow:
    """Typed projection of one active row from the source products table."""

    product_id: int
    product_name: str
    category_id: int
    category_code: str
    brand_id: int
    brand_name: str
    current_price: Decimal
    is_active: bool
    updated_ts: datetime


class ProductSource(Protocol):
    """Port for deterministic, ordered active-product reads."""

    def fetch_products(
        self, *, item_ids: Sequence[int] | None = None, limit: int = 0
    ) -> list[ProductRow]:
        """Return selected active products, with zero limit meaning all rows."""

        ...


class ContentGenerator(Protocol):
    """Port for a provider that returns schema-validated generated content."""

    model: str

    def generate(self, prompt: str) -> GeneratedItemContent:
        """Return provider output only after strict contract validation."""

        ...


@dataclass(frozen=True)
class GenerationOutcome:
    """One successful canonical document or serializable failure record."""

    result: CanonicalItemDocument | FailureRecord
    finish_reason: str | None = None


class PostgresProductSource:
    """Read-only PostgreSQL adapter for active product catalog rows."""

    SELECT_COLUMNS = """
        SELECT
            product_id,
            product_name,
            category_id,
            category_code,
            brand_id,
            brand_name,
            current_price,
            is_active,
            updated_ts
        FROM products
        WHERE is_active IS TRUE
    """

    def __init__(self, conninfo: str | None = None) -> None:
        self.conninfo = conninfo or self.conninfo_from_env()

    @staticmethod
    def conninfo_from_env() -> str:
        """Build a libpq connection string from secret-backed environment values."""

        return (
            f"host={os.getenv('POSTGRES_HOST', 'source-postgres')} "
            f"port={os.getenv('POSTGRES_PORT', '5432')} "
            f"dbname={os.getenv('POSTGRES_DB', 'recsys')} "
            f"user={os.getenv('POSTGRES_USER', 'recsys')} "
            f"password={os.getenv('POSTGRES_PASSWORD', 'recsys')} "
            f"connect_timeout={os.getenv('POSTGRES_CONNECT_TIMEOUT_SECONDS', '10')}"
        )

    def fetch_products(
        self, *, item_ids: Sequence[int] | None = None, limit: int = 0
    ) -> list[ProductRow]:
        """Fetch selected active products in stable ID order without mutations."""

        import psycopg
        from psycopg.rows import dict_row

        if limit < 0:
            raise ValueError("limit must be 0 or greater")
        query = self.SELECT_COLUMNS
        parameters: list[Any] = []
        if item_ids:
            query += " AND product_id = ANY(%s)"
            parameters.append(list(dict.fromkeys(item_ids)))
        query += " ORDER BY product_id"
        if limit:
            query += " LIMIT %s"
            parameters.append(limit)

        with psycopg.connect(self.conninfo, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, parameters)
                return [ProductRow(**row) for row in cursor.fetchall()]


def compose_document(
    product: ProductRow,
    generated: GeneratedItemContent,
    mapping: CatalogMapping,
) -> CanonicalItemDocument:
    """Merge validated generated text with deterministic, trusted metadata."""

    item_id = product.product_id
    structured = mapping.structured_metadata(
        item_id=item_id,
        brand_id=product.brand_id,
        category_id=product.category_id,
        current_price=product.current_price,
        is_active=product.is_active,
    )
    review_ratings = mapping.review_ratings(item_id)
    reviews = [
        CanonicalReview(
            review_id=f"rev_{item_id}_{index:02d}",
            rating=review_ratings[index - 1],
            content=review.content,
            sentiment_aspects=review.sentiment_aspects,
        )
        for index, review in enumerate(generated.reviews, start=1)
    ]
    return CanonicalItemDocument(
        item_id=item_id,
        sku=mapping.sku(item_id, product.brand_id, product.category_id),
        structured_metadata=structured,
        unstructured_text=UnstructuredText(
            title=generated.title,
            description=generated.description,
            specifications=generated.specifications,
            usage_instructions=generated.usage_instructions,
        ),
        reviews_and_qna=ReviewsAndQna(
            average_rating=mapping.average_rating(item_id),
            total_reviews=mapping.total_reviews(item_id),
            sample_reviews=reviews,
            qna_pairs=generated.qna_pairs,
        ),
    )


class ItemMetadataGenerator:
    """Orchestrate resumable per-item generation and MinIO checkpoints."""

    def __init__(
        self,
        *,
        source: ProductSource,
        content_generator: ContentGenerator,
        mapping: CatalogMapping,
        storage: MinioRunStorage,
        checkpoint_every: int = 10,
        workers: int = 1,
    ) -> None:
        if checkpoint_every < 1:
            raise ValueError("checkpoint_every must be at least 1")
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.source = source
        self.content_generator = content_generator
        self.mapping = mapping
        self.storage = storage
        self.checkpoint_every = checkpoint_every
        self.workers = workers

    def _generate_product(self, product: ProductRow) -> GenerationOutcome:
        try:
            brand = self.mapping.brand(product.brand_id)
            category_path = self.mapping.category_path(product.category_id)
            generated = self.content_generator.generate(
                user_prompt(
                    item_id=product.product_id,
                    source_product_name=product.product_name,
                    brand=brand,
                    category_path=category_path,
                    current_price=product.current_price,
                )
            )
            return GenerationOutcome(
                result=compose_document(product, generated, self.mapping),
                finish_reason=generated.finish_reason,
            )
        except Exception as exc:  # Per-item isolation is part of the job contract.
            finish_reason = getattr(exc, "finish_reason", None)
            return GenerationOutcome(
                result=FailureRecord(
                    item_id=product.product_id,
                    error_type=type(exc).__name__,
                    message=str(exc)[:2000],
                    attempts=max(1, int(getattr(exc, "attempts", 1))),
                    finish_reason=finish_reason,
                ),
                finish_reason=finish_reason,
            )

    def _record_result(
        self,
        state: RunState,
        outcome: GenerationOutcome,
    ) -> None:
        result = outcome.result
        if isinstance(result, CanonicalItemDocument):
            self.storage.add_items(state, [result])
            finish_reason = outcome.finish_reason or "unknown"
            counts = dict(state.manifest.finish_reason_counts)
            counts[finish_reason] = counts.get(finish_reason, 0) + 1
            state.manifest = state.manifest.refreshed(finish_reason_counts=counts)
        else:
            self.storage.add_failures(state, [result])

    def _manifest(self) -> RunManifest:
        return RunManifest(
            run_id=self.storage.run_id,
            model=self.content_generator.model,
            prompt_version=PROMPT_VERSION,
            catalog_mapping_version=CATALOG_MAPPING_VERSION,
        )

    def run(
        self,
        *,
        item_ids: Sequence[int] | None = None,
        limit: int = 0,
        force: bool = False,
    ) -> RunState:
        """Generate pending items, checkpoint progress, and finalize run status.

        Reusing a run ID skips completed items. A complete run is protected unless
        ``force`` is set; remaining item failures produce a partial state.
        """

        products = self.source.fetch_products(item_ids=item_ids, limit=limit)
        state = self.storage.start(manifest=self._manifest(), force=force)
        target_ids = {product.product_id for product in products}
        state.manifest = state.manifest.refreshed(source_count=len(products))
        processed_since_checkpoint = 0

        pending_products = [
            product
            for product in products
            if product.product_id not in state.completed_item_ids
        ]
        if self.workers == 1:
            results = (self._generate_product(product) for product in pending_products)
            executor = None
        else:
            executor = ThreadPoolExecutor(
                max_workers=self.workers, thread_name_prefix="rag-item"
            )
            futures = [
                executor.submit(self._generate_product, product)
                for product in pending_products
            ]
            results = (future.result() for future in as_completed(futures))

        for result in results:
            self._record_result(state, result)
            processed_since_checkpoint += 1
            if processed_since_checkpoint >= self.checkpoint_every:
                state.manifest = state.manifest.refreshed(
                    generated_count=len(state.items),
                    failed_count=len(state.failures),
                    status="running",
                )
                self.storage.checkpoint(state)
                processed_since_checkpoint = 0

        if executor is not None:
            executor.shutdown(wait=True)

        status = (
            "complete"
            if not state.failures and set(state.items) == target_ids
            else "partial"
        )
        state.manifest = state.manifest.refreshed(
            status=status,
            generated_count=len(state.items),
            failed_count=len(state.failures),
        )
        self.storage.checkpoint(state)
        return state


def default_generator_client(**kwargs: Any) -> OrcaRouterClient:
    """Construct the production OrcaRouter content generator adapter."""

    return OrcaRouterClient(**kwargs)

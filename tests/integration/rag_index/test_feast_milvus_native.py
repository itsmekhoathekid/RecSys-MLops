from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
from feast import FeatureStore

from rag_data.feast_publisher import FeastMilvusPublisher


def test_feast_native_upsert_and_cosine_retrieval(tmp_path: Path):
    """Exercise the Feast blue view against Lite locally or Milvus 2.6 in CI."""

    from rag_feature_definitions import chunk, rag_item_chunks_blue

    repo = tmp_path / "feature_repo"
    repo.mkdir()
    remote_uri = os.getenv("RAG_TEST_MILVUS_URI", "")
    if remote_uri:
        parsed = urlsplit(remote_uri)
        online_store = f"""host: {parsed.scheme}://{parsed.hostname}
  port: {parsed.port}
"""
        publisher_uri = remote_uri
    else:
        online_store = f"path: {tmp_path / 'milvus.db'}\n"
        publisher_uri = str(tmp_path / "milvus.db")
    (repo / "feature_store.yaml").write_text(
        f"""project: recsys_rag
provider: local
registry: {tmp_path / 'registry.db'}
offline_store:
  type: file
online_store:
  type: milvus
  {online_store.rstrip()}
  vector_enabled: true
  embedding_dim: 384
  index_type: FLAT
  metric_type: COSINE
entity_key_serialization_version: 3
""",
        encoding="utf-8",
    )
    store = FeatureStore(repo_path=str(repo))
    store.apply([chunk, rag_item_chunks_blue])
    rows = []
    for item_id, score in ((1, 1.0), (2, 0.8)):
        vector = [0.0] * 384
        vector[0] = score
        vector[1] = (1.0 - score * score) ** 0.5
        rows.append(
            {
                "chunk_id": f"{item_id}:review:r:0",
                "embedding": vector,
                "item_id": item_id,
                "chunk_type": "review",
                "source_key": "r",
                "text": f"item {item_id}",
                "brand": "Sony",
                "category_l1": "Điện tử",
                "category_l2": "Âm thanh",
                "category_l3": "Tai nghe",
                "current_price": 20.99,
                "in_stock": True,
                "average_rating": 4.7,
                "content_hash": "sha256:a",
                "item_content_hash": "sha256:b",
                "source_run_id": "ci",
                "event_timestamp": datetime.now(timezone.utc),
            }
        )
    store.write_to_online_store("rag_item_chunks_blue", pd.DataFrame(rows))
    result = store.retrieve_online_documents_v2(
        features=[
            "rag_item_chunks_blue:embedding",
            "rag_item_chunks_blue:item_id",
            "rag_item_chunks_blue:text",
        ],
        query=[1.0] + [0.0] * 383,
        top_k=2,
        distance_metric="COSINE",
    ).to_dict()
    assert result["chunk_id"] == ["1:review:r:0", "2:review:r:0"]
    assert [int(value) for value in result["item_id"]] == [1, 2]
    assert result["distance"][0] > result["distance"][1]

    publisher = FeastMilvusPublisher(
        repo_path=repo,
        milvus_uri=publisher_uri,
        milvus_token="",
    )
    assert publisher.collection_count("blue") == 2
    assert publisher.collection_ids("blue") == {"1:review:r:0", "2:review:r:0"}
    assert publisher.smoke_search("blue", [1.0] + [0.0] * 383)

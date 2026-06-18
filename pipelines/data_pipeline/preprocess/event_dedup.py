from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class DedupResult:
    clean: pd.DataFrame
    rejected: pd.DataFrame
    exact_duplicate_count: int
    conflicting_duplicate_count: int


def deduplicate_behavior_events(events: pd.DataFrame) -> DedupResult:
    """Deduplicate behavior events by event_id.

    Exact duplicates share both event_id and payload_hash. Conflicting
    duplicates share event_id but disagree on payload_hash; the latest
    ingestion_ts row is retained and the rest are rejected.
    """
    if events.empty:
        return DedupResult(events.copy(), events.copy(), 0, 0)
    required = {"event_id", "payload_hash", "ingestion_ts"}
    missing = required - set(events.columns)
    if missing:
        raise ValueError(f"behavior_events missing required columns: {sorted(missing)}")

    frame = events.copy()
    frame["_row_number"] = range(len(frame))
    frame["_ingestion_sort"] = pd.to_datetime(frame["ingestion_ts"], utc=True)

    duplicate_mask = frame.duplicated("event_id", keep=False)
    duplicate_groups = frame.loc[duplicate_mask].groupby("event_id", dropna=False)
    exact_duplicate_count = 0
    conflicting_duplicate_count = 0
    rejected_indexes: set[int] = set()
    kept_indexes: set[int] = set()

    for _, group in duplicate_groups:
        hashes = set(group["payload_hash"].astype(str))
        sorted_group = group.sort_values(
            ["_ingestion_sort", "_row_number"], ascending=[False, False]
        )
        keep_index = int(sorted_group.index[0])
        kept_indexes.add(keep_index)
        rejected_indexes.update(int(index) for index in sorted_group.index[1:])
        if len(hashes) == 1:
            exact_duplicate_count += len(group) - 1
        else:
            conflicting_duplicate_count += len(group) - 1

    clean = frame.drop(index=list(rejected_indexes)).drop(
        columns=["_row_number", "_ingestion_sort"]
    )
    rejected = frame.loc[sorted(rejected_indexes)].drop(
        columns=["_row_number", "_ingestion_sort"]
    )
    return DedupResult(
        clean=clean.reset_index(drop=True),
        rejected=rejected.reset_index(drop=True),
        exact_duplicate_count=exact_duplicate_count,
        conflicting_duplicate_count=conflicting_duplicate_count,
    )


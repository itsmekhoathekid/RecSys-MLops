from __future__ import annotations

import pandas as pd

from .data_quality_checks import CheckResult


FEAST_REQUIRED_BASE_COLUMNS = ["event_timestamp"]


def check_feast_feature_table(
    frame: pd.DataFrame,
    entity_columns: list[str],
    feature_columns: list[str],
    name: str,
) -> CheckResult:
    required = entity_columns + FEAST_REQUIRED_BASE_COLUMNS + feature_columns
    missing = sorted(set(required) - set(frame.columns))
    null_timestamp_count = (
        int(frame["event_timestamp"].isna().sum())
        if "event_timestamp" in frame.columns
        else len(frame)
    )
    errors = []
    if missing:
        errors.append(f"{name} missing Feast columns: {missing}")
    if null_timestamp_count:
        errors.append(f"{name} has {null_timestamp_count} null event_timestamp rows")
    return CheckResult(
        passed=not errors,
        errors=errors,
        metrics={
            "row_count": len(frame),
            "missing_column_count": len(missing),
            "null_event_timestamp_count": null_timestamp_count,
        },
    )


def check_sequence_lengths(frame: pd.DataFrame, max_history_length: int) -> CheckResult:
    if "hist_length" not in frame.columns:
        return CheckResult(False, ["missing hist_length"], {"row_count": len(frame)})
    too_long = int((frame["hist_length"] > max_history_length).sum())
    return CheckResult(
        passed=too_long == 0,
        errors=[] if too_long == 0 else [f"{too_long} sequences exceed max history length"],
        metrics={"row_count": len(frame), "too_long_count": too_long},
    )


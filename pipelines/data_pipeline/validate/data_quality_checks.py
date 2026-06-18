from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    errors: list[str]
    metrics: dict[str, int | float]


def check_unique_grain(frame: pd.DataFrame, columns: list[str], name: str) -> CheckResult:
    if frame.empty:
        return CheckResult(False, [f"{name} is empty"], {"row_count": 0})
    duplicate_count = int(frame.duplicated(columns).sum())
    return CheckResult(
        passed=duplicate_count == 0,
        errors=[] if duplicate_count == 0 else [f"{name} has {duplicate_count} duplicate grain rows"],
        metrics={"row_count": len(frame), "duplicate_count": duplicate_count},
    )


def check_required_columns(frame: pd.DataFrame, columns: list[str], name: str) -> CheckResult:
    missing = sorted(set(columns) - set(frame.columns))
    return CheckResult(
        passed=not missing,
        errors=[] if not missing else [f"{name} missing columns: {missing}"],
        metrics={"row_count": len(frame), "missing_column_count": len(missing)},
    )


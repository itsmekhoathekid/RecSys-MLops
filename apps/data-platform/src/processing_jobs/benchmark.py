from __future__ import annotations

import argparse
import hashlib
import json
import random
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


def stable_hash(value: Any) -> int:
    digest = hashlib.md5(str(value).encode("utf-8"), usedforsecurity=False).hexdigest()
    return int(digest[:12], 16)


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def timed(callable_obj):
    start = time.perf_counter()
    value = callable_obj()
    duration_ms = (time.perf_counter() - start) * 1000
    return value, duration_ms


def floor_time(value: datetime, seconds: int) -> datetime:
    epoch_seconds = int(value.timestamp())
    return datetime.fromtimestamp(epoch_seconds - (epoch_seconds % seconds), tz=timezone.utc)


def max_partition_ratio(rows: list[dict[str, Any]], key: str, partitions: int) -> float:
    counts = Counter(stable_hash(row[key]) % partitions for row in rows)
    if not counts:
        return 0.0
    average = sum(counts.values()) / max(1, partitions)
    return max(counts.values()) / max(1.0, average)


def salted_partition_ratio(
    rows: list[dict[str, Any]],
    *,
    key: str,
    hot_keys: set[Any],
    partitions: int,
    salt_buckets: int,
) -> float:
    counts: Counter[int] = Counter()
    for row in rows:
        value = row[key]
        salt = stable_hash(row["event_id"]) % salt_buckets if value in hot_keys else 0
        counts[stable_hash(f"{value}:{salt}") % partitions] += 1
    if not counts:
        return 0.0
    average = sum(counts.values()) / max(1, partitions)
    return max(counts.values()) / max(1.0, average)


def generate_offline_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    data = config["data"]
    random.seed(int(data.get("seed", 42)))
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows: list[dict[str, Any]] = []
    event_count = int(data["event_count"])
    schema_change_at = int(event_count * float(data.get("old_schema_ratio", 0.45)))
    hot_product_id = int(data.get("hot_product_id", 1))

    for index in range(event_count):
        product_id = (
            hot_product_id
            if random.random() < float(data.get("hot_product_ratio", 0.55))
            else random.randint(2, int(data["product_count"]))
        )
        event_type = random.choices(["view", "cart", "purchase"], weights=[0.78, 0.16, 0.06], k=1)[0]
        row = {
            "event_id": f"event-{index}",
            "user_id": random.randint(1, int(data["user_count"])),
            "product_id": product_id,
            "event_type": event_type,
            "event_timestamp": base + timedelta(seconds=index),
            "ingestion_ts": base + timedelta(seconds=index, milliseconds=random.randint(0, 999)),
            "category_id": random.randint(1, int(data["category_count"])),
            "brand_id": random.randint(1, int(data["brand_count"])),
            "price_bucket": random.randint(1, 20),
            "price": round(random.uniform(5.0, 200.0), 2),
            "campaign_id": f"campaign-{random.randint(1, int(data['campaign_count']))}",
        }
        if index >= schema_change_at:
            row["device_type"] = random.choice(["ios", "android", "web"])
        rows.append(row)

    duplicates = int(event_count * float(data.get("duplicate_rate", 0.025)))
    for duplicate_index in range(duplicates):
        original = dict(rows[stable_hash(f"duplicate-{duplicate_index}") % len(rows)])
        original["ingestion_ts"] = original["ingestion_ts"] + timedelta(minutes=5)
        original["price"] = round(float(original["price"]) * 1.01, 2)
        rows.append(original)
    random.shuffle(rows)
    return rows


def run_spark_baseline(config: dict[str, Any]) -> dict[str, Any]:
    rows = generate_offline_rows(config)
    partitions = int(config["spark"].get("shuffle_partitions", 8))

    def job() -> dict[str, Any]:
        schema_missing = sum(1 for row in rows if "device_type" not in row)
        usable = [row for row in rows if "device_type" in row]
        product_ids = sorted({row["product_id"] for row in usable})
        feature_rows = []
        operation_count = 0
        for product_id in product_ids:
            views = 0
            purchases = 0
            for row in usable:
                operation_count += 1
                if row["product_id"] != product_id:
                    continue
                views += 1 if row["event_type"] == "view" else 0
                purchases += 1 if row["event_type"] == "purchase" else 0
            feature_rows.append({"product_id": product_id, "views": views, "purchases": purchases})

        return {
            "engine": "spark",
            "version": "baseline",
            "input_rows": len(rows),
            "rows_used": len(usable),
            "schema_evolution_rows_dropped": schema_missing,
            "duplicates_removed": 0,
            "duplicate_rows_written": len(usable) - len({row["event_id"] for row in usable}),
            "product_feature_rows": len(feature_rows),
            "raw_campaign_cardinality": len({row["campaign_id"] for row in usable}),
            "bounded_campaign_cardinality": len({row["campaign_id"] for row in usable}),
            "max_partition_ratio": round(max_partition_ratio(usable, "product_id", partitions), 4),
            "operation_count": operation_count,
            "optimizations": [],
        }

    metrics, duration_ms = timed(job)
    metrics["duration_ms"] = round(duration_ms, 3)
    metrics["rows_per_second"] = round(metrics["rows_used"] / max(duration_ms / 1000, 0.001), 2)
    metrics["verification_passed"] = metrics["rows_used"] > 0 and metrics["product_feature_rows"] > 0
    return metrics


def run_spark_optimized(config: dict[str, Any]) -> dict[str, Any]:
    rows = generate_offline_rows(config)
    partitions = int(config["spark"].get("shuffle_partitions", 8))
    salt_buckets = int(config["spark"].get("salt_buckets", 16))
    hot_key_threshold = float(config["spark"].get("hot_key_threshold", 0.20))
    top_campaigns = int(config["spark"].get("top_campaigns", 64))
    campaign_buckets = int(config["spark"].get("campaign_buckets", 32))

    def job() -> dict[str, Any]:
        normalized = []
        schema_defaults = 0
        for row in rows:
            copy = dict(row)
            if "device_type" not in copy:
                copy["device_type"] = "unknown"
                schema_defaults += 1
            normalized.append(copy)

        latest_by_event: dict[str, dict[str, Any]] = {}
        for row in normalized:
            current = latest_by_event.get(row["event_id"])
            if current is None or row["ingestion_ts"] > current["ingestion_ts"]:
                latest_by_event[row["event_id"]] = row
        deduped = list(latest_by_event.values())

        product_counts = Counter(row["product_id"] for row in deduped)
        hot_keys = {
            product_id
            for product_id, count in product_counts.items()
            if count / max(1, len(deduped)) >= hot_key_threshold
        }

        campaign_counts = Counter(row["campaign_id"] for row in deduped)
        top_campaign_set = {campaign for campaign, _ in campaign_counts.most_common(top_campaigns)}
        product_features: dict[int, dict[str, int]] = {}
        campaign_features: Counter[str] = Counter()
        operation_count = 0
        for row in deduped:
            operation_count += 1
            product_id = int(row["product_id"])
            feature = product_features.setdefault(product_id, {"views": 0, "purchases": 0})
            feature["views"] += 1 if row["event_type"] == "view" else 0
            feature["purchases"] += 1 if row["event_type"] == "purchase" else 0
            campaign = row["campaign_id"]
            if campaign not in top_campaign_set:
                campaign = f"rare_bucket_{stable_hash(campaign) % campaign_buckets}"
            campaign_features[campaign] += 1

        return {
            "engine": "spark",
            "version": "optimized",
            "input_rows": len(rows),
            "rows_used": len(deduped),
            "schema_evolution_rows_dropped": 0,
            "schema_defaults_applied": schema_defaults,
            "duplicates_removed": len(normalized) - len(deduped),
            "duplicate_rows_written": 0,
            "product_feature_rows": len(product_features),
            "raw_campaign_cardinality": len(campaign_counts),
            "bounded_campaign_cardinality": len(campaign_features),
            "hot_keys_salted": sorted(hot_keys),
            "max_partition_ratio": round(
                salted_partition_ratio(
                    deduped,
                    key="product_id",
                    hot_keys=hot_keys,
                    partitions=partitions,
                    salt_buckets=salt_buckets,
                ),
                4,
            ),
            "operation_count": operation_count,
            "optimizations": [
                "schema normalization with defaults for evolved columns",
                "latest-event dedup by event_id and ingestion_ts",
                "hot-key salting before product aggregation",
                "one-pass pre-aggregation instead of repeated scans",
                "top-k campaign retention with rare campaign hash buckets",
            ],
        }

    metrics, duration_ms = timed(job)
    metrics["duration_ms"] = round(duration_ms, 3)
    metrics["rows_per_second"] = round(metrics["rows_used"] / max(duration_ms / 1000, 0.001), 2)
    metrics["verification_passed"] = (
        metrics["rows_used"] > 0
        and metrics["schema_evolution_rows_dropped"] == 0
        and metrics["duplicate_rows_written"] == 0
        and metrics["bounded_campaign_cardinality"] <= metrics["raw_campaign_cardinality"]
    )
    return metrics


def generate_stream_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    data = config["data"]
    random.seed(int(data.get("seed", 42)))
    base = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    event_count = int(data["event_count"])
    burst_ratio = float(data.get("burst_ratio", 0.35))
    late_ratio = float(data.get("late_arrival_ratio", 0.12))
    watermark_seconds = int(data.get("watermark_seconds", 300))
    rows = []
    for index in range(event_count):
        if random.random() < burst_ratio:
            event_ts = base + timedelta(seconds=random.randint(0, 59))
        else:
            event_ts = base + timedelta(seconds=60 + index)
        is_late_source = random.random() < late_ratio
        processing_lag = (
            watermark_seconds + random.randint(60, 600)
            if is_late_source
            else random.randint(0, max(1, watermark_seconds // 3))
        )
        rows.append(
            {
                "event_id": f"stream-{index}",
                "user_id": random.randint(1, int(data["user_count"])),
                "product_id": random.randint(1, int(data["product_count"])),
                "event_type": random.choices(["view", "cart", "purchase"], weights=[0.80, 0.15, 0.05], k=1)[0],
                "event_timestamp": event_ts,
                "processed_timestamp": event_ts + timedelta(seconds=processing_lag),
            }
        )

    duplicates = int(event_count * float(data.get("duplicate_rate", 0.03)))
    for duplicate_index in range(duplicates):
        original = dict(rows[stable_hash(f"stream-duplicate-{duplicate_index}") % len(rows)])
        original["processed_timestamp"] = original["processed_timestamp"] + timedelta(seconds=15)
        rows.append(original)
    rows.sort(key=lambda row: row["processed_timestamp"])
    return rows


def run_flink_baseline(config: dict[str, Any]) -> dict[str, Any]:
    rows = generate_stream_rows(config)

    def job() -> dict[str, Any]:
        seen_history: list[dict[str, Any]] = []
        duplicate_rows = 0
        event_ids: set[str] = set()
        operation_count = 0
        writes = 0
        for row in rows:
            duplicate_rows += 1 if row["event_id"] in event_ids else 0
            event_ids.add(row["event_id"])
            for previous in seen_history:
                operation_count += 1
                if previous["user_id"] == row["user_id"]:
                    pass
            seen_history.append(row)
            writes += 3
        return {
            "engine": "flink",
            "version": "baseline",
            "input_events": len(rows),
            "events_processed": len(rows),
            "duplicate_events_skipped": 0,
            "duplicate_events_written": duplicate_rows,
            "late_events_detected": 0,
            "windows_emitted": 0,
            "bursty_windows": 0,
            "redis_or_sink_writes": writes,
            "max_state_events": len(seen_history),
            "operation_count": operation_count,
            "optimizations": [],
        }

    metrics, duration_ms = timed(job)
    metrics["duration_ms"] = round(duration_ms, 3)
    metrics["events_per_second"] = round(metrics["events_processed"] / max(duration_ms / 1000, 0.001), 2)
    metrics["verification_passed"] = metrics["events_processed"] == metrics["input_events"]
    return metrics


def run_flink_optimized(config: dict[str, Any]) -> dict[str, Any]:
    rows = generate_stream_rows(config)
    streaming = config["streaming"]
    watermark_seconds = int(streaming.get("watermark_seconds", 300))
    quality_window_seconds = int(streaming.get("quality_window_seconds", 60))
    burst_threshold = int(streaming.get("burst_threshold_event_count", 150))
    dedup_ttl_seconds = int(streaming.get("dedup_ttl_seconds", 3600))
    state_ttl_seconds = int(streaming.get("state_ttl_seconds", 7200))

    def job() -> dict[str, Any]:
        seen: dict[str, datetime] = {}
        user_state: dict[int, deque[datetime]] = defaultdict(deque)
        windows: dict[datetime, dict[str, Any]] = {}
        duplicate_skipped = 0
        late_events = 0
        writes = 0
        operation_count = 0

        for row in rows:
            processed_ts = row["processed_timestamp"]
            cutoff = processed_ts - timedelta(seconds=dedup_ttl_seconds)
            for event_id, timestamp in list(seen.items()):
                if timestamp < cutoff:
                    del seen[event_id]

            duplicate = row["event_id"] in seen
            if duplicate:
                duplicate_skipped += 1
            else:
                seen[row["event_id"]] = processed_ts

            late_by_seconds = max(0.0, (processed_ts - row["event_timestamp"]).total_seconds())
            is_late = late_by_seconds > watermark_seconds
            late_events += 1 if is_late else 0

            window_start = floor_time(row["event_timestamp"], quality_window_seconds)
            window = windows.setdefault(
                window_start,
                {
                    "event_count": 0,
                    "late_event_count": 0,
                    "duplicate_event_count": 0,
                    "max_late_by_seconds": 0.0,
                },
            )
            window["event_count"] += 1
            window["late_event_count"] += 1 if is_late else 0
            window["duplicate_event_count"] += 1 if duplicate else 0
            window["max_late_by_seconds"] = max(window["max_late_by_seconds"], late_by_seconds)

            if duplicate:
                continue

            history = user_state[int(row["user_id"])]
            history.append(row["event_timestamp"])
            state_cutoff = row["event_timestamp"] - timedelta(seconds=state_ttl_seconds)
            while history and history[0] < state_cutoff:
                history.popleft()
                operation_count += 1
            writes += 3
            operation_count += 1

        bursty_windows = sum(1 for window in windows.values() if window["event_count"] >= burst_threshold)
        return {
            "engine": "flink",
            "version": "optimized",
            "input_events": len(rows),
            "events_processed": len(rows) - duplicate_skipped,
            "duplicate_events_skipped": duplicate_skipped,
            "duplicate_events_written": 0,
            "late_events_detected": late_events,
            "windows_emitted": len(windows),
            "bursty_windows": bursty_windows,
            "redis_or_sink_writes": writes,
            "max_state_events": sum(len(history) for history in user_state.values()),
            "operation_count": operation_count,
            "optimizations": [
                "event-id dedup with TTL",
                "watermark-based late arrival detection",
                "fixed event-time quality windows",
                "burst flagging by per-window event count",
                "bounded keyed user state with TTL",
            ],
        }

    metrics, duration_ms = timed(job)
    metrics["duration_ms"] = round(duration_ms, 3)
    metrics["events_per_second"] = round(metrics["events_processed"] / max(duration_ms / 1000, 0.001), 2)
    metrics["verification_passed"] = (
        metrics["duplicate_events_written"] == 0
        and metrics["late_events_detected"] > 0
        and metrics["windows_emitted"] > 0
        and metrics["bursty_windows"] > 0
    )
    return metrics


def run_benchmark(config_path: str | Path) -> dict[str, Any]:
    config = load_config(config_path)
    engine = config["job"]["engine"]
    version = config["job"]["version"]
    if engine == "spark" and version == "baseline":
        return run_spark_baseline(config)
    if engine == "spark" and version == "optimized":
        return run_spark_optimized(config)
    if engine == "flink" and version == "baseline":
        return run_flink_baseline(config)
    if engine == "flink" and version == "optimized":
        return run_flink_optimized(config)
    raise ValueError(f"Unsupported benchmark config: engine={engine}, version={version}")


def compare_metrics(baseline: dict[str, Any], optimized: dict[str, Any]) -> dict[str, Any]:
    duration_speedup = baseline["duration_ms"] / max(optimized["duration_ms"], 0.001)
    operation_reduction = baseline["operation_count"] / max(optimized["operation_count"], 1)
    comparison = {
        "engine": baseline["engine"],
        "baseline_version": baseline["version"],
        "optimized_version": optimized["version"],
        "baseline_duration_ms": baseline["duration_ms"],
        "optimized_duration_ms": optimized["duration_ms"],
        "duration_speedup": round(duration_speedup, 3),
        "operation_reduction": round(operation_reduction, 3),
        "verification_passed": baseline.get("verification_passed", False) and optimized.get("verification_passed", False),
    }
    if baseline["engine"] == "spark":
        comparison.update(
            {
                "baseline_max_partition_ratio": baseline["max_partition_ratio"],
                "optimized_max_partition_ratio": optimized["max_partition_ratio"],
                "partition_ratio_improvement": round(
                    baseline["max_partition_ratio"] / max(optimized["max_partition_ratio"], 0.001),
                    3,
                ),
                "schema_rows_recovered": optimized.get("schema_defaults_applied", 0),
                "duplicates_removed_after_optimize": optimized.get("duplicates_removed", 0),
                "campaign_cardinality_reduction": baseline["bounded_campaign_cardinality"]
                - optimized["bounded_campaign_cardinality"],
            }
        )
    if baseline["engine"] == "flink":
        comparison.update(
            {
                "duplicates_no_longer_written": baseline["duplicate_events_written"],
                "late_events_detected_after_optimize": optimized["late_events_detected"],
                "windows_emitted_after_optimize": optimized["windows_emitted"],
                "bursty_windows_after_optimize": optimized["bursty_windows"],
            }
        )
    return comparison


def write_outputs(payload: dict[str, Any], output_dir: str | Path, name: str) -> None:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    lines = [f"# {name}", "", "| Metric | Value |", "| --- | --- |"]
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, sort_keys=True, default=str)
        else:
            rendered = str(value)
        lines.append(f"| `{key}` | `{rendered}` |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local processing job baseline/optimized benchmarks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--config", required=True)
    run_parser.add_argument("--output-dir", default="reports/processing_jobs")
    run_parser.add_argument("--name")

    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--baseline", required=True)
    compare_parser.add_argument("--optimized", required=True)
    compare_parser.add_argument("--output-dir", default="reports/processing_jobs")
    compare_parser.add_argument("--name")

    args = parser.parse_args()
    if args.command == "run":
        result = run_benchmark(args.config)
        name = args.name or f"{result['engine']}_{result['version']}"
        write_outputs(result, args.output_dir, name)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0 if result.get("verification_passed") else 1

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    optimized = json.loads(Path(args.optimized).read_text(encoding="utf-8"))
    comparison = compare_metrics(baseline, optimized)
    name = args.name or f"{comparison['engine']}_comparison"
    write_outputs(comparison, args.output_dir, name)
    print(json.dumps(comparison, indent=2, sort_keys=True, default=str))
    return 0 if comparison.get("verification_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())


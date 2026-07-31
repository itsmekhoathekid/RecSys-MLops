from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timezone

from generator_config import load_stream_config
from streaming.config import StreamGeneratorConfig
from streaming.event_factory import StreamEventFactory
from streaming.metrics import push_realtime_metrics
from streaming.postgres import bootstrap_dimensions, conninfo, write_bundle
from streaming.problem_pipeline import StreamProblemPipeline


DEFAULT_CONFIG = "configs/data-platform/generator/default.yaml"


def write_verification_event(event_id: str) -> None:
    import psycopg

    if not event_id.strip():
        raise ValueError("verification event ID must not be empty")

    now = datetime.now(timezone.utc)
    rows = StreamEventFactory(1, 1).create(0, now, now)
    rows["sessions"]["session_id"] = f"{event_id}-session"
    rows["recommendation_requests"].update(
        request_id=f"{event_id}-request",
        session_id=f"{event_id}-session",
    )
    rows["impressions"].update(
        impression_id=f"{event_id}-impression",
        request_id=f"{event_id}-request",
        session_id=f"{event_id}-session",
    )
    rows["behavior_events"].update(
        event_id=event_id,
        payload_hash=event_id,
        session_id=f"{event_id}-session",
        request_id=f"{event_id}-request",
        impression_id=f"{event_id}-impression",
    )
    with psycopg.connect(conninfo()) as connection:
        with connection.cursor() as cursor:
            bootstrap_dimensions(cursor, now, 1, 1)
            write_bundle(cursor, rows)
        connection.commit()
        # The second write exercises Flink idempotency for the same event ID.
        with connection.cursor() as cursor:
            write_bundle(cursor, rows)
        connection.commit()
    print(json.dumps({"event_id": event_id, "writes": 2}), flush=True)


def run(config: StreamGeneratorConfig) -> None:
    import psycopg

    settings = config.generator
    problems = StreamProblemPipeline(random.Random(config.seed), config.problems)
    factory = StreamEventFactory(settings.n_users, settings.n_products)
    counter = tick = duplicates_total = late_total = 0

    with psycopg.connect(conninfo()) as connection:
        with connection.cursor() as cursor:
            bootstrap_dimensions(
                cursor, datetime.now(timezone.utc), settings.n_users, settings.n_products
            )
        connection.commit()

        while settings.max_events <= 0 or counter < settings.max_events:
            inserted = duplicated = late = 0
            max_late_seconds = 0
            tick += 1
            events_this_tick = problems.burst.events_for_tick(
                tick, settings.events_per_tick
            )
            with connection.cursor() as cursor:
                for _ in range(events_this_tick):
                    if settings.max_events > 0 and counter >= settings.max_events:
                        break
                    now = datetime.now(timezone.utc)
                    replay = problems.replay(now)
                    if replay is not None:
                        rows = replay
                        duplicated += 1
                    else:
                        timing = problems.event_time(now)
                        rows = factory.create(counter, now, timing.event_timestamp)
                        problems.duplicates.remember(rows)
                        late += int(timing.late)
                        max_late_seconds = max(max_late_seconds, timing.delay_seconds)
                    write_bundle(cursor, rows)
                    counter += 1
                    inserted += 1
            connection.commit()
            duplicates_total += duplicated
            late_total += late
            burst_tick = events_this_tick > settings.events_per_tick
            push_realtime_metrics(
                {
                    "recsys_streaming_events_total": counter,
                    "recsys_streaming_late_events_total": late_total,
                    "recsys_streaming_duplicate_events_total": duplicates_total,
                    "recsys_streaming_last_event_unixtime": int(
                        datetime.now(timezone.utc).timestamp()
                    ),
                    "recsys_streaming_current_max_late_seconds": max_late_seconds,
                    "recsys_streaming_current_window_bursty": int(burst_tick),
                }
            )
            print(
                json.dumps(
                    {
                        "inserted": inserted,
                        "total_events": counter,
                        "tick": tick,
                        "duplicates_emitted": duplicated,
                        "late_events_emitted": late,
                        "burst_tick": burst_tick,
                    }
                ),
                flush=True,
            )
            if settings.max_events > 0 and counter >= settings.max_events:
                break
            time.sleep(settings.interval_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Continuously simulate online source events."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--verification-event-id")
    args = parser.parse_args()
    if args.verification_event_id:
        write_verification_event(args.verification_event_id)
        return 0
    run(load_stream_config(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

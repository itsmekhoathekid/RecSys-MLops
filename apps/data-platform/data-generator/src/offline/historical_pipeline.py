from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from config import GeneratorConfig
from drift.reporting import DriftReporter
from offline.problem_pipeline import OfflineProblemPipeline
from offline.simulation import RecsysSimulation
from sink import LocalParquetSink
from validation import InvariantValidator, duplicate_metrics


LOGGER = logging.getLogger(__name__)


class HistoricalDataPipeline:
    def __init__(self, config: GeneratorConfig):
        self.config = config

    def run(self) -> dict[str, Any]:
        simulation = RecsysSimulation(self.config)
        data = simulation.generate()

        challenge_rng = simulation.rng
        challenge_pipeline = OfflineProblemPipeline(
            rng=challenge_rng,
            config=self.config.challenges,
            schema_change_date=self.config.schema_evolution.change_date,
            breaking_schema_change_date=(
                self.config.schema_evolution.breaking_change_date
            ),
            breaking_schema_version=self.config.schema_evolution.breaking_schema_version,
        )
        emitted_events, challenge_stats = challenge_pipeline.apply(
            data.behavior_events
        )
        data.behavior_events = emitted_events

        validation = InvariantValidator().validate(
            data=data,
            config=self.config,
            emitted_event_count=len(emitted_events),
        )
        if not validation.passed:
            raise ValueError(
                "Generated data failed validation:\n- "
                + "\n- ".join(validation.errors[:20])
            )

        run_path = Path(self.config.output.base_path) / self.config.output.run_id
        sink = LocalParquetSink(
            run_path=run_path, overwrite=self.config.output.overwrite
        )
        paths: dict[str, list[str]] = {}
        row_counts: dict[str, int] = {}
        for table_name, records in data.table_records().items():
            paths[table_name] = sink.write(table_name, records)
            row_counts[table_name] = len(records)

        drift_artifacts = None
        if self.config.drift.enabled:
            drift_artifacts = DriftReporter(self.config).write(run_path, data)

        config_payload = self.config.model_dump(mode="json")
        config_hash = hashlib.sha256(
            json.dumps(config_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        duplicate_stats = duplicate_metrics(data)
        city_counts = Counter(user.city for user in data.users)
        category_counts = Counter(
            event.category_id for event in data.behavior_events
        )
        event_count = max(len(data.behavior_events), 1)
        dq_report = {
            "validation_passed": validation.passed,
            "validation_errors": validation.errors,
            "validation_metrics": validation.metrics,
            "injected": {
                "exact_duplicates": challenge_stats.exact_duplicates_injected,
            },
            "observed": {
                **duplicate_stats,
                "top_city": city_counts.most_common(1)[0][0],
                "top_city_ratio": round(
                    city_counts.most_common(1)[0][1] / len(data.users), 6
                ),
                "top_event_category_id": category_counts.most_common(1)[0][0],
                "top_event_category_ratio": round(
                    category_counts.most_common(1)[0][1] / event_count, 6
                ),
                "schema_v1_events": challenge_stats.schema_v1_events,
                "schema_v2_events": challenge_stats.schema_v2_events,
                "null_device_type_events": sum(
                    event.device_type is None for event in data.behavior_events
                ),
            },
        }
        if drift_artifacts is not None:
            dq_report["drift"] = drift_artifacts.summary
        manifest = {
            "run_id": self.config.output.run_id,
            "seed": self.config.seed,
            "config_hash": config_hash,
            "config": config_payload,
            "row_counts": row_counts,
            "schema_versions": [1, 2],
            "paths": paths,
            "data_quality_report": "data_quality_report.json",
            "drift": self.config.drift.model_dump(mode="json"),
            "drift_artifacts": (
                drift_artifacts.paths if drift_artifacts is not None else {}
            ),
        }
        sink.write_json("data_quality_report.json", dq_report)
        sink.write_json("manifest.json", manifest)

        LOGGER.info(
            json.dumps(
                {
                    "event": "generation_complete",
                    "run_path": str(run_path),
                    "row_counts": row_counts,
                    "injected": dq_report["injected"],
                },
                sort_keys=True,
            )
        )
        return {
            "run_path": str(run_path),
            "manifest": manifest,
            "data_quality_report": dq_report,
        }

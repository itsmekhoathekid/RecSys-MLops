from pathlib import Path

import pytest

from data_generator.config import load_config


@pytest.fixture
def base_config():
    return load_config(Path("config/data_generator_test.yaml"))


@pytest.fixture
def small_config(base_config, tmp_path):
    return base_config.model_copy(
        update={
            "entities": base_config.entities.model_copy(
                update={
                    "n_users": 30,
                    "n_products": 40,
                    "n_categories": 8,
                    "n_brands": 12,
                }
            ),
            "traffic": base_config.traffic.model_copy(
                update={"target_behavior_events": 250, "target_tolerance": 0.15}
            ),
            "output": base_config.output.model_copy(
                update={
                    "base_path": str(tmp_path),
                    "run_id": "small",
                    "overwrite": True,
                }
            ),
        }
    )

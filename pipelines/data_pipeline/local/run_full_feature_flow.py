from __future__ import annotations

from pipelines.data_pipeline.local.run_batch_features import run_batch_features


def run_full_feature_flow() -> dict[str, int]:
    """Run the currently executable local POC flow.

    Dockerized Kafka/Flink/Feast services are scaffolded separately; this runner
    intentionally stops at offline feature table readiness.
    """
    return run_batch_features()


if __name__ == "__main__":
    print(run_full_feature_flow())


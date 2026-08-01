import os


def push_realtime_metrics(samples: dict[str, float]) -> bool:
    try:
        from monitoring.pushgateway import MetricSample, push_metrics
    except ImportError:
        return False
    return push_metrics(
        [MetricSample(name, value) for name, value in samples.items()],
        "recsys_streaming_source_live",
        gateway_url=os.getenv("PUSHGATEWAY_URL") or None,
        grouping_key={
            "pipeline_role": "online",
            "source_topic": os.getenv("REALTIME_STREAM_TOPIC", "cdc.behavior_events"),
        },
    )

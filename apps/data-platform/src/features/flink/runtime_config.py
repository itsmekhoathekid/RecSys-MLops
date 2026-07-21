from __future__ import annotations

from typing import Any


def apply_state_ttl(descriptor: Any, ttl_seconds: int) -> Any:
    if ttl_seconds <= 0:
        return descriptor
    from pyflink.common import Time
    from pyflink.datastream.state import StateTtlConfig

    ttl_config = (
        StateTtlConfig.new_builder(Time.seconds(ttl_seconds))
        .update_ttl_on_create_and_write()
        .never_return_expired()
        .build()
    )
    descriptor.enable_time_to_live(ttl_config)
    return descriptor


def configure_checkpointing(env: Any, args: Any) -> None:
    from pyflink.datastream import CheckpointingMode
    from pyflink.datastream.checkpoint_config import ExternalizedCheckpointRetention

    env.enable_checkpointing(args.checkpoint_interval_seconds * 1000)
    checkpoint_config = env.get_checkpoint_config()
    checkpoint_config.set_checkpointing_mode(CheckpointingMode.EXACTLY_ONCE)
    checkpoint_config.set_min_pause_between_checkpoints(args.checkpoint_min_pause_seconds * 1000)
    checkpoint_config.set_checkpoint_timeout(args.checkpoint_timeout_seconds * 1000)
    checkpoint_config.set_max_concurrent_checkpoints(1)
    checkpoint_config.set_tolerable_checkpoint_failure_number(args.tolerable_checkpoint_failures)
    checkpoint_config.set_externalized_checkpoint_retention(
        ExternalizedCheckpointRetention.RETAIN_ON_CANCELLATION
    )
    if args.unaligned_checkpoints_enabled:
        checkpoint_config.enable_unaligned_checkpoints()

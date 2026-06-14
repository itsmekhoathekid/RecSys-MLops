from collections import defaultdict

from data_generator.behavior import SessionState
from data_generator.challenges import ChallengePipeline
from data_generator.pipeline import HistoricalDataPipeline
from data_generator.simulation import RecsysSimulation
from data_generator.validation import InvariantValidator, validate_parquet_output


def test_same_seed_is_reproducible(small_config):
    first = RecsysSimulation(small_config).generate()
    second = RecsysSimulation(small_config).generate()
    assert first.users == second.users
    assert first.products == second.products
    assert first.behavior_events == second.behavior_events


def test_different_seed_changes_ids(small_config):
    first = RecsysSimulation(small_config).generate()
    changed = small_config.model_copy(update={"seed": small_config.seed + 1})
    second = RecsysSimulation(changed).generate()
    assert first.sessions[0].session_id != second.sessions[0].session_id


def test_state_machine_transitions_are_valid(small_config):
    simulation = RecsysSimulation(small_config)
    users, _ = simulation._generate_users()
    products, _ = simulation._generate_products()
    valid_sequences = {
        (SessionState.IMPRESSION, SessionState.LEAVE),
        (SessionState.IMPRESSION, SessionState.VIEW, SessionState.LEAVE),
        (
            SessionState.IMPRESSION,
            SessionState.VIEW,
            SessionState.CART,
            SessionState.ABANDON,
        ),
        (
            SessionState.IMPRESSION,
            SessionState.VIEW,
            SessionState.CART,
            SessionState.PURCHASE,
        ),
    }
    from data_generator.behavior import BehaviorContext

    for _ in range(100):
        states = simulation.state_machine.run(
            users[0], products[0], BehaviorContext(rank_position=1, is_campaign=False)
        )
        assert tuple(states) in valid_sequences


def test_clean_data_invariants(small_config):
    clean = RecsysSimulation(small_config).generate()
    emitted, _ = ChallengePipeline(
        RecsysSimulation(small_config).rng,
        small_config.challenges,
        small_config.schema_evolution.change_date,
    ).apply(clean.behavior_events)
    clean.behavior_events = emitted
    result = InvariantValidator().validate(clean, small_config, len(emitted))
    assert result.passed, result.errors


def test_purchase_order_and_impression_linkage(small_config):
    data = RecsysSimulation(small_config).generate()
    active_user_ids = {user.user_id for user in data.users if user.is_active}
    order_ids = {order.order_id for order in data.orders}
    request_ids = {request.request_id for request in data.recommendation_requests}
    impression_ids = {
        impression.impression_id for impression in data.impressions
    }
    items_by_order = defaultdict(list)
    for item in data.order_items:
        items_by_order[item.order_id].append(item)

    purchases = [
        event for event in data.behavior_events if event.event_type == "purchase"
    ]
    assert purchases
    assert all(event.order_id in order_ids for event in purchases)
    assert all(event.request_id in request_ids for event in data.behavior_events)
    assert all(event.impression_id in impression_ids for event in data.behavior_events)
    assert all(items_by_order[order_id] for order_id in order_ids)
    assert all(session.user_id in active_user_ids for session in data.sessions)


def test_challenge_contracts(small_config):
    data = RecsysSimulation(small_config).generate()
    challenge_config = small_config.challenges.model_copy(
        update={
            "duplicate_event_rate": 1.0,
            "conflicting_duplicate_rate": 1.0,
            "late_arrival_rate": 1.0,
            "out_of_order_rate": 1.0,
        }
    )
    pipeline = ChallengePipeline(
        RecsysSimulation(small_config).rng,
        challenge_config,
        small_config.schema_evolution.change_date,
    )
    output, stats = pipeline.apply(data.behavior_events[:10])
    assert len(output) == 30
    assert stats.exact_duplicates_injected == 10
    assert stats.conflicting_duplicates_injected == 10
    assert stats.late_arrivals_injected == 10
    assert stats.out_of_order_injected == 10
    assert all(
        challenge_config.late_delay_minutes_min * 60
        <= (event.created_ts - event.event_timestamp).total_seconds()
        <= challenge_config.late_delay_minutes_max * 60
        for event in output
    )


def test_schema_evolution(small_config):
    data = RecsysSimulation(small_config).generate()
    output, stats = ChallengePipeline(
        RecsysSimulation(small_config).rng,
        small_config.challenges.model_copy(
            update={
                "duplicate_event_rate": 0,
                "conflicting_duplicate_rate": 0,
            }
        ),
        small_config.schema_evolution.change_date,
    ).apply(data.behavior_events)
    assert stats.schema_v1_events > 0
    assert stats.schema_v2_events > 0
    assert all(
        event.device_type is None and event.campaign_id is None
        for event in output
        if event.schema_version == 1
    )
    assert all(event.device_type is not None for event in output if event.schema_version == 2)


def test_parquet_round_trip(small_config):
    result = HistoricalDataPipeline(small_config).run()
    validation = validate_parquet_output(
        __import__("pathlib").Path(result["run_path"])
    )
    assert validation.passed, validation.errors
    assert validation.metrics["row_counts"]["behavior_events"] > 0

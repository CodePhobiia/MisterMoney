from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from pmm1.materializers.canary_cycle_fact import CanaryCycleFactMaterializer
from pmm1.materializers.shadow_cycle_fact import ShadowCycleFactMaterializer
from pmm1.storage.spine import SpineEvent


class _FakePostgresStore:
    def __init__(self) -> None:
        self.shadow_cycle_facts: dict[str, dict] = {}
        self.canary_cycle_facts: dict[str, dict] = {}
        self.spine_events: list[dict] = []

    async def upsert_shadow_cycle_fact(self, record: dict) -> None:
        self.shadow_cycle_facts[record["source_event_id"]] = dict(record)

    async def reset_shadow_cycle_fact_materialization(self) -> None:
        self.shadow_cycle_facts.clear()

    async def get_recent_shadow_cycle_facts(self, limit: int = 100):
        return list(self.shadow_cycle_facts.values())[:limit]

    async def upsert_canary_cycle_fact(self, record: dict) -> None:
        self.canary_cycle_facts[record["source_event_id"]] = dict(record)

    async def reset_canary_cycle_fact_materialization(self) -> None:
        self.canary_cycle_facts.clear()

    async def get_recent_canary_cycle_facts(self, limit: int = 100):
        return list(self.canary_cycle_facts.values())[:limit]

    async def get_spine_events(self, *, event_types=None, order_by="ts_ingest ASC"):
        if not event_types:
            return list(self.spine_events)
        return [row for row in self.spine_events if row["event_type"] in set(event_types)]


class _FakeRedisStreamStore:
    def __init__(self, entries):
        self.entries = list(entries)
        self.acked: list[str] = []

    async def ensure_spine_consumer_group(self, group_name: str, **kwargs) -> None:
        return None

    async def read_spine_stream(self, **kwargs):
        entries = list(self.entries)
        self.entries.clear()
        return entries

    async def ack_spine_stream_event(self, event_id: str, **kwargs):
        self.acked.append(event_id)
        return 1


def _stream_entry(stream_event_id: str, event: SpineEvent):
    return (
        stream_event_id,
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "ts_event": event.ts_event.isoformat(),
            "ts_ingest": event.ts_ingest.isoformat(),
            "controller": event.controller,
            "strategy": event.strategy,
            "session_id": event.session_id,
            "git_sha": event.git_sha,
            "config_hash": event.config_hash,
            "run_stage": event.run_stage,
            "condition_id": event.condition_id or "",
            "token_id": event.token_id or "",
            "order_id": event.order_id or "",
            "payload_json": json.dumps(event.payload_json),
        },
    )


def _shadow_event() -> SpineEvent:
    return SpineEvent(
        event_id="shadow-1",
        event_type="pmm2_shadow_cycle",
        ts_event=datetime(2026, 3, 13, 12, 0, tzinfo=UTC),
        controller="pmm2_shadow",
        strategy="pmm2_shadow",
        session_id="20260313_120000",
        git_sha="sha-1",
        config_hash="cfg-1",
        run_stage="shadow",
        payload_json={
            "cycle_num": 7,
            "comparison": {
                "v1_markets": ["m1"],
                "pmm2_markets": ["m1", "m2"],
                "market_overlap_pct": 0.5,
                "overlap_quote_distance_bps": 12.0,
                "v1_total_ev_usdc": 1.0,
                "pmm2_total_ev_usdc": 2.0,
                "ev_delta_usdc": 1.0,
                "v1_reward_market_count": 1,
                "pmm2_reward_market_count": 2,
                "reward_market_delta": 1.0,
                "v1_reward_ev_usdc": 0.5,
                "pmm2_reward_ev_usdc": 0.8,
                "reward_ev_delta_usdc": 0.3,
                "v1_cancel_rate_per_order_min": 0.4,
                "pmm2_cancel_rate_per_order_min": 0.2,
                "churn_delta_per_order_min": 0.2,
            },
            "summary": {"ready_for_live": True},
            "gate_diagnostics": {
                "window_cycles": 100,
                "ev_sample_count": 60,
                "reward_sample_count": 60,
                "churn_sample_count": 60,
                "blocking_gates": [],
            },
        },
    )


def _canary_event() -> SpineEvent:
    return SpineEvent(
        event_id="canary-1",
        event_type="pmm2_canary_decision",
        ts_event=datetime(2026, 3, 13, 12, 1, tzinfo=UTC),
        controller="pmm2_canary",
        strategy="pmm2_canary",
        session_id="20260313_120000",
        git_sha="sha-1",
        config_hash="cfg-1",
        run_stage="canary_5pct",
        payload_json={
            "cycle_num": 11,
            "canary_stage": "canary_5pct",
            "live_capital_pct": 0.05,
            "controlled_market_count": 2,
            "controlled_order_count": 4,
            "realized_fills": 3,
            "realized_cancels": 1,
            "realized_markout_1s": None,
            "realized_markout_5s": None,
            "reward_capture_count": 0,
            "rollback_flag": False,
            "promotion_eligible": False,
            "incident_flag": False,
            "summary_json": {"recent_fill_markets": ["m1", "m2"]},
        },
    )


def test_shadow_cycle_fact_materializer_consumes_stream():
    event = _shadow_event()
    postgres = _FakePostgresStore()
    redis_stream = _FakeRedisStreamStore([_stream_entry("1-0", event)])
    materializer = ShadowCycleFactMaterializer(
        postgres, redis_stream, consumer_name="test-consumer"
    )

    processed = asyncio.run(materializer.run_once())

    assert processed == 1
    fact = postgres.shadow_cycle_facts["shadow-1"]
    assert fact["cycle_num"] == 7
    assert fact["ready_for_live"] is True
    assert fact["pmm2_market_count"] == 2


def test_canary_cycle_fact_materializer_consumes_stream():
    event = _canary_event()
    postgres = _FakePostgresStore()
    redis_stream = _FakeRedisStreamStore([_stream_entry("1-0", event)])
    materializer = CanaryCycleFactMaterializer(
        postgres, redis_stream, consumer_name="test-consumer"
    )

    processed = asyncio.run(materializer.run_once())

    assert processed == 1
    fact = postgres.canary_cycle_facts["canary-1"]
    assert fact["cycle_num"] == 11
    assert fact["canary_stage"] == "canary_5pct"
    assert fact["controlled_market_count"] == 2


def test_cycle_fact_materializers_can_rebuild_from_event_log():
    shadow_event = _shadow_event()
    canary_event = _canary_event()
    postgres = _FakePostgresStore()
    postgres.spine_events = [
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "ts_event": event.ts_event,
            "ts_ingest": event.ts_ingest,
            "controller": event.controller,
            "strategy": event.strategy,
            "session_id": event.session_id,
            "git_sha": event.git_sha,
            "config_hash": event.config_hash,
            "run_stage": event.run_stage,
            "condition_id": event.condition_id,
            "token_id": event.token_id,
            "order_id": event.order_id,
            "payload_json": event.payload_json,
        }
        for event in (shadow_event, canary_event)
    ]
    shadow_materializer = ShadowCycleFactMaterializer(postgres, None, consumer_name="test-consumer")
    canary_materializer = CanaryCycleFactMaterializer(postgres, None, consumer_name="test-consumer")

    rebuilt_shadow = asyncio.run(shadow_materializer.rebuild_from_event_log())
    rebuilt_canary = asyncio.run(canary_materializer.rebuild_from_event_log())

    assert rebuilt_shadow == 1
    assert rebuilt_canary == 1
    assert set(postgres.shadow_cycle_facts) == {"shadow-1"}
    assert set(postgres.canary_cycle_facts) == {"canary-1"}

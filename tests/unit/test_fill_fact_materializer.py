from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from pmm1.materializers.fill_fact import FillFactMaterializer
from pmm1.storage.spine import SpineEvent


class _FakePostgresStore:
    def __init__(self) -> None:
        self.fill_facts: dict[str, dict] = {}
        self.spine_events: list[dict] = []

    async def upsert_fill_fact(self, record: dict) -> None:
        self.fill_facts[record["fill_id"]] = dict(record)

    async def reset_fill_fact_materialization(self) -> None:
        self.fill_facts.clear()

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


def _event(event_id: str, event_type: str, *, order_id: str = "ord-1") -> SpineEvent:
    return SpineEvent(
        event_id=event_id,
        event_type=event_type,
        ts_event=datetime(2026, 3, 13, 12, 0, tzinfo=UTC),
        controller="v1",
        strategy="mm",
        session_id="20260313_120000",
        git_sha="sha-1",
        config_hash="cfg-1",
        run_stage="shadow",
        condition_id="cond-1",
        token_id="tok-1",
        order_id=order_id,
        payload_json={
            "side": "BUY",
            "price": "0.51",
            "size": "4",
            "fee": 0.01,
            "is_scoring": True,
            "reward_eligible": True,
            "markout_1s": 0.02,
        },
    )


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


def test_fill_fact_materializer_consumes_stream():
    fill = _event("fill-1", "order_filled")
    postgres = _FakePostgresStore()
    redis_stream = _FakeRedisStreamStore([_stream_entry("1-0", fill)])
    materializer = FillFactMaterializer(postgres, redis_stream, consumer_name="test-consumer")

    processed = asyncio.run(materializer.run_once())

    assert processed == 1
    assert redis_stream.acked == ["1-0"]
    fact = postgres.fill_facts["fill-1"]
    assert fact["order_id"] == "ord-1"
    assert fact["fill_size"] == 4.0
    assert fact["reward_eligible"] is True
    assert fact["scoring_flag"] is True
    assert fact["fee_usdc"] == 0.01
    assert fact["mid_at_fill"] is None


def test_fill_fact_materializer_can_rebuild_from_event_log():
    fill_a = _event("fill-a", "order_partially_filled", order_id="ord-a")
    fill_b = _event("fill-b", "order_filled", order_id="ord-b")

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
        for event in (fill_a, fill_b)
    ]
    materializer = FillFactMaterializer(
        postgres, _FakeRedisStreamStore([]),
        consumer_name="test-consumer",
    )

    rebuilt = asyncio.run(materializer.rebuild_from_event_log())

    assert rebuilt == 2
    assert set(postgres.fill_facts) == {"fill-a", "fill-b"}

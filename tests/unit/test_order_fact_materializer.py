from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from pmm1.materializers.order_fact import OrderFactMaterializer
from pmm1.storage.spine import SpineEvent


class _FakePostgresStore:
    def __init__(self) -> None:
        self.order_facts: dict[str, dict] = {}
        self.applied_event_ids: set[str] = set()
        self.spine_events: list[dict] = []

    async def get_order_fact(self, order_id: str):
        row = self.order_facts.get(order_id)
        return dict(row) if row else None

    async def apply_order_fact_update(self, event_id: str, record: dict) -> bool:
        if event_id in self.applied_event_ids:
            return False
        self.applied_event_ids.add(event_id)
        self.order_facts[record["order_id"]] = dict(record)
        return True

    async def reset_order_fact_materialization(self) -> None:
        self.order_facts.clear()
        self.applied_event_ids.clear()

    async def get_spine_events(self, *, event_types=None, order_by="ts_ingest ASC"):
        if not event_types:
            return list(self.spine_events)
        return [row for row in self.spine_events if row["event_type"] in set(event_types)]


class _FakeRedisStreamStore:
    def __init__(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        self.entries = list(entries)
        self.acked: list[str] = []
        self.groups: list[str] = []

    async def ensure_spine_consumer_group(self, group_name: str, **kwargs) -> None:
        self.groups.append(group_name)

    async def read_spine_stream(self, **kwargs):
        entries = list(self.entries)
        self.entries.clear()
        return entries

    async def ack_spine_stream_event(self, event_id: str, **kwargs):
        self.acked.append(event_id)
        return 1


def _stream_entry(stream_event_id: str, event: SpineEvent) -> tuple[str, dict[str, str]]:
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


def _event(
    event_id: str,
    event_type: str,
    *,
    order_id: str = "",
    payload_json: dict | None = None,
) -> SpineEvent:
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
        order_id=order_id or None,
        payload_json=payload_json or {},
    )


def test_order_fact_materializer_consumes_stream_and_builds_row():
    requested = _event(
        "evt-1",
        "order_submit_requested",
        payload_json={
            "side": "BUY",
            "price": "0.51",
            "size": "10",
            "post_only": True,
            "order_type": "GTD",
            "expiration": 123,
        },
    )
    acknowledged = _event(
        "evt-2",
        "order_submit_acknowledged",
        order_id="ord-1",
        payload_json={
            "side": "BUY",
            "price": "0.51",
            "size": "10",
            "post_only": True,
            "order_type": "GTD",
            "expiration": 123,
        },
    )
    live = _event(
        "evt-3",
        "order_live",
        order_id="ord-1",
        payload_json={"side": "BUY", "price": "0.51", "size": "10"},
    )
    filled = _event(
        "evt-4",
        "order_filled",
        order_id="ord-1",
        payload_json={"side": "BUY", "price": "0.51", "size": "10"},
    )

    postgres = _FakePostgresStore()
    redis_stream = _FakeRedisStreamStore(
        [
            _stream_entry("1-0", requested),
            _stream_entry("2-0", acknowledged),
            _stream_entry("3-0", live),
            _stream_entry("4-0", filled),
        ]
    )
    materializer = OrderFactMaterializer(postgres, redis_stream, consumer_name="test-consumer")

    processed = asyncio.run(materializer.run_once())

    assert processed == 4
    assert redis_stream.acked == ["1-0", "2-0", "3-0", "4-0"]
    fact = postgres.order_facts["ord-1"]
    assert fact["submit_requested_at"] == requested.ts_event
    assert fact["submit_ack_at"] == acknowledged.ts_event
    assert fact["live_at"] == live.ts_event
    assert fact["filled_at"] == filled.ts_event
    assert fact["terminal_status"] == "FILLED"
    assert fact["filled_size"] == 10.0
    assert fact["remaining_size"] == 0.0


def test_order_fact_materializer_ignores_duplicate_fill_event_ids():
    fill = _event(
        "evt-dup",
        "order_filled",
        order_id="ord-dup",
        payload_json={"side": "BUY", "price": "0.50", "size": "4"},
    )
    postgres = _FakePostgresStore()
    redis_stream = _FakeRedisStreamStore(
        [
            _stream_entry("1-0", fill),
            _stream_entry("2-0", fill),
        ]
    )
    materializer = OrderFactMaterializer(postgres, redis_stream, consumer_name="test-consumer")

    processed = asyncio.run(materializer.run_once())

    assert processed == 2
    fact = postgres.order_facts["ord-dup"]
    assert fact["filled_size"] == 4.0


def test_order_fact_materializer_can_rebuild_from_event_log():
    requested = _event(
        "evt-r1",
        "order_submit_requested",
        payload_json={
            "side": "BUY",
            "price": "0.51",
            "size": "10",
            "post_only": True,
            "order_type": "GTD",
            "expiration": 123,
        },
    )
    acknowledged = _event(
        "evt-r2",
        "order_submit_acknowledged",
        order_id="ord-r1",
        payload_json={
            "side": "BUY",
            "price": "0.51",
            "size": "10",
            "post_only": True,
            "order_type": "GTD",
            "expiration": 123,
        },
    )
    filled = _event(
        "evt-r3",
        "order_filled",
        order_id="ord-r1",
        payload_json={"side": "BUY", "price": "0.51", "size": "10"},
    )

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
        for event in (requested, acknowledged, filled)
    ]
    materializer = OrderFactMaterializer(
        postgres, _FakeRedisStreamStore([]),
        consumer_name="test-consumer",
    )

    rebuilt = asyncio.run(materializer.rebuild_from_event_log())

    assert rebuilt == 3
    fact = postgres.order_facts["ord-r1"]
    assert fact["submit_requested_at"] == requested.ts_event
    assert fact["terminal_status"] == "FILLED"

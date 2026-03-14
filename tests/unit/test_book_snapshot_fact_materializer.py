from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from pmm1.materializers.book_snapshot_fact import BookSnapshotFactMaterializer
from pmm1.storage.spine import SpineEvent


class _FakePostgresStore:
    def __init__(self) -> None:
        self.book_snapshot_facts: dict[str, dict] = {}
        self.spine_events: list[dict] = []

    async def upsert_book_snapshot_fact(self, record: dict) -> None:
        self.book_snapshot_facts[record["snapshot_id"]] = dict(record)

    async def reset_book_snapshot_fact_materialization(self) -> None:
        self.book_snapshot_facts.clear()

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


def _event(event_id: str) -> SpineEvent:
    return SpineEvent(
        event_id=event_id,
        event_type="book_snapshot",
        ts_event=datetime(2026, 3, 13, 12, 0, tzinfo=UTC),
        controller="v1",
        strategy="market_data",
        session_id="20260313_120000",
        git_sha="sha-1",
        config_hash="cfg-1",
        run_stage="shadow",
        condition_id="cond-1",
        token_id="tok-1",
        payload_json={
            "best_bid": 0.49,
            "best_ask": 0.51,
            "mid": 0.50,
            "spread": 0.02,
            "spread_cents": 2.0,
            "depth_best_bid": 100.0,
            "depth_best_ask": 120.0,
            "depth_within_1c": 220.0,
            "depth_within_2c": 280.0,
            "depth_within_5c": 350.0,
            "is_stale": False,
            "tick_size": "0.01",
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


def test_book_snapshot_fact_materializer_consumes_stream():
    snapshot = _event("snap-1")
    postgres = _FakePostgresStore()
    redis_stream = _FakeRedisStreamStore([_stream_entry("1-0", snapshot)])
    materializer = BookSnapshotFactMaterializer(
        postgres, redis_stream, consumer_name="test-consumer"
    )

    processed = asyncio.run(materializer.run_once())

    assert processed == 1
    assert redis_stream.acked == ["1-0"]
    fact = postgres.book_snapshot_facts["snap-1"]
    assert fact["token_id"] == "tok-1"
    assert fact["mid"] == 0.5
    assert fact["depth_within_5c"] == 350.0


def test_book_snapshot_fact_materializer_can_rebuild_from_event_log():
    snapshot_a = _event("snap-a")
    snapshot_b = _event("snap-b")
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
        for event in (snapshot_a, snapshot_b)
    ]
    materializer = BookSnapshotFactMaterializer(postgres, None, consumer_name="test-consumer")

    rebuilt = asyncio.run(materializer.rebuild_from_event_log())

    assert rebuilt == 2
    assert set(postgres.book_snapshot_facts) == {"snap-a", "snap-b"}

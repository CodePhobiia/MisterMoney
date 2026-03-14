from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from pmm1.materializers.quote_fact import QuoteFactMaterializer
from pmm1.storage.spine import SpineEvent


class _FakePostgresStore:
    def __init__(self) -> None:
        self.quote_facts: dict[str, dict] = {}
        self.applied_event_ids: set[str] = set()
        self.spine_events: list[dict] = []

    async def get_quote_fact(self, quote_intent_id: str):
        row = self.quote_facts.get(quote_intent_id)
        return dict(row) if row else None

    async def apply_quote_fact_update(self, event_id: str, record: dict) -> bool:
        if event_id in self.applied_event_ids:
            return False
        self.applied_event_ids.add(event_id)
        self.quote_facts[record["quote_intent_id"]] = dict(record)
        return True

    async def reset_quote_fact_materialization(self) -> None:
        self.quote_facts.clear()
        self.applied_event_ids.clear()

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


def test_quote_fact_materializer_handles_suppressed_quote():
    suppressed = _event(
        "quote-sup-1",
        "quote_side_suppressed",
        payload_json={
            "stage": "yes_quote",
            "side": "BUY",
            "reasons": ["extreme_fair_value"],
            "fair_value": 0.92,
            "inventory": 0.0,
        },
    )
    postgres = _FakePostgresStore()
    redis_stream = _FakeRedisStreamStore([_stream_entry("1-0", suppressed)])
    materializer = QuoteFactMaterializer(postgres, redis_stream, consumer_name="test-consumer")

    processed = asyncio.run(materializer.run_once())

    assert processed == 1
    fact = postgres.quote_facts["quote-sup-1"]
    assert fact["side"] == "BUY"
    assert fact["terminal_status"] == "SUPPRESSED"
    assert fact["suppression_reason_json"] == ["extreme_fair_value"]


def test_quote_fact_materializer_correlates_intent_to_order_outcome():
    intent = _event(
        "quote-1",
        "quote_intent_created",
        payload_json={
            "stage": "yes_quote",
            "side": "BUY",
            "intended_price": 0.51,
            "intended_size": 10.0,
            "fair_value": 0.52,
            "confidence": 0.9,
            "inventory": 0.0,
        },
    )
    submit_requested = _event(
        "quote-req-1",
        "order_submit_requested",
        payload_json={"side": "BUY", "price": "0.51", "size": "10"},
    )
    submit_ack = _event(
        "quote-ack-1",
        "order_submit_acknowledged",
        order_id="ord-1",
        payload_json={"side": "BUY", "price": "0.51", "size": "10"},
    )
    live = _event(
        "quote-live-1",
        "order_live",
        order_id="ord-1",
        payload_json={"side": "BUY", "price": "0.51", "size": "10"},
    )
    filled = _event(
        "quote-fill-1",
        "order_filled",
        order_id="ord-1",
        payload_json={"side": "BUY", "price": "0.51", "size": "10"},
    )

    postgres = _FakePostgresStore()
    redis_stream = _FakeRedisStreamStore(
        [
            _stream_entry("1-0", intent),
            _stream_entry("2-0", submit_requested),
            _stream_entry("3-0", submit_ack),
            _stream_entry("4-0", live),
            _stream_entry("5-0", filled),
        ]
    )
    materializer = QuoteFactMaterializer(postgres, redis_stream, consumer_name="test-consumer")

    processed = asyncio.run(materializer.run_once())

    assert processed == 5
    fact = postgres.quote_facts["quote-1"]
    assert fact["order_id"] == "ord-1"
    assert fact["submitted_price"] == 0.51
    assert fact["submitted_size"] == 10.0
    assert fact["became_live"] is True
    assert fact["filled_any"] is True
    assert fact["filled_full"] is True
    assert fact["terminal_status"] == "FILLED"


def test_quote_fact_materializer_can_rebuild_from_event_log():
    intent = _event(
        "quote-r1",
        "quote_intent_created",
        payload_json={
            "stage": "yes_quote",
            "side": "SELL",
            "intended_price": 0.53,
            "intended_size": 7.0,
        },
    )
    suppressed = _event(
        "quote-r2",
        "quote_side_suppressed",
        payload_json={"stage": "yes_quote", "side": "BUY", "reasons": ["book_missing"]},
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
        for event in (intent, suppressed)
    ]
    materializer = QuoteFactMaterializer(postgres, None, consumer_name="test-consumer")

    rebuilt = asyncio.run(materializer.rebuild_from_event_log())

    assert rebuilt == 2
    assert set(postgres.quote_facts) == {"quote-r1", "quote-r2"}

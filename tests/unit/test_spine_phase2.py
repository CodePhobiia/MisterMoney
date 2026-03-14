from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pmm1.storage.redis import RedisStateStore
from pmm1.storage.spine import SpineEvent
from pmm1.tools.backfill_spine import (
    backfill_legacy_postgres,
    backfill_recordings,
    backfill_sqlite,
    iter_recording_backfill_events,
    map_fill_record_to_event,
    map_legacy_order_to_event,
)


class _FakeSpineStore:
    def __init__(self) -> None:
        self.events = []

    async def append_spine_event(self, event) -> None:
        self.events.append(event)


class _FakeLegacyStore(_FakeSpineStore):
    async def get_legacy_orders(self):
        return [
            {
                "id": 10,
                "order_id": "legacy-order-1",
                "token_id": "tok-legacy",
                "condition_id": "cond-legacy",
                "side": "BUY",
                "price": 0.51,
                "original_size": 12.0,
                "filled_size": 0.0,
                "status": "LIVE",
                "order_type": "GTC",
                "strategy": "mm",
                "neg_risk": False,
                "post_only": True,
                "expiration": 0,
                "error_msg": "",
                "created_at": "2026-03-13T12:00:00+00:00",
                "updated_at": "2026-03-13T12:00:00+00:00",
                "submitted_at": "2026-03-13T12:00:00+00:00",
                "filled_at": None,
                "canceled_at": None,
            }
        ]

    async def get_legacy_fills(self):
        return [
            {
                "id": 11,
                "order_id": "legacy-order-1",
                "token_id": "tok-legacy",
                "condition_id": "cond-legacy",
                "side": "BUY",
                "price": 0.51,
                "size": 12.0,
                "fee": 0.02,
                "transaction_hash": "0xabc",
                "timestamp": "2026-03-13T12:01:00+00:00",
            }
        ]

    async def get_legacy_bot_events(self):
        return [
            {
                "id": 12,
                "event_type": "kill_switch_triggered",
                "details": {"reason": "manual"},
                "timestamp": "2026-03-13T12:02:00+00:00",
            }
        ]


class _FakeRedisClient:
    def __init__(self) -> None:
        self.xadd_calls = []
        self.xgroup_calls = []
        self.xreadgroup_calls = []
        self.xread_calls = []
        self.xack_calls = []

    async def xadd(self, stream_key, fields, **kwargs):
        self.xadd_calls.append((stream_key, fields, kwargs))
        return "1-0"

    async def xgroup_create(self, stream_key, group_name, id="0", mkstream=False):
        self.xgroup_calls.append((stream_key, group_name, id, mkstream))
        return True

    async def xreadgroup(self, group_name, consumer_name, streams, count=100, block=1000):
        self.xreadgroup_calls.append((group_name, consumer_name, streams, count, block))
        stream_key = next(iter(streams.keys()))
        return [(stream_key, [("1-0", {"event_type": "order_filled"})])]

    async def xread(self, streams, count=100, block=1000):
        self.xread_calls.append((streams, count, block))
        stream_key = next(iter(streams.keys()))
        return [(stream_key, [("2-0", {"event_type": "book_snapshot"})])]

    async def xack(self, stream_key, group_name, event_id):
        self.xack_calls.append((stream_key, group_name, event_id))
        return 1


def test_map_fill_record_to_event_has_deterministic_event_id():
    row = {
        "id": 1,
        "ts": "2026-03-13T12:00:00+00:00",
        "condition_id": "cond-1",
        "token_id": "tok-1",
        "order_id": "ord-1",
        "side": "BUY",
        "price": 0.45,
        "size": 10.0,
        "dollar_value": 4.5,
        "fee": 0.01,
        "markout_1s": 0.01,
        "markout_5s": 0.02,
        "markout_30s": 0.03,
        "mid_at_fill": 0.46,
        "is_scoring": 1,
        "reward_eligible": 1,
    }

    event_a = map_fill_record_to_event(row)
    event_b = map_fill_record_to_event(row)

    assert event_a.event_id == event_b.event_id
    assert event_a.event_type == "order_filled"
    assert event_a.payload_json["backfill"]["lineage_complete"] is False


def test_map_legacy_order_to_event_uses_status_to_pick_event_type():
    event = map_legacy_order_to_event(
        {
            "id": 10,
            "order_id": "legacy-order-1",
            "token_id": "tok-1",
            "condition_id": "cond-1",
            "side": "BUY",
            "price": 0.51,
            "original_size": 12.0,
            "filled_size": 0.0,
            "status": "LIVE",
            "order_type": "GTC",
            "strategy": "mm",
            "neg_risk": False,
            "post_only": True,
            "expiration": 0,
            "error_msg": "",
            "created_at": "2026-03-13T12:00:00+00:00",
            "updated_at": "2026-03-13T12:00:00+00:00",
            "submitted_at": "2026-03-13T12:00:00+00:00",
            "filled_at": None,
            "canceled_at": None,
        }
    )

    assert event.event_type == "order_live"
    assert event.order_id == "legacy-order-1"


def test_backfill_sqlite_maps_expected_tables(tmp_path: Path):
    db_path = tmp_path / "pmm1.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE fill_record (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            condition_id TEXT,
            token_id TEXT,
            order_id TEXT,
            side TEXT,
            price REAL,
            size REAL,
            dollar_value REAL,
            fee REAL,
            markout_1s REAL,
            markout_5s REAL,
            markout_30s REAL,
            mid_at_fill REAL,
            is_scoring INTEGER,
            reward_eligible INTEGER
        );
        CREATE TABLE book_snapshot (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            condition_id TEXT,
            token_id TEXT,
            best_bid REAL,
            best_ask REAL,
            bid_depth_5 REAL,
            ask_depth_5 REAL,
            spread_cents REAL,
            mid REAL
        );
        CREATE TABLE shadow_cycle (
            ts TEXT,
            cycle_num INTEGER,
            ready_for_live INTEGER,
            window_cycles INTEGER,
            ev_sample_count INTEGER,
            reward_sample_count INTEGER,
            churn_sample_count INTEGER,
            market_overlap_pct REAL,
            overlap_quote_distance_bps REAL,
            v1_total_ev_usdc REAL,
            pmm2_total_ev_usdc REAL,
            ev_delta_usdc REAL,
            reward_ev_delta_usdc REAL,
            churn_delta_per_order_min REAL,
            gate_blockers_json TEXT,
            gate_diagnostics_json TEXT,
            summary_json TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO fill_record VALUES
        (1, '2026-03-13T12:00:00+00:00', 'cond-1', 'tok-1',
         'ord-1', 'BUY', 0.45, 10.0, 4.5, 0.01,
         0.01, 0.02, 0.03, 0.46, 1, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO book_snapshot VALUES
        (2, '2026-03-13T12:01:00+00:00', 'cond-1', 'tok-1', 0.44, 0.46, 100.0, 120.0, 2.0, 0.45)
        """
    )
    conn.execute(
        """
        INSERT INTO shadow_cycle VALUES
        ('2026-03-13T12:02:00+00:00', 7, 1, 100, 60, 60, 60,
         0.5, 12.0, 1.0, 2.0, 1.0, 0.5, 0.1,
         '[]', '{}', '{}')
        """
    )
    conn.commit()
    conn.close()

    store = _FakeSpineStore()
    counts = asyncio.run(backfill_sqlite(db_path, store))

    assert counts["order_filled"] == 1
    assert counts["book_snapshot"] == 1
    assert counts["pmm2_shadow_cycle"] == 1
    assert len(store.events) == 3


def test_backfill_recordings_maps_supported_event_types(tmp_path: Path):
    recording_dir = tmp_path / "recordings"
    jsonl_dir = recording_dir / "jsonl" / "quote_intent" / "2026-03-13"
    jsonl_dir.mkdir(parents=True)
    quote_file = jsonl_dir / "20260313_120000.jsonl"
    quote_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "_ts": 1_741_870_000.0,
                        "_type": "quote_intent",
                        "token_id": "tok-1",
                        "reservation_price": 0.45,
                        "bid_price": 0.44,
                    }
                ),
                json.dumps(
                    {
                        "_ts": 1_741_870_005.0,
                        "_type": "tick_size_change",
                        "token_id": "tok-1",
                        "new_tick_size": "0.01",
                    }
                ),
                json.dumps(
                    {
                        "_ts": 1_741_870_010.0,
                        "_type": "trade",
                        "token_id": "tok-1",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    events = list(iter_recording_backfill_events(recording_dir))
    assert [event.event_type for event in events] == [
        "quote_intent_created",
        "tick_size_changed",
    ]

    store = _FakeSpineStore()
    counts = asyncio.run(backfill_recordings(recording_dir, store))
    assert counts["quote_intent_created"] == 1
    assert counts["tick_size_changed"] == 1
    assert len(store.events) == 2


def test_backfill_legacy_postgres_maps_orders_fills_and_bot_events():
    store = _FakeLegacyStore()
    counts = asyncio.run(backfill_legacy_postgres(store))

    assert counts["order_live"] == 1
    assert counts["order_filled"] == 1
    assert counts["kill_switch_triggered"] == 1
    assert len(store.events) == 3


def test_redis_state_store_exposes_spine_stream_helpers():
    redis_store = RedisStateStore()
    redis_store._client = _FakeRedisClient()
    event = SpineEvent(
        event_type="order_filled",
        ts_event=datetime(2026, 3, 13, 12, 0, tzinfo=UTC),
        controller="v1",
        strategy="mm",
        session_id="20260313_120000",
        git_sha="sha-1",
        config_hash="cfg-1",
        run_stage="shadow",
        condition_id="cond-1",
        token_id="tok-1",
        order_id="ord-1",
        payload_json={"size": 10},
    )

    stream_event_id = asyncio.run(redis_store.append_spine_stream_event(event))
    assert stream_event_id == "1-0"
    assert redis_store._client.xadd_calls[0][0] == redis_store.KEY_SPINE_STREAM

    asyncio.run(redis_store.ensure_spine_consumer_group("spine-materializers"))
    assert redis_store._client.xgroup_calls[0][1] == "spine-materializers"

    group_events = asyncio.run(
        redis_store.read_spine_stream(group_name="spine-materializers", consumer_name="worker-1")
    )
    assert group_events[0][0] == "1-0"

    stream_events = asyncio.run(redis_store.read_spine_stream(start_id="0"))
    assert stream_events[0][0] == "2-0"

    acked = asyncio.run(
        redis_store.ack_spine_stream_event("1-0", group_name="spine-materializers")
    )
    assert acked == 1

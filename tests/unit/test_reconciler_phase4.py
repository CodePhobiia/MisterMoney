"""Phase 4 reconciler operational hardening tests."""

from __future__ import annotations

import asyncio

from pmm1.execution.reconciler import Reconciler
from pmm1.state.orders import OrderTracker, TrackedOrder
from pmm1.state.positions import PositionTracker
from pmm1.storage.spine import SpineEmitter


class _FakeClobClient:
    async def get_open_orders(self) -> list:
        return []


class _FakeDataClient:
    async def get_positions(self, wallet: str) -> list:
        return []


class _FakeKillSwitch:
    def __init__(self) -> None:
        self.mismatch_calls: list[str] = []
        self.clean_calls = 0

    def report_reconciliation_mismatch(self, details: str = "") -> bool:
        self.mismatch_calls.append(details)
        return False

    def report_reconciliation_clean(self) -> None:
        self.clean_calls += 1


class _FakeSpineStore:
    def __init__(self) -> None:
        self.events = []

    async def append_spine_event(self, event) -> None:
        self.events.append(event)

    async def upsert_config_snapshot(self, snapshot) -> None:
        return None

    async def upsert_model_snapshot(self, snapshot) -> None:
        return None


def test_reconciler_resets_order_mismatch_streak_after_clean_cycle():
    order_tracker = OrderTracker()
    order_tracker.track_submitted(
        TrackedOrder(
            order_id="order-1",
            token_id="token-1",
            condition_id="condition-1",
            side="BUY",
            price="0.50",
            original_size="10",
            strategy="mm",
        )
    )
    reconciler = Reconciler(
        clob_client=_FakeClobClient(),
        data_client=_FakeDataClient(),
        order_tracker=order_tracker,
        position_tracker=PositionTracker(),
    )
    kill_switch = _FakeKillSwitch()
    mismatch_events: list[dict] = []

    async def on_mismatch(**kwargs):
        mismatch_events.append(kwargs)

    reconciler.set_kill_switch(kill_switch)
    reconciler.set_on_mismatch(on_mismatch)

    asyncio.run(reconciler.reconcile_orders())
    assert reconciler.get_stats()["order_mismatch_streak"] == 1
    assert kill_switch.mismatch_calls == []
    assert mismatch_events[0]["kind"] == "orders"

    asyncio.run(reconciler.reconcile_orders())
    stats = reconciler.get_stats()
    assert stats["order_mismatch_streak"] == 0
    assert kill_switch.clean_calls == 1


def test_reconciler_only_trips_kill_switch_after_persistent_order_mismatches():
    kill_switch = _FakeKillSwitch()

    for idx in range(3):
        order_tracker = OrderTracker()
        order_tracker.track_submitted(
            TrackedOrder(
                order_id=f"order-{idx}",
                token_id="token-1",
                condition_id="condition-1",
                side="BUY",
                price="0.50",
                original_size="10",
                strategy="mm",
            )
        )
        reconciler = Reconciler(
            clob_client=_FakeClobClient(),
            data_client=_FakeDataClient(),
            order_tracker=order_tracker,
            position_tracker=PositionTracker(),
        )
        reconciler.set_kill_switch(kill_switch)
        # simulate prior reconcile windows so escalation is allowed
        reconciler._last_order_reconcile = 1.0
        reconciler._order_mismatch_streak = idx

        asyncio.run(reconciler.reconcile_orders())

    assert kill_switch.mismatch_calls == ["orders: 0 unknown, 1 missing"]


def test_reconciler_emits_spine_events_for_clean_and_mismatch_cycles():
    order_tracker = OrderTracker()
    order_tracker.track_submitted(
        TrackedOrder(
            order_id="order-1",
            token_id="token-1",
            condition_id="condition-1",
            side="BUY",
            price="0.50",
            original_size="10",
            strategy="mm",
        )
    )
    spine_store = _FakeSpineStore()
    spine = SpineEmitter(
        spine_store,
        session_id="20260313_120000",
        git_sha="sha-1",
        config_hash="cfg-1",
    )
    reconciler = Reconciler(
        clob_client=_FakeClobClient(),
        data_client=_FakeDataClient(),
        order_tracker=order_tracker,
        position_tracker=PositionTracker(),
        spine_emitter=spine,
    )

    asyncio.run(reconciler.reconcile_orders())
    mismatch_event = spine_store.events[-1]
    assert mismatch_event.event_type == "position_mismatch_detected"
    assert mismatch_event.payload_json["kind"] == "orders"

    # A clean cycle should emit a reconciled event.
    clean_reconciler = Reconciler(
        clob_client=_FakeClobClient(),
        data_client=_FakeDataClient(),
        order_tracker=OrderTracker(),
        position_tracker=PositionTracker(),
        spine_emitter=spine,
    )
    asyncio.run(clean_reconciler.reconcile_orders())
    clean_event = spine_store.events[-1]
    assert clean_event.event_type == "position_reconciled"
    assert clean_event.payload_json["kind"] == "orders"

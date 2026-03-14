from __future__ import annotations

import asyncio
from decimal import Decimal

from pmm1.api.clob_private import CreateOrderRequest, OrderResponse
from pmm1.execution.order_manager import OrderManager
from pmm1.state.orders import OrderState, OrderTracker, TrackedOrder
from pmm1.storage.spine import SpineEmitter
from pmm1.strategy.quote_engine import QuoteIntent


class _FakeSpineStore:
    def __init__(self) -> None:
        self.config_snapshots: list[object] = []
        self.events: list[object] = []
        self.model_snapshots: list[object] = []

    async def upsert_config_snapshot(self, snapshot) -> None:
        self.config_snapshots.append(snapshot)

    async def append_spine_event(self, event) -> None:
        self.events.append(event)

    async def upsert_model_snapshot(self, snapshot) -> None:
        self.model_snapshots.append(snapshot)


class _FakeStreamStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[object] = []
        self.fail = fail

    async def append_spine_stream_event(self, event, *, stream_key=None, maxlen=100000):
        if self.fail:
            raise RuntimeError("stream unavailable")
        self.events.append((event, stream_key, maxlen))
        return "1-0"


class _FakeClobClient:
    def __init__(self, responses: list[list[OrderResponse]] | None = None) -> None:
        self._responses = list(responses or [])
        self.cancel_calls: list[list[str]] = []

    async def create_orders_batch(self, orders: list[CreateOrderRequest]) -> list[OrderResponse]:
        if self._responses:
            return self._responses.pop(0)
        return [
            OrderResponse(
                orderID=f"oid-{idx}", success=True,
                errorMsg="", status="SUBMITTED",
            )
            for idx, _ in enumerate(orders, start=1)
        ]

    async def cancel_orders(self, order_ids: list[str]) -> dict[str, bool]:
        self.cancel_calls.append(list(order_ids))
        return {"success": True}


def _tracked_order(order_id: str, state: OrderState = OrderState.LIVE) -> TrackedOrder:
    return TrackedOrder(
        order_id=order_id,
        token_id="tok-1",
        condition_id="cond-1",
        side="BUY",
        price="0.45",
        original_size="10",
        remaining_size="10",
        state=state,
        strategy="mm",
    )


def test_spine_emitter_persists_redacted_config_snapshot_and_event():
    store = _FakeSpineStore()
    emitter = SpineEmitter(
        store,
        session_id="20260313_120000",
        git_sha="sha-1",
        config_hash="bootstrap",
        default_controller="v1",
        default_run_stage="paper",
    )

    snapshot = asyncio.run(
        emitter.persist_config_snapshot(
            {
                "api": {"api_key": "secret-key"},
                "wallet": {"private_key": "secret-private"},
                "bot": {"paper_mode": True},
            }
        )
    )
    assert snapshot is not None
    assert snapshot.config_json["api"]["api_key"] == "[REDACTED]"
    assert snapshot.config_json["wallet"]["private_key"] == "[REDACTED]"

    asyncio.run(
        emitter.emit_event(
            event_type="order_live",
            strategy="mm",
            condition_id="cond-1",
            token_id="tok-1",
            order_id="order-1",
            payload_json={"price": 0.45},
        )
    )

    assert len(store.config_snapshots) == 1
    assert len(store.events) == 1
    event = store.events[0]
    assert event.session_id == "20260313_120000"
    assert event.git_sha == "sha-1"
    assert event.config_hash == "bootstrap"
    assert event.controller == "v1"
    assert event.run_stage == "paper"


def test_spine_emitter_dual_writes_to_postgres_and_stream():
    store = _FakeSpineStore()
    stream_store = _FakeStreamStore()
    emitter = SpineEmitter(
        store,
        session_id="20260313_120000",
        git_sha="sha-1",
        config_hash="cfg-1",
        stream_store=stream_store,
    )

    ok = asyncio.run(
        emitter.emit_event(
            event_type="order_filled",
            strategy="mm",
            condition_id="cond-1",
            token_id="tok-1",
            order_id="ord-1",
            payload_json={"size": 10},
        )
    )

    assert ok is True
    assert len(store.events) == 1
    assert len(stream_store.events) == 1
    assert stream_store.events[0][0].event_id == store.events[0].event_id


def test_spine_emitter_tolerates_stream_failure_when_postgres_succeeds():
    store = _FakeSpineStore()
    stream_store = _FakeStreamStore(fail=True)
    emitter = SpineEmitter(
        store,
        session_id="20260313_120000",
        git_sha="sha-1",
        config_hash="cfg-1",
        stream_store=stream_store,
    )

    ok = asyncio.run(
        emitter.emit_event(
            event_type="order_filled",
            strategy="mm",
            condition_id="cond-1",
            token_id="tok-1",
            order_id="ord-1",
            payload_json={"size": 10},
        )
    )

    assert ok is True
    assert len(store.events) == 1


def test_order_manager_emits_cancel_and_submit_spine_events():
    tracker = OrderTracker()
    tracker.track(_tracked_order("old-1", state=OrderState.LIVE))
    client = _FakeClobClient(
        responses=[[OrderResponse(orderID="new-1", success=True, errorMsg="", status="SUBMITTED")]]
    )
    spine_store = _FakeSpineStore()
    emitter = SpineEmitter(
        spine_store,
        session_id="20260313_120000",
        git_sha="sha-1",
        config_hash="cfg-1",
    )
    manager = OrderManager(
        client=client,
        order_tracker=tracker,
        use_gtd=False,
        spine_emitter=emitter,
    )

    result = asyncio.run(
        manager.diff_and_apply(
            QuoteIntent(
                token_id="tok-1",
                condition_id="cond-1",
                bid_price=0.55,
                bid_size=10.0,
                strategy="mm",
            ),
            Decimal("0.01"),
        )
    )

    assert result["submitted"] == 1
    assert result["canceled"] == 1
    event_types = [event.event_type for event in spine_store.events]
    assert event_types == [
        "order_cancel_requested",
        "order_submit_requested",
        "order_submit_acknowledged",
    ]
    assert spine_store.events[0].order_id == "old-1"
    assert spine_store.events[1].token_id == "tok-1"
    assert spine_store.events[2].order_id == "new-1"

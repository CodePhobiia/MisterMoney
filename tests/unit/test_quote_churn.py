from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from pmm1.api.clob_private import CreateOrderRequest, OrderResponse, OrderSide, OrderType
from pmm1.execution.order_manager import OrderManager
from pmm1.state.orders import OrderState, OrderTracker, TrackedOrder, zero_lifecycle_counts
from pmm1.strategy.fill_escalation import FillEscalationConfig, FillEscalator
from pmm1.strategy.quote_engine import QuoteIntent


def _make_response(order_id: str = "", success: bool = True, error_msg: str = "") -> OrderResponse:
    return OrderResponse(orderID=order_id, success=success, errorMsg=error_msg, status="SUBMITTED")


class _FakeClobClient:
    def __init__(self, responses: list[list[OrderResponse]] | None = None) -> None:
        self._responses = list(responses or [])
        self.cancel_calls: list[list[str]] = []

    async def create_orders_batch(
        self,
        orders: list[CreateOrderRequest],
    ) -> list[OrderResponse]:
        if self._responses:
            return self._responses.pop(0)
        return [_make_response(order_id=f"oid-{idx}") for idx, _ in enumerate(orders, start=1)]

    async def cancel_orders(self, order_ids: list[str]) -> dict[str, bool]:
        self.cancel_calls.append(list(order_ids))
        return {"success": True}

    async def cancel_all(self) -> dict[str, bool]:
        return {"success": True}


def _tracked_order(
    order_id: str,
    *,
    side: str = "BUY",
    price: str = "0.45",
    size: str = "10",
    state: OrderState = OrderState.LIVE,
    strategy: str = "mm",
    post_only: bool = True,
    origin: str = "submit",
    created_at: float | None = None,
) -> TrackedOrder:
    timestamp = time.time() if created_at is None else created_at
    return TrackedOrder(
        order_id=order_id,
        token_id="tok-1",
        condition_id="cond-1",
        side=side,
        price=price,
        original_size=size,
        remaining_size=size,
        state=state,
        strategy=strategy,
        post_only=post_only,
        origin=origin,
        created_at=timestamp,
        updated_at=timestamp,
        submitted_at=timestamp,
    )


def test_import_exchange_orders_seeds_tracker_without_lifecycle_drift() -> None:
    tracker = OrderTracker()
    baseline = tracker.snapshot_lifecycle_counts()

    imported = tracker.import_exchange_orders([
        {
            "orderID": "ex-1",
            "market": "cond-1",
            "asset_id": "tok-1",
            "side": "BUY",
            "price": "0.44",
            "originalSize": "12",
            "sizeMatched": "0",
            "status": "LIVE",
        }
    ], source="startup_sync")

    assert imported == ["ex-1"]
    tracked = tracker.get("ex-1")
    assert tracked is not None
    assert tracked.state == OrderState.LIVE
    assert tracked.origin == "startup_sync"
    assert tracker.diff_lifecycle_counts(baseline) == zero_lifecycle_counts()


def test_reconcile_imports_unknown_exchange_orders() -> None:
    tracker = OrderTracker()

    mismatches = tracker.reconcile_with_exchange([
        {
            "orderID": "restored-1",
            "market": "cond-1",
            "asset_id": "tok-1",
            "side": "SELL",
            "price": "0.51",
            "originalSize": "9",
            "sizeMatched": "0",
            "status": "LIVE",
        }
    ])

    assert mismatches["unknown_on_exchange"] == ["restored-1"]
    assert mismatches["imported_from_exchange"] == ["restored-1"]
    tracked = tracker.get("restored-1")
    assert tracked is not None
    assert tracked.state == OrderState.LIVE
    assert tracked.side == "SELL"


def test_diff_and_apply_cancels_duplicate_live_orders_without_resubmitting() -> None:
    tracker = OrderTracker()
    newer = _tracked_order("live-new", price="0.55", created_at=time.time())
    older = _tracked_order(
        "live-old",
        price="0.55",
        origin="startup_sync",
        created_at=time.time() - 10,
    )
    tracker.track(newer)
    tracker.track(older)
    client = _FakeClobClient()
    manager = OrderManager(client=client, order_tracker=tracker, use_gtd=False)

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

    assert result["submitted"] == 0
    assert result["canceled"] == 1
    assert result["unchanged"] == 1
    assert result["replacement_reason_counts"]["duplicate_live_order"] == 1
    assert result["replacement_reason_counts"]["reconcile_mismatch"] == 1
    assert tracker.get("live-new").state == OrderState.LIVE
    assert tracker.get("live-old").state == OrderState.CANCELED


def test_diff_and_apply_reports_ttl_expired_replacements() -> None:
    tracker = OrderTracker()
    stale_order = _tracked_order(
        "stale-1",
        price="0.55",
        created_at=time.time() - 120,
    )
    tracker.track(stale_order)
    client = _FakeClobClient(responses=[[_make_response(order_id="fresh-1")]])
    manager = OrderManager(
        client=client,
        order_tracker=tracker,
        use_gtd=False,
        order_ttl_s=30,
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
    assert result["replacement_reason_counts"]["ttl_expired"] == 1
    assert tracker.get("stale-1").state == OrderState.CANCELED
    assert tracker.get("fresh-1").state == OrderState.SUBMITTED


def test_submit_exit_keeps_existing_exit_order() -> None:
    tracker = OrderTracker()
    tracker.track(
        _tracked_order(
            "exit-live",
            side="SELL",
            price="0.50",
            strategy="exit",
            post_only=False,
        )
    )
    manager = OrderManager(client=_FakeClobClient(), order_tracker=tracker, use_gtd=False)

    result = asyncio.run(
        manager.submit_exit(
            token_id="tok-1",
            condition_id="cond-1",
            price=0.50,
            size=10.0,
            tick_size=Decimal("0.01"),
            urgency="low",
            neg_risk=False,
        )
    )

    assert result["kept"] is True
    assert result["submitted"] is False
    assert result["canceled"] == 0
    assert result["unchanged"] == 1


def test_submit_exit_replaces_passive_mm_order() -> None:
    tracker = OrderTracker()
    tracker.track(
        _tracked_order(
            "mm-ask",
            side="SELL",
            price="0.50",
            strategy="mm",
            post_only=True,
        )
    )
    client = _FakeClobClient(responses=[[_make_response(order_id="exit-1")]])
    manager = OrderManager(client=client, order_tracker=tracker, use_gtd=False)

    result = asyncio.run(
        manager.submit_exit(
            token_id="tok-1",
            condition_id="cond-1",
            price=0.50,
            size=10.0,
            tick_size=Decimal("0.01"),
            urgency="low",
            neg_risk=False,
        )
    )

    assert result["submitted"] is True
    assert result["canceled"] == 1
    assert result["replacement_reason_counts"]["exit_replace"] == 1
    assert tracker.get("mm-ask").state == OrderState.CANCELED
    new_exit = tracker.get("exit-1")
    assert new_exit is not None
    assert new_exit.strategy == "exit"
    assert new_exit.post_only is False


def test_diff_and_apply_uses_side_specific_suppression_reasons() -> None:
    tracker = OrderTracker()
    tracker.track(_tracked_order("bid-live", side="BUY", price="0.45"))
    tracker.track(_tracked_order("ask-live", side="SELL", price="0.55"))
    manager = OrderManager(client=_FakeClobClient(), order_tracker=tracker, use_gtd=False)

    result = asyncio.run(
        manager.diff_and_apply(
            QuoteIntent(
                token_id="tok-1",
                condition_id="cond-1",
                strategy="mm",
                bid_suppression_reasons=["book_stale"],
                ask_suppression_reasons=["no_inventory_to_offer"],
            ),
            Decimal("0.01"),
        )
    )

    assert result["canceled"] == 2
    assert result["submitted"] == 0
    assert result["replacement_reason_counts"]["book_stale"] == 1
    assert result["replacement_reason_counts"]["no_inventory_to_offer"] == 1
    assert tracker.get("bid-live").state == OrderState.CANCELED
    assert tracker.get("ask-live").state == OrderState.CANCELED


def test_fill_escalator_allows_single_taker_trigger_until_fill() -> None:
    escalator = FillEscalator(
        FillEscalationConfig(
            taker_trigger_secs=10,
            taker_enabled=True,
            enabled=True,
        )
    )
    escalator.last_fill_time -= 20

    assert escalator.should_take_liquidity() is True
    assert escalator.should_take_liquidity() is False

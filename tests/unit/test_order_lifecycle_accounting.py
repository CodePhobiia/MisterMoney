from __future__ import annotations

import asyncio
from decimal import Decimal

from pmm1.api.clob_private import CreateOrderRequest, OrderResponse, OrderSide, OrderType
from pmm1.execution.mutation_guard import MutationGuardDecision
from pmm1.execution.order_manager import OrderManager
from pmm1.state.orders import OrderState, OrderTracker, TrackedOrder
from pmm1.strategy.quote_engine import QuoteIntent


def _make_tracked_order(order_id: str, state: OrderState = OrderState.INTENT) -> TrackedOrder:
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


def _make_order_response(
    *,
    order_id: str = "",
    success: bool = True,
    error_msg: str = "",
) -> OrderResponse:
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
        return [
            _make_order_response(order_id=f"oid-{idx}")
            for idx, _ in enumerate(orders, start=1)
        ]

    async def cancel_orders(self, order_ids: list[str]) -> dict[str, bool]:
        self.cancel_calls.append(list(order_ids))
        return {"success": True}

    async def cancel_all(self) -> dict[str, bool]:
        return {"success": True}


def test_track_submitted_sets_canonical_counters() -> None:
    tracker = OrderTracker()
    baseline = tracker.snapshot_lifecycle_counts()

    assert tracker.track_submitted(_make_tracked_order("sub-1"), source="mm") is True

    tracked = tracker.get("sub-1")
    assert tracked is not None
    assert tracked.state == OrderState.SUBMITTED
    assert tracked.submitted_at is not None
    assert tracker.diff_lifecycle_counts(baseline)["submitted"] == 1


def test_reconcile_missing_order_counts_cancel() -> None:
    tracker = OrderTracker()
    tracker.track(_make_tracked_order("live-1", state=OrderState.LIVE))
    baseline = tracker.snapshot_lifecycle_counts()

    tracker.reconcile_with_exchange([])

    tracked = tracker.get("live-1")
    assert tracked is not None
    assert tracked.state == OrderState.CANCELED
    assert tracker.diff_lifecycle_counts(baseline)["canceled"] == 1


def test_diff_and_apply_counts_successful_submit_and_cancel() -> None:
    tracker = OrderTracker()
    tracker.track(_make_tracked_order("old-1", state=OrderState.LIVE))
    client = _FakeClobClient(responses=[[_make_order_response(order_id="new-1", success=True)]])
    manager = OrderManager(client=client, order_tracker=tracker, use_gtd=False)
    baseline = tracker.snapshot_lifecycle_counts()

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
    assert result["rejected"] == 0
    assert tracker.get("old-1").state == OrderState.CANCELED
    assert tracker.get("new-1").state == OrderState.SUBMITTED
    assert tracker.diff_lifecycle_counts(baseline)["submitted"] == 1
    assert tracker.diff_lifecycle_counts(baseline)["canceled"] == 1


def test_diff_and_apply_does_not_count_rejected_response_as_submitted() -> None:
    tracker = OrderTracker()
    client = _FakeClobClient(
        responses=[[_make_order_response(success=False, error_msg="rejected")]]
    )
    manager = OrderManager(client=client, order_tracker=tracker, use_gtd=False)
    baseline = tracker.snapshot_lifecycle_counts()

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
    assert result["rejected"] == 1
    assert tracker.total_tracked == 0
    assert tracker.diff_lifecycle_counts(baseline)["submitted"] == 0


def test_submit_order_tracks_non_quote_submission_paths() -> None:
    tracker = OrderTracker()
    client = _FakeClobClient(responses=[[_make_order_response(order_id="taker-1", success=True)]])
    manager = OrderManager(client=client, order_tracker=tracker, use_gtd=False)
    baseline = tracker.snapshot_lifecycle_counts()

    result = asyncio.run(
        manager.submit_order(
            CreateOrderRequest(
                token_id="tok-1",
                price="0.61",
                size="7",
                side=OrderSide.BUY,
                order_type=OrderType.FAK,
                post_only=False,
            ),
            condition_id="cond-1",
            strategy="taker_bootstrap",
        )
    )

    tracked = tracker.get("taker-1")
    assert result["success"] is True
    assert tracked is not None
    assert tracked.strategy == "taker_bootstrap"
    assert tracker.diff_lifecycle_counts(baseline)["submitted"] == 1


def test_submit_order_blocks_when_mutation_guard_denies() -> None:
    tracker = OrderTracker()
    client = _FakeClobClient()
    manager = OrderManager(client=client, order_tracker=tracker, use_gtd=False)
    manager.set_mutation_guard(
        lambda request, condition_id, strategy: MutationGuardDecision(
            allowed=False,
            reason="resume_token_invalid",
            details={"blocked_reason": "market_ws_reconnect"},
        )
    )

    result = asyncio.run(
        manager.submit_order(
            CreateOrderRequest(
                token_id="tok-1",
                price="0.61",
                size="7",
                side=OrderSide.BUY,
                order_type=OrderType.FAK,
                post_only=False,
            ),
            condition_id="cond-1",
            strategy="taker_bootstrap",
        )
    )

    assert result["success"] is False
    assert result["error"] == "blocked_by_guard:resume_token_invalid"
    assert tracker.total_tracked == 0

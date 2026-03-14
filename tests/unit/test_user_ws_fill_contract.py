from __future__ import annotations

from pmm1.state.orders import OrderState, OrderTracker, TrackedOrder
from pmm1.ws.user_ws import UserWebSocket


def _tracked_order(order_id: str) -> TrackedOrder:
    return TrackedOrder(
        order_id=order_id,
        token_id="token-1",
        condition_id="condition-1",
        side="BUY",
        price="0.45",
        original_size="10",
        remaining_size="10",
        state=OrderState.LIVE,
        strategy="mm",
    )


def test_trade_update_normalization_ignores_generic_trade_id() -> None:
    tracker = OrderTracker()
    tracker.track(_tracked_order("order-1"))
    ws = UserWebSocket(order_tracker=tracker)

    event = ws._normalize_trade_update(
        {
            "id": "trade-uuid",
            "makerOrderID": "order-1",
            "matchSize": "4",
            "matchPrice": "0.51",
        }
    )

    assert event is not None
    assert event["order_id"] == "order-1"
    assert event["condition_id"] == "condition-1"
    assert event["token_id"] == "token-1"
    assert event["price"] == 0.51
    assert event["size"] == 4.0
    assert event["exchange_trade_id"] == "trade-uuid"
    assert event["fill_identity"] == "trade-uuid"


def test_trade_update_normalization_uses_tracked_defaults() -> None:
    tracker = OrderTracker()
    tracker.track(_tracked_order("order-2"))
    ws = UserWebSocket(order_tracker=tracker)

    event = ws._normalize_trade_update(
        {
            "orderID": "order-2",
            "size": "3",
            "price": "0.47",
        }
    )

    assert event is not None
    assert event["side"] == "BUY"
    assert event["token_id"] == "token-1"
    assert event["condition_id"] == "condition-1"
    assert event["fill_identity"]


def test_trade_update_normalization_extracts_explicit_fee() -> None:
    tracker = OrderTracker()
    tracker.track(_tracked_order("order-3"))
    ws = UserWebSocket(order_tracker=tracker)

    event = ws._normalize_trade_update(
        {
            "orderID": "order-3",
            "size": "2",
            "price": "0.50",
            "feeAmount": "0.04",
        }
    )

    assert event is not None
    assert event["fee_amount"] == 0.04
    assert event["fee_source"] == "payload"

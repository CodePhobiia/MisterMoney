"""Tests for order state machine — transitions, terminal rejection (T0-04)."""

import pytest

from pmm1.state.orders import (
    OrderState,
    TrackedOrder,
    OrderTracker,
    TERMINAL_STATES,
    ACTIVE_STATES,
)


class TestTrackedOrderTransitions:
    """Order state machine transition tests."""

    def _make_order(self, state: OrderState = OrderState.INTENT) -> TrackedOrder:
        return TrackedOrder(
            order_id="test-001",
            token_id="tok-yes",
            condition_id="cond-001",
            side="BUY",
            price="0.50",
            original_size="10",
            remaining_size="10",
            state=state,
        )

    def test_valid_intent_to_signed(self):
        order = self._make_order(OrderState.INTENT)
        assert order.transition_to(OrderState.SIGNED) is True
        assert order.state == OrderState.SIGNED

    def test_valid_signed_to_submitted(self):
        order = self._make_order(OrderState.SIGNED)
        assert order.transition_to(OrderState.SUBMITTED) is True
        assert order.state == OrderState.SUBMITTED

    def test_valid_submitted_to_live(self):
        order = self._make_order(OrderState.SUBMITTED)
        assert order.transition_to(OrderState.LIVE) is True
        assert order.state == OrderState.LIVE

    def test_valid_live_to_filled(self):
        order = self._make_order(OrderState.LIVE)
        assert order.transition_to(OrderState.FILLED) is True
        assert order.state == OrderState.FILLED

    def test_valid_live_to_canceled(self):
        order = self._make_order(OrderState.LIVE)
        assert order.transition_to(OrderState.CANCELED) is True
        assert order.state == OrderState.CANCELED

    # T0-04: Terminal → active must be REJECTED
    def test_rejected_filled_to_live(self):
        order = self._make_order(OrderState.FILLED)
        assert order.transition_to(OrderState.LIVE) is False
        assert order.state == OrderState.FILLED  # Stays terminal

    def test_rejected_canceled_to_live(self):
        order = self._make_order(OrderState.CANCELED)
        assert order.transition_to(OrderState.LIVE) is False
        assert order.state == OrderState.CANCELED

    def test_rejected_expired_to_submitted(self):
        order = self._make_order(OrderState.EXPIRED)
        assert order.transition_to(OrderState.SUBMITTED) is False
        assert order.state == OrderState.EXPIRED

    def test_rejected_failed_to_signed(self):
        order = self._make_order(OrderState.FAILED)
        assert order.transition_to(OrderState.SIGNED) is False
        assert order.state == OrderState.FAILED

    def test_noop_same_state(self):
        order = self._make_order(OrderState.LIVE)
        assert order.transition_to(OrderState.LIVE) is True
        assert order.state == OrderState.LIVE

    def test_timestamps_set_on_submit(self):
        order = self._make_order(OrderState.SIGNED)
        order.transition_to(OrderState.SUBMITTED)
        assert order.submitted_at is not None

    def test_timestamps_set_on_fill(self):
        order = self._make_order(OrderState.LIVE)
        order.transition_to(OrderState.FILLED)
        assert order.filled_at is not None

    def test_timestamps_set_on_cancel(self):
        order = self._make_order(OrderState.LIVE)
        order.transition_to(OrderState.CANCELED)
        assert order.canceled_at is not None


class TestTrackedOrderProperties:
    def test_is_terminal(self):
        for state in TERMINAL_STATES:
            order = TrackedOrder(state=state, order_id="t")
            assert order.is_terminal is True

    def test_is_active(self):
        for state in ACTIVE_STATES:
            order = TrackedOrder(state=state, order_id="t")
            assert order.is_active is True

    def test_price_float(self):
        order = TrackedOrder(price="0.55", order_id="t")
        assert order.price_float == 0.55

    def test_is_buy(self):
        order = TrackedOrder(side="BUY", order_id="t")
        assert order.is_buy is True
        order2 = TrackedOrder(side="SELL", order_id="t")
        assert order2.is_buy is False


class TestTrackedOrderApplyFill:
    def test_partial_fill(self):
        order = TrackedOrder(
            order_id="test",
            original_size="10",
            remaining_size="10",
            filled_size="0",
            state=OrderState.LIVE,
        )
        order.apply_fill("3", "0.50")
        assert order.filled_size_float == 3.0
        assert order.remaining_size_float == 7.0
        assert order.state == OrderState.PARTIAL

    def test_full_fill(self):
        order = TrackedOrder(
            order_id="test",
            original_size="10",
            remaining_size="10",
            filled_size="0",
            state=OrderState.LIVE,
        )
        order.apply_fill("10", "0.50")
        assert order.filled_size_float == 10.0
        assert order.state == OrderState.FILLED


class TestOrderTracker:
    def test_track_and_get(self):
        tracker = OrderTracker()
        order = TrackedOrder(order_id="abc", token_id="tok1", side="BUY", state=OrderState.LIVE)
        tracker.track(order)
        assert tracker.get("abc") is order

    def test_get_active_orders(self):
        tracker = OrderTracker()
        o1 = TrackedOrder(order_id="a1", token_id="tok1", state=OrderState.LIVE)
        o2 = TrackedOrder(order_id="a2", token_id="tok1", state=OrderState.FILLED)
        tracker.track(o1)
        tracker.track(o2)
        actives = tracker.get_active_orders()
        assert len(actives) == 1
        assert actives[0].order_id == "a1"

    def test_count_active(self):
        tracker = OrderTracker()
        tracker.track(TrackedOrder(order_id="a1", token_id="t1", side="BUY", state=OrderState.LIVE))
        tracker.track(TrackedOrder(order_id="a2", token_id="t1", side="SELL", state=OrderState.LIVE))
        assert tracker.count_active("t1") == 2
        assert tracker.count_active("t1", "BUY") == 1

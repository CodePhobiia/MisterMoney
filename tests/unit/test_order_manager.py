"""Tests for OrderManager — MM-06 queue-aware repricing + PM-10 post-fill cancel."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from pmm1.api.clob_private import CreateOrderRequest, OrderResponse
from pmm1.execution.order_manager import OrderManager
from pmm1.state.orders import OrderState, OrderTracker, TrackedOrder
from pmm1.strategy.quote_engine import QuoteIntent


def _make_response(
    order_id: str = "", success: bool = True, error_msg: str = ""
) -> OrderResponse:
    return OrderResponse(
        orderID=order_id, success=success, errorMsg=error_msg, status="SUBMITTED"
    )


class _FakeClobClient:
    def __init__(
        self, responses: list[list[OrderResponse]] | None = None
    ) -> None:
        self._responses = list(responses or [])
        self.cancel_calls: list[list[str]] = []
        self.cancel_order_calls: list[str] = []

    async def create_orders_batch(
        self,
        orders: list[CreateOrderRequest],
    ) -> list[OrderResponse]:
        if self._responses:
            return self._responses.pop(0)
        return [
            _make_response(order_id=f"oid-{idx}")
            for idx, _ in enumerate(orders, start=1)
        ]

    async def cancel_orders(self, order_ids: list[str]) -> dict[str, bool]:
        self.cancel_calls.append(list(order_ids))
        return {"success": True}

    async def cancel_order(self, order_id: str) -> dict[str, bool]:
        self.cancel_order_calls.append(order_id)
        return {"success": True}

    async def cancel_all(self) -> dict[str, bool]:
        return {"success": True}


def _tracked_order(
    order_id: str,
    *,
    token_id: str = "tok-1",
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
        token_id=token_id,
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


# ---------------------------------------------------------------------------
# MM-06: Passive reprice threshold (2 ticks)
# ---------------------------------------------------------------------------


class TestPassiveRepriceThreshold:
    """MM-06: Passive orders (bid < mid, ask > mid) use the wider 2-tick threshold."""

    def test_passive_bid_within_2_ticks_kept(self) -> None:
        """A passive bid that moved < 2 ticks from desired should NOT be replaced."""
        tracker = OrderTracker()
        # Live bid at 0.45, mid is 0.50 => passive (bid < mid)
        tracker.track(_tracked_order("bid-1", price="0.45"))
        client = _FakeClobClient()
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        # Desired bid at 0.46 — only 1 tick away from live 0.45
        result = asyncio.run(
            manager.diff_and_apply(
                QuoteIntent(
                    token_id="tok-1",
                    condition_id="cond-1",
                    bid_price=0.46,
                    bid_size=10.0,
                    reservation_price=0.50,
                    strategy="mm",
                ),
                Decimal("0.01"),
            )
        )

        # With passive threshold = 2 ticks, 1 tick move should be kept
        assert result["unchanged"] >= 1
        assert result["canceled"] == 0
        assert result["submitted"] == 0

    def test_passive_bid_at_2_ticks_replaced(self) -> None:
        """A passive bid that moved >= 2 ticks should be replaced."""
        tracker = OrderTracker()
        # Live bid at 0.45, mid is 0.50 => passive
        tracker.track(_tracked_order("bid-1", price="0.45"))
        client = _FakeClobClient(responses=[[_make_response(order_id="bid-new")]])
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        # Desired bid at 0.47 — 2 ticks away from live 0.45
        result = asyncio.run(
            manager.diff_and_apply(
                QuoteIntent(
                    token_id="tok-1",
                    condition_id="cond-1",
                    bid_price=0.47,
                    bid_size=10.0,
                    reservation_price=0.50,
                    strategy="mm",
                ),
                Decimal("0.01"),
            )
        )

        assert result["canceled"] == 1
        assert result["submitted"] == 1
        assert "price_move" in result["replacement_reason_counts"]

    def test_passive_ask_within_2_ticks_kept(self) -> None:
        """A passive ask that moved < 2 ticks should NOT be replaced."""
        tracker = OrderTracker()
        # Live ask at 0.55, mid is 0.50 => passive (ask > mid)
        tracker.track(
            _tracked_order("ask-1", side="SELL", price="0.55")
        )
        client = _FakeClobClient()
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        # Desired ask at 0.54 — only 1 tick away from live 0.55
        result = asyncio.run(
            manager.diff_and_apply(
                QuoteIntent(
                    token_id="tok-1",
                    condition_id="cond-1",
                    ask_price=0.54,
                    ask_size=10.0,
                    reservation_price=0.50,
                    strategy="mm",
                ),
                Decimal("0.01"),
            )
        )

        assert result["unchanged"] >= 1
        assert result["canceled"] == 0
        assert result["submitted"] == 0


# ---------------------------------------------------------------------------
# MM-06: Aggressive reprice threshold (1 tick, the default)
# ---------------------------------------------------------------------------


class TestAggressiveRepriceThreshold:
    """MM-06: Off-market / aggressive orders still use the 1-tick threshold."""

    def test_aggressive_bid_above_mid_uses_1_tick(self) -> None:
        """A bid at or above mid is aggressive and should use the 1-tick threshold."""
        tracker = OrderTracker()
        # Live bid at 0.51, mid is 0.50 => aggressive (bid >= mid)
        tracker.track(_tracked_order("bid-agg", price="0.51"))
        client = _FakeClobClient(responses=[[_make_response(order_id="bid-new")]])
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        # Desired bid at 0.52 — 1 tick away, should trigger at aggressive threshold
        result = asyncio.run(
            manager.diff_and_apply(
                QuoteIntent(
                    token_id="tok-1",
                    condition_id="cond-1",
                    bid_price=0.52,
                    bid_size=10.0,
                    reservation_price=0.50,
                    strategy="mm",
                ),
                Decimal("0.01"),
            )
        )

        assert result["canceled"] == 1
        assert result["submitted"] == 1
        assert "price_move" in result["replacement_reason_counts"]

    def test_aggressive_ask_below_mid_uses_1_tick(self) -> None:
        """An ask at or below mid is aggressive and uses 1-tick threshold."""
        tracker = OrderTracker()
        # Live ask at 0.49, mid is 0.50 => aggressive (ask <= mid)
        tracker.track(
            _tracked_order("ask-agg", side="SELL", price="0.49")
        )
        client = _FakeClobClient(responses=[[_make_response(order_id="ask-new")]])
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        # Desired ask at 0.48 — 1 tick away, aggressive threshold
        result = asyncio.run(
            manager.diff_and_apply(
                QuoteIntent(
                    token_id="tok-1",
                    condition_id="cond-1",
                    ask_price=0.48,
                    ask_size=10.0,
                    reservation_price=0.50,
                    strategy="mm",
                ),
                Decimal("0.01"),
            )
        )

        assert result["canceled"] == 1
        assert result["submitted"] == 1
        assert "price_move" in result["replacement_reason_counts"]

    def test_no_mid_price_falls_back_to_aggressive_threshold(self) -> None:
        """Without reservation_price, all orders use the 1-tick threshold."""
        tracker = OrderTracker()
        tracker.track(_tracked_order("bid-1", price="0.45"))
        client = _FakeClobClient(responses=[[_make_response(order_id="bid-new")]])
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        # No reservation_price (defaults to 0.0) => aggressive threshold
        result = asyncio.run(
            manager.diff_and_apply(
                QuoteIntent(
                    token_id="tok-1",
                    condition_id="cond-1",
                    bid_price=0.46,
                    bid_size=10.0,
                    strategy="mm",
                ),
                Decimal("0.01"),
            )
        )

        # 1 tick move triggers at aggressive threshold
        assert result["canceled"] == 1
        assert result["submitted"] == 1


# ---------------------------------------------------------------------------
# MM-06: Queue-aware — worse price skip unless >3 ticks off-market
# ---------------------------------------------------------------------------


class TestQueueAwareWorsePrice:
    """MM-06: When the new price is further from mid, skip cancel unless >3 ticks off."""

    def test_worse_price_within_3_ticks_skipped(self) -> None:
        """Moving a passive bid further from mid when it's within 3 ticks should be skipped."""
        tracker = OrderTracker()
        # Live bid at 0.48, mid is 0.50 => passive, 2 ticks from mid
        tracker.track(_tracked_order("bid-1", price="0.48"))
        client = _FakeClobClient()
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        # Desired bid at 0.44 — 4 ticks diff, but new price is WORSE (0.44 is further
        # from mid=0.50 than 0.48). Live is only 2 ticks from mid, so skip.
        result = asyncio.run(
            manager.diff_and_apply(
                QuoteIntent(
                    token_id="tok-1",
                    condition_id="cond-1",
                    bid_price=0.44,
                    bid_size=10.0,
                    reservation_price=0.50,
                    strategy="mm",
                ),
                Decimal("0.01"),
            )
        )

        assert result["unchanged"] >= 1
        assert result["canceled"] == 0
        assert result["submitted"] == 0

    def test_worse_price_beyond_3_ticks_replaced(self) -> None:
        """When live order is >3 ticks off-market, even a worse move should trigger cancel."""
        tracker = OrderTracker()
        # Live bid at 0.45, mid is 0.50 => passive, 5 ticks off-market (> 3)
        tracker.track(_tracked_order("bid-1", price="0.45"))
        client = _FakeClobClient(responses=[[_make_response(order_id="bid-new")]])
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        # Desired bid at 0.42 — worse price, but live is 5 ticks off-market
        result = asyncio.run(
            manager.diff_and_apply(
                QuoteIntent(
                    token_id="tok-1",
                    condition_id="cond-1",
                    bid_price=0.42,
                    bid_size=10.0,
                    reservation_price=0.50,
                    strategy="mm",
                ),
                Decimal("0.01"),
            )
        )

        assert result["canceled"] == 1
        assert result["submitted"] == 1
        assert "price_move" in result["replacement_reason_counts"]


# ---------------------------------------------------------------------------
# MM-06: Extended TTL (60s default)
# ---------------------------------------------------------------------------


class TestExtendedTTL:
    """MM-06: TTL default changed from 30s to 60s."""

    def test_default_ttl_is_60(self) -> None:
        """Verify the default order_ttl_s is 60."""
        tracker = OrderTracker()
        client = _FakeClobClient()
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )
        assert manager._order_ttl_s == 60

    def test_order_at_45s_not_expired_with_default_ttl(self) -> None:
        """An order that is 45s old should NOT be expired under the new 60s TTL."""
        tracker = OrderTracker()
        tracker.track(
            _tracked_order(
                "bid-1",
                price="0.45",
                created_at=time.time() - 45,
            )
        )
        client = _FakeClobClient()
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        result = asyncio.run(
            manager.diff_and_apply(
                QuoteIntent(
                    token_id="tok-1",
                    condition_id="cond-1",
                    bid_price=0.45,
                    bid_size=10.0,
                    strategy="mm",
                ),
                Decimal("0.01"),
            )
        )

        # 45s < 60s TTL, order should be kept
        assert result["unchanged"] >= 1
        assert result["canceled"] == 0

    def test_order_at_65s_expired_with_default_ttl(self) -> None:
        """An order that is 65s old SHOULD be expired under the new 60s TTL."""
        tracker = OrderTracker()
        tracker.track(
            _tracked_order(
                "bid-1",
                price="0.45",
                created_at=time.time() - 65,
            )
        )
        client = _FakeClobClient(responses=[[_make_response(order_id="bid-new")]])
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        result = asyncio.run(
            manager.diff_and_apply(
                QuoteIntent(
                    token_id="tok-1",
                    condition_id="cond-1",
                    bid_price=0.45,
                    bid_size=10.0,
                    strategy="mm",
                ),
                Decimal("0.01"),
            )
        )

        assert result["canceled"] == 1
        assert "ttl_expired" in result["replacement_reason_counts"]


# ---------------------------------------------------------------------------
# MM-06: Size threshold (30%)
# ---------------------------------------------------------------------------


class TestSizeThreshold:
    """MM-06: reprice_threshold_size_pct changed from 0.20 to 0.30."""

    def test_default_size_threshold_is_30_pct(self) -> None:
        """Verify the default size threshold is 0.30."""
        tracker = OrderTracker()
        client = _FakeClobClient()
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )
        assert manager._reprice_threshold_size_pct == 0.30

    def test_25_pct_size_change_not_replaced(self) -> None:
        """A 25% size change should NOT trigger replacement under the new 30% threshold."""
        tracker = OrderTracker()
        # Live bid: size=10
        tracker.track(_tracked_order("bid-1", price="0.45", size="10"))
        client = _FakeClobClient()
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        # Desired size=7.5 => 25% change
        result = asyncio.run(
            manager.diff_and_apply(
                QuoteIntent(
                    token_id="tok-1",
                    condition_id="cond-1",
                    bid_price=0.45,
                    bid_size=7.5,
                    strategy="mm",
                ),
                Decimal("0.01"),
            )
        )

        assert result["unchanged"] >= 1
        assert result["canceled"] == 0

    def test_35_pct_size_change_replaced(self) -> None:
        """A 35% size change SHOULD trigger replacement under the new 30% threshold."""
        tracker = OrderTracker()
        # Live bid: size=10
        tracker.track(_tracked_order("bid-1", price="0.45", size="10"))
        client = _FakeClobClient(responses=[[_make_response(order_id="bid-new")]])
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        # Desired size=6.5 => 35% change
        result = asyncio.run(
            manager.diff_and_apply(
                QuoteIntent(
                    token_id="tok-1",
                    condition_id="cond-1",
                    bid_price=0.45,
                    bid_size=6.5,
                    strategy="mm",
                ),
                Decimal("0.01"),
            )
        )

        assert result["canceled"] == 1
        assert result["submitted"] == 1
        assert "size_move" in result["replacement_reason_counts"]


# ---------------------------------------------------------------------------
# PM-10: Post-fill stale quote protection
# ---------------------------------------------------------------------------


class TestCancelOpposingOnFill:
    """PM-10: Cancel opposing side orders after a fill to prevent stale quote exposure."""

    def test_buy_fill_cancels_sell_orders(self) -> None:
        """After a BUY fill, all active SELL orders for that token should be canceled."""
        tracker = OrderTracker()
        tracker.track(
            _tracked_order("ask-1", side="SELL", price="0.55")
        )
        tracker.track(
            _tracked_order("ask-2", side="SELL", price="0.56")
        )
        # A BUY order that should NOT be canceled
        tracker.track(
            _tracked_order("bid-1", side="BUY", price="0.44")
        )
        client = _FakeClobClient()
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        asyncio.run(manager.cancel_opposing_on_fill("tok-1", "BUY"))

        # Both SELL orders should have been canceled individually
        assert len(client.cancel_order_calls) == 2
        assert "ask-1" in client.cancel_order_calls
        assert "ask-2" in client.cancel_order_calls
        assert tracker.get("ask-1").state == OrderState.CANCELED
        assert tracker.get("ask-2").state == OrderState.CANCELED
        # BUY order untouched
        assert tracker.get("bid-1").state == OrderState.LIVE

    def test_sell_fill_cancels_buy_orders(self) -> None:
        """After a SELL fill, all active BUY orders for that token should be canceled."""
        tracker = OrderTracker()
        tracker.track(
            _tracked_order("bid-1", side="BUY", price="0.44")
        )
        # A SELL order that should NOT be canceled
        tracker.track(
            _tracked_order("ask-1", side="SELL", price="0.55")
        )
        client = _FakeClobClient()
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        asyncio.run(manager.cancel_opposing_on_fill("tok-1", "SELL"))

        assert len(client.cancel_order_calls) == 1
        assert "bid-1" in client.cancel_order_calls
        assert tracker.get("bid-1").state == OrderState.CANCELED
        assert tracker.get("ask-1").state == OrderState.LIVE

    def test_no_opposing_orders_is_noop(self) -> None:
        """If there are no opposing orders, nothing should happen."""
        tracker = OrderTracker()
        tracker.track(
            _tracked_order("bid-1", side="BUY", price="0.44")
        )
        client = _FakeClobClient()
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        asyncio.run(manager.cancel_opposing_on_fill("tok-1", "SELL"))

        assert len(client.cancel_order_calls) == 1  # bid-1 is opposing to SELL

    def test_cancel_failure_does_not_crash(self) -> None:
        """If cancel_order raises, the method should log a warning but not crash."""
        tracker = OrderTracker()
        tracker.track(
            _tracked_order("ask-1", side="SELL", price="0.55")
        )
        tracker.track(
            _tracked_order("ask-2", side="SELL", price="0.56")
        )
        client = _FakeClobClient()

        # Make cancel_order raise on the first call but succeed on the second
        call_count = 0
        original_cancel = client.cancel_order

        async def flaky_cancel(order_id: str) -> dict[str, bool]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("network blip")
            return await original_cancel(order_id)

        client.cancel_order = flaky_cancel
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        # Should not raise
        asyncio.run(manager.cancel_opposing_on_fill("tok-1", "BUY"))

        # One succeeded, one failed — at least the second cancel went through
        assert call_count == 2

    def test_different_token_not_affected(self) -> None:
        """Orders on a different token should not be canceled."""
        tracker = OrderTracker()
        tracker.track(
            _tracked_order("ask-tok1", side="SELL", price="0.55", token_id="tok-1")
        )
        tracker.track(
            _tracked_order("ask-tok2", side="SELL", price="0.55", token_id="tok-2")
        )
        client = _FakeClobClient()
        manager = OrderManager(
            client=client, order_tracker=tracker, use_gtd=False
        )

        asyncio.run(manager.cancel_opposing_on_fill("tok-1", "BUY"))

        # Only tok-1's ask should be canceled
        assert len(client.cancel_order_calls) == 1
        assert "ask-tok1" in client.cancel_order_calls
        assert tracker.get("ask-tok1").state == OrderState.CANCELED
        assert tracker.get("ask-tok2").state == OrderState.LIVE

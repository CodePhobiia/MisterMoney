"""Fill simulator — simulates order fills during backtesting.

Uses queue-aware fill model to determine if our orders would have been filled
given the recorded market data.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from pydantic import BaseModel, Field

from pmm1.backtest.queue_model import QueuePositionModel
from pmm1.state.books import OrderBook

logger = structlog.get_logger(__name__)


class SimulatedOrder(BaseModel):
    """An order in the simulation."""

    order_id: str
    token_id: str
    side: str  # BUY or SELL
    price: float
    size: float
    remaining_size: float = 0.0
    placed_at: float = Field(default_factory=time.time)
    filled_at: float | None = None
    queue_position: float = 0.0  # Estimated queue position
    is_filled: bool = False
    is_canceled: bool = False
    fill_price: float = 0.0
    fill_size: float = 0.0
    fills: list[dict[str, float]] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        if self.remaining_size == 0:
            self.remaining_size = self.size


class SimulatedFill(BaseModel):
    """A simulated fill event."""

    order_id: str
    token_id: str
    side: str
    price: float
    size: float
    timestamp: float = Field(default_factory=time.time)
    queue_position_at_fill: float = 0.0


class FillSimulator:
    """Simulates fill execution using recorded book data and queue model.

    Determines whether our resting orders would have been filled based on:
    1. Trade flow (did trades cross our price?)
    2. Queue position (were we near the front?)
    3. Trade size (was the incoming order large enough to reach us?)
    """

    def __init__(
        self,
        queue_model: QueuePositionModel | None = None,
        partial_fill_enabled: bool = True,
    ) -> None:
        self._queue_model = queue_model or QueuePositionModel()
        self._partial_fill_enabled = partial_fill_enabled
        self._orders: dict[str, SimulatedOrder] = {}
        self._fills: list[SimulatedFill] = []
        self._total_fills = 0

    def place_order(
        self,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        book: OrderBook | None = None,
    ) -> SimulatedOrder:
        """Place a simulated order.

        Estimates initial queue position based on current book state.
        """
        # Estimate queue position
        queue_pos = 0.0
        if book is not None:
            queue_pos = self._queue_model.estimate_queue_position(
                price, side, book
            )

        order = SimulatedOrder(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            queue_position=queue_pos,
        )
        self._orders[order_id] = order
        return order

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a simulated order."""
        order = self._orders.get(order_id)
        if order and not order.is_filled:
            order.is_canceled = True
            return True
        return False

    def process_trade(
        self,
        token_id: str,
        trade_price: float,
        trade_size: float,
        trade_side: str,
        timestamp: float = 0.0,
    ) -> list[SimulatedFill]:
        """Process a market trade and check if it fills any of our orders.

        A trade fills our order if:
        1. The trade crosses our price
        2. The trade is on the opposite side (their buy fills our sell, etc.)
        3. The trade size reaches our queue position

        Args:
            token_id: Token the trade happened in.
            trade_price: Trade execution price.
            trade_size: Trade size.
            trade_side: Side of the aggressor (BUY or SELL).
            timestamp: Trade timestamp.

        Returns:
            List of fills generated.
        """
        fills: list[SimulatedFill] = []
        remaining_trade_size = trade_size

        # Get our orders on the opposite side
        # If incoming trade is a BUY, it fills our SELL orders
        # If incoming trade is a SELL, it fills our BUY orders
        target_side = "SELL" if trade_side == "BUY" else "BUY"

        # Get relevant orders sorted by price priority
        relevant_orders = [
            o for o in self._orders.values()
            if o.token_id == token_id
            and o.side == target_side
            and not o.is_filled
            and not o.is_canceled
            and o.remaining_size > 0
        ]

        if target_side == "SELL":
            # Incoming buy fills our sells — lowest ask first
            relevant_orders.sort(key=lambda o: o.price)
        else:
            # Incoming sell fills our buys — highest bid first
            relevant_orders.sort(key=lambda o: -o.price)

        for order in relevant_orders:
            if remaining_trade_size <= 0:
                break

            # Check if trade crosses our price
            crosses = False
            if order.side == "SELL" and trade_price >= order.price:
                crosses = True
            elif order.side == "BUY" and trade_price <= order.price:
                crosses = True

            if not crosses:
                continue

            # Check queue position — do we get filled?
            fill_probability = self._queue_model.fill_probability(
                distance_from_best=0.0,  # We're at the matching price
                queue_position=order.queue_position,
                trade_size=remaining_trade_size,
                order_size=order.remaining_size,
            )

            # Simple threshold for fill
            if fill_probability < 0.3:
                # Advance queue position
                order.queue_position = max(0, order.queue_position - remaining_trade_size * 0.5)
                continue

            # Determine fill size
            if self._partial_fill_enabled:
                fill_size = min(
                    order.remaining_size,
                    remaining_trade_size,
                )
            else:
                if remaining_trade_size >= order.remaining_size:
                    fill_size = order.remaining_size
                else:
                    continue  # Not enough to fill completely

            # Execute fill
            fill = SimulatedFill(
                order_id=order.order_id,
                token_id=token_id,
                side=order.side,
                price=order.price,
                size=fill_size,
                timestamp=timestamp or time.time(),
                queue_position_at_fill=order.queue_position,
            )
            fills.append(fill)
            self._fills.append(fill)
            self._total_fills += 1

            order.remaining_size -= fill_size
            order.fills.append({
                "price": order.price,
                "size": fill_size,
                "timestamp": fill.timestamp,
            })

            if order.remaining_size <= 0:
                order.is_filled = True
                order.filled_at = fill.timestamp
                order.fill_price = order.price
                order.fill_size = order.size

            remaining_trade_size -= fill_size

        return fills

    def get_active_orders(self, token_id: str | None = None) -> list[SimulatedOrder]:
        """Get active (unfilled, uncanceled) orders."""
        orders = [
            o for o in self._orders.values()
            if not o.is_filled and not o.is_canceled
        ]
        if token_id:
            orders = [o for o in orders if o.token_id == token_id]
        return orders

    def get_fills(self) -> list[SimulatedFill]:
        """Get all simulated fills."""
        return self._fills

    def get_stats(self) -> dict[str, Any]:
        total = len(self._orders)
        filled = sum(1 for o in self._orders.values() if o.is_filled)
        canceled = sum(1 for o in self._orders.values() if o.is_canceled)
        return {
            "total_orders": total,
            "filled": filled,
            "canceled": canceled,
            "active": total - filled - canceled,
            "total_fills": self._total_fills,
            "fill_rate": filled / total if total > 0 else 0.0,
        }

    def reset(self) -> None:
        """Reset simulator state."""
        self._orders.clear()
        self._fills.clear()
        self._total_fills = 0

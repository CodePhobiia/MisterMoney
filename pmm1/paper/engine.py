"""Paper trading engine — simulates order fills against live book data.

Maintains a virtual portfolio with cash, positions, and PnL tracking.
Orders are checked against live book prices each cycle:
  - A limit buy fills if book best_ask <= our bid price
  - A limit sell fills if book best_bid >= our ask price
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

from pmm1.state.books import BookManager, OrderBook

logger = structlog.get_logger(__name__)


class PaperSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class PaperOrder:
    """A simulated order."""

    order_id: str
    token_id: str
    condition_id: str
    side: PaperSide
    price: float
    size: float
    remaining: float
    strategy: str = "mm"
    created_at: float = field(default_factory=time.time)
    ttl_s: float = 30.0  # expire after this many seconds
    neg_risk: bool = False

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_s

    @property
    def is_filled(self) -> bool:
        return self.remaining <= 0


@dataclass
class PaperFill:
    """A simulated fill."""

    fill_id: str
    order_id: str
    token_id: str
    condition_id: str
    side: PaperSide
    price: float
    size: float
    strategy: str
    timestamp: float
    cash_delta: float  # change in cash from this fill
    neg_risk: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "token_id": self.token_id[:16],
            "condition_id": self.condition_id[:16],
            "side": self.side.value,
            "price": self.price,
            "size": self.size,
            "strategy": self.strategy,
            "timestamp": self.timestamp,
            "cash_delta": self.cash_delta,
            "neg_risk": self.neg_risk,
        }


@dataclass
class PnlSnapshot:
    """Point-in-time snapshot of paper portfolio state."""

    timestamp: float
    cycle: int
    cash: float
    position_value: float
    nav: float
    pnl: float  # nav - initial_nav
    pnl_pct: float
    num_positions: int
    num_open_orders: int
    total_fills: int
    total_volume: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "cycle": self.cycle,
            "cash": round(self.cash, 4),
            "position_value": round(self.position_value, 4),
            "nav": round(self.nav, 4),
            "pnl": round(self.pnl, 4),
            "pnl_pct": round(self.pnl_pct, 4),
            "num_positions": self.num_positions,
            "num_open_orders": self.num_open_orders,
            "total_fills": self.total_fills,
            "total_volume": round(self.total_volume, 4),
        }


class PaperEngine:
    """Simulates order fills against live order book data.

    Maintains virtual cash, positions, and tracks all fills and PnL.
    """

    def __init__(self, initial_nav: float) -> None:
        self.initial_nav = initial_nav
        self.cash = initial_nav
        self.positions: dict[str, float] = {}  # token_id -> quantity
        self.avg_prices: dict[str, float] = {}  # token_id -> avg entry price
        self.orders: list[PaperOrder] = []
        self.fills: list[PaperFill] = []
        self.pnl_history: list[PnlSnapshot] = []
        self.total_volume: float = 0.0
        self._cycle_count: int = 0

    def submit_order(self, order: PaperOrder) -> None:
        """Submit a paper order — will be checked against live books each cycle."""
        # Normalize numeric fields defensively (arb detectors may emit strings).
        order.price = float(order.price)
        order.size = float(order.size)
        order.remaining = float(order.remaining)

        self.orders.append(order)
        logger.debug(
            "paper_order_submitted",
            side=order.side.value,
            price=f"{order.price:.4f}",
            size=f"{order.size:.1f}",
            token=order.token_id[:16],
        )

    def submit_quote(
        self,
        token_id: str,
        condition_id: str,
        bid_price: float | None,
        bid_size: float | None,
        ask_price: float | None,
        ask_size: float | None,
        strategy: str = "mm",
        neg_risk: bool = False,
    ) -> list[PaperOrder]:
        """Submit a two-sided quote as paper orders. Returns created orders."""
        created = []

        if bid_price is not None and bid_size is not None and bid_size > 0:
            order = PaperOrder(
                order_id=str(uuid.uuid4())[:8],
                token_id=token_id,
                condition_id=condition_id,
                side=PaperSide.BUY,
                price=float(bid_price),
                size=float(bid_size),
                remaining=float(bid_size),
                strategy=strategy,
                neg_risk=neg_risk,
            )
            self.submit_order(order)
            created.append(order)

        if ask_price is not None and ask_size is not None and ask_size > 0:
            order = PaperOrder(
                order_id=str(uuid.uuid4())[:8],
                token_id=token_id,
                condition_id=condition_id,
                side=PaperSide.SELL,
                price=float(ask_price),
                size=float(ask_size),
                remaining=float(ask_size),
                strategy=strategy,
                neg_risk=neg_risk,
            )
            self.submit_order(order)
            created.append(order)

        return created

    def submit_arb_orders(
        self,
        arb_orders: list[dict[str, Any]],
    ) -> list[PaperOrder]:
        """Submit arb orders (FOK-style — fill immediately or cancel)."""
        created = []
        for o in arb_orders:
            side_raw = o["side"]
            side_val = side_raw.value if hasattr(side_raw, "value") else str(side_raw)
            order = PaperOrder(
                order_id=str(uuid.uuid4())[:8],
                token_id=o["token_id"],
                condition_id=o.get("condition_id", ""),
                side=PaperSide(side_val),
                price=float(o["price"]),
                size=float(o["size"]),
                remaining=float(o["size"]),
                strategy="arb",
                ttl_s=0.5,  # very short TTL for arb orders
                neg_risk=o.get("neg_risk", False),
            )
            self.submit_order(order)
            created.append(order)
        return created

    def check_fills(self, book_manager: BookManager) -> list[PaperFill]:
        """Check if any paper orders would have filled at current book prices.

        A limit buy fills if book best_ask <= our bid price
        A limit sell fills if book best_bid >= our ask price

        Returns list of new fills this cycle.
        """
        self._cycle_count += 1
        new_fills: list[PaperFill] = []
        still_open: list[PaperOrder] = []

        for order in self.orders:
            # Skip expired orders
            if order.is_expired or order.is_filled:
                continue

            book = book_manager.get(order.token_id)
            if book is None or book.is_stale:
                still_open.append(order)
                continue

            filled = False

            if order.side == PaperSide.BUY:
                best_ask = book.get_best_ask()
                if best_ask and best_ask.price_float <= order.price:
                    # Fill at the ask price (we're the aggressive buyer or passive gets lifted)
                    fill_price = best_ask.price_float
                    fill_size = min(order.remaining, best_ask.size_float)

                    # Check if we have enough cash
                    cost = fill_price * fill_size
                    if cost > self.cash:
                        fill_size = self.cash / fill_price
                        cost = fill_price * fill_size

                    if fill_size > 0.5:  # minimum fill size
                        fill = self._execute_fill(order, fill_price, fill_size)
                        new_fills.append(fill)
                        filled = True

            elif order.side == PaperSide.SELL:
                best_bid = book.get_best_bid()
                if best_bid and best_bid.price_float >= order.price:
                    fill_price = best_bid.price_float
                    fill_size = min(order.remaining, best_bid.size_float)

                    # For sells, check if we have enough position (or allow short selling)
                    current_pos = self.positions.get(order.token_id, 0.0)
                    if current_pos < fill_size:
                        fill_size = max(0, current_pos)

                    if fill_size > 0.5:
                        fill = self._execute_fill(order, fill_price, fill_size)
                        new_fills.append(fill)
                        filled = True

            if not filled and not order.is_expired:
                still_open.append(order)

        self.orders = still_open
        return new_fills

    def _execute_fill(
        self, order: PaperOrder, fill_price: float, fill_size: float,
    ) -> PaperFill:
        """Execute a simulated fill, updating cash and positions."""
        if order.side == PaperSide.BUY:
            cost = fill_price * fill_size
            self.cash -= cost
            cash_delta = -cost

            # Update position
            old_pos = self.positions.get(order.token_id, 0.0)
            old_avg = self.avg_prices.get(order.token_id, 0.0)
            new_pos = old_pos + fill_size

            # Update average price
            if new_pos > 0:
                self.avg_prices[order.token_id] = (
                    (old_avg * old_pos + fill_price * fill_size) / new_pos
                )
            self.positions[order.token_id] = new_pos

        else:  # SELL
            proceeds = fill_price * fill_size
            self.cash += proceeds
            cash_delta = proceeds

            old_pos = self.positions.get(order.token_id, 0.0)
            new_pos = old_pos - fill_size
            self.positions[order.token_id] = max(0.0, new_pos)

            # Clean up zero positions
            if self.positions[order.token_id] < 0.01:
                self.positions.pop(order.token_id, None)
                self.avg_prices.pop(order.token_id, None)

        order.remaining -= fill_size
        self.total_volume += fill_price * fill_size

        fill = PaperFill(
            fill_id=str(uuid.uuid4())[:8],
            order_id=order.order_id,
            token_id=order.token_id,
            condition_id=order.condition_id,
            side=order.side,
            price=fill_price,
            size=fill_size,
            strategy=order.strategy,
            timestamp=time.time(),
            cash_delta=cash_delta,
            neg_risk=order.neg_risk,
        )
        self.fills.append(fill)

        logger.info(
            "paper_fill",
            side=order.side.value,
            price=f"{fill_price:.4f}",
            size=f"{fill_size:.1f}",
            token=order.token_id[:16],
            strategy=order.strategy,
            cash=f"{self.cash:.2f}",
        )

        return fill

    def get_nav(self, book_manager: BookManager) -> float:
        """Calculate current NAV = cash + mark-to-market positions."""
        nav = self.cash

        for token_id, qty in self.positions.items():
            if qty <= 0:
                continue
            book = book_manager.get(token_id)
            if book:
                mid = book.get_midpoint()
                if mid is not None:
                    nav += qty * mid
                    continue
            # Fallback to avg price if no book
            avg = self.avg_prices.get(token_id, 0.5)
            nav += qty * avg

        return nav

    def snapshot(self, book_manager: BookManager) -> PnlSnapshot:
        """Record current state for PnL tracking."""
        nav = self.get_nav(book_manager)
        position_value = nav - self.cash

        snap = PnlSnapshot(
            timestamp=time.time(),
            cycle=self._cycle_count,
            cash=self.cash,
            position_value=position_value,
            nav=nav,
            pnl=nav - self.initial_nav,
            pnl_pct=((nav - self.initial_nav) / self.initial_nav * 100) if self.initial_nav > 0 else 0,
            num_positions=len([q for q in self.positions.values() if q > 0]),
            num_open_orders=len(self.orders),
            total_fills=len(self.fills),
            total_volume=self.total_volume,
        )
        self.pnl_history.append(snap)
        return snap

    def cancel_all_orders(self, token_id: str | None = None) -> int:
        """Cancel all open orders, optionally filtered by token_id."""
        if token_id:
            before = len(self.orders)
            self.orders = [o for o in self.orders if o.token_id != token_id]
            return before - len(self.orders)
        else:
            count = len(self.orders)
            self.orders.clear()
            return count

    def get_position_summary(self) -> dict[str, Any]:
        """Get a summary of all positions."""
        return {
            "cash": round(self.cash, 4),
            "num_positions": len(self.positions),
            "positions": {
                tid[:16]: {
                    "qty": round(qty, 2),
                    "avg_price": round(self.avg_prices.get(tid, 0), 4),
                }
                for tid, qty in self.positions.items()
                if qty > 0
            },
            "total_fills": len(self.fills),
            "total_volume": round(self.total_volume, 2),
        }

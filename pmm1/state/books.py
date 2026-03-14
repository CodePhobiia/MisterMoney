"""Order book state management — tick-size-aware local book with microprice, imbalance."""

from __future__ import annotations

import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


class PriceLevel(BaseModel):
    """A single price level on one side of the book."""

    price: Decimal
    size: Decimal

    @property
    def price_float(self) -> float:
        return float(self.price)

    @property
    def size_float(self) -> float:
        return float(self.size)


class OrderBook:
    """Local order book maintained from WebSocket snapshots + deltas.

    Keeps bid and ask sides sorted, supports tick-size-aware operations.
    """

    def __init__(self, token_id: str, tick_size: Decimal = Decimal("0.01")) -> None:
        self.token_id = token_id
        self.tick_size = tick_size
        self._bids: dict[Decimal, Decimal] = {}  # price → size (sorted desc)
        self._asks: dict[Decimal, Decimal] = {}  # price → size (sorted asc)
        self.last_update_ts: float = 0.0
        self.last_trade_price: float | None = None
        self.sequence: int = 0

    @property
    def age_seconds(self) -> float:
        """Seconds since last update."""
        if self.last_update_ts == 0:
            return float("inf")
        return time.time() - self.last_update_ts

    @property
    def is_stale(self) -> bool:
        """True if book hasn't been updated in > 2 seconds."""
        return self.age_seconds > 2.0

    def apply_snapshot(self, bids: list[dict[str, Any]], asks: list[dict[str, Any]]) -> None:
        """Replace entire book with a snapshot.

        Args:
            bids: List of {"price": "0.50", "size": "100"} dicts.
            asks: List of {"price": "0.51", "size": "100"} dicts.
        """
        self._bids.clear()
        self._asks.clear()

        for level in bids:
            price = Decimal(str(level["price"]))
            size = Decimal(str(level["size"]))
            if size > 0:
                self._bids[price] = size

        for level in asks:
            price = Decimal(str(level["price"]))
            size = Decimal(str(level["size"]))
            if size > 0:
                self._asks[price] = size

        self.last_update_ts = time.time()
        logger.debug(
            "book_snapshot_applied",
            token_id=self.token_id[:16],
            bids=len(self._bids),
            asks=len(self._asks),
        )

    def apply_delta(self, changes: list[dict[str, Any]], side: str) -> None:
        """Apply incremental price_change deltas.

        Args:
            changes: List of {"price": "0.50", "size": "100"} dicts.
                     size=0 means remove the level.
            side: "bids" or "asks"
        """
        book = self._bids if side == "bids" else self._asks

        for change in changes:
            price = Decimal(str(change["price"]))
            size = Decimal(str(change["size"]))

            if size <= 0:
                book.pop(price, None)
            else:
                book[price] = size

        self.last_update_ts = time.time()

    def get_level_size(self, side: str, price: Decimal | float | str) -> float:
        """Return the visible size at one price level."""
        price_dec = Decimal(str(price))
        normalized_side = side.lower()
        if normalized_side in {"bid", "bids"}:
            return float(self._bids.get(price_dec, Decimal("0")))
        if normalized_side in {"ask", "asks"}:
            return float(self._asks.get(price_dec, Decimal("0")))
        raise ValueError(f"unknown book side: {side}")

    def set_tick_size(self, new_tick_size: Decimal) -> None:
        """Update tick size (from tick_size_change WS event)."""
        if new_tick_size != self.tick_size:
            logger.info(
                "tick_size_changed",
                token_id=self.token_id[:16],
                old=str(self.tick_size),
                new=str(new_tick_size),
            )
            self.tick_size = new_tick_size

    # ── Accessors ──

    def get_bids(self, depth: int | None = None) -> list[PriceLevel]:
        """Get bid levels sorted by price descending."""
        sorted_prices = sorted(self._bids.keys(), reverse=True)
        if depth:
            sorted_prices = sorted_prices[:depth]
        return [PriceLevel(price=p, size=self._bids[p]) for p in sorted_prices]

    def get_asks(self, depth: int | None = None) -> list[PriceLevel]:
        """Get ask levels sorted by price ascending."""
        sorted_prices = sorted(self._asks.keys())
        if depth:
            sorted_prices = sorted_prices[:depth]
        return [PriceLevel(price=p, size=self._asks[p]) for p in sorted_prices]

    def get_best_bid(self) -> PriceLevel | None:
        """Best (highest) bid."""
        if not self._bids:
            return None
        price = max(self._bids.keys())
        return PriceLevel(price=price, size=self._bids[price])

    def get_best_ask(self) -> PriceLevel | None:
        """Best (lowest) ask."""
        if not self._asks:
            return None
        price = min(self._asks.keys())
        return PriceLevel(price=price, size=self._asks[price])

    def get_midpoint(self) -> float | None:
        """Midpoint = (best_bid + best_ask) / 2."""
        bid = self.get_best_bid()
        ask = self.get_best_ask()
        if bid is None or ask is None:
            return None
        return float((bid.price + ask.price) / 2)

    def get_microprice(self) -> float | None:
        """Microprice = (ask * bid_size + bid * ask_size) / (bid_size + ask_size).

        Size-weighted midpoint that reflects order flow pressure.
        """
        bid = self.get_best_bid()
        ask = self.get_best_ask()
        if bid is None or ask is None:
            return None
        total_size = bid.size + ask.size
        if total_size == 0:
            return self.get_midpoint()
        microprice = (ask.price * bid.size + bid.price * ask.size) / total_size
        return float(microprice)

    def get_imbalance(self) -> float:
        """Book imbalance I_t = (bid_size - ask_size) / (bid_size + ask_size).

        Returns value in [-1, 1]. Positive = more buy pressure.
        """
        bid = self.get_best_bid()
        ask = self.get_best_ask()
        if bid is None or ask is None:
            return 0.0
        total = bid.size + ask.size
        if total == 0:
            return 0.0
        return float((bid.size - ask.size) / total)

    def get_spread(self) -> float | None:
        """Spread in absolute price terms."""
        bid = self.get_best_bid()
        ask = self.get_best_ask()
        if bid is None or ask is None:
            return None
        return float(ask.price - bid.price)

    def get_spread_cents(self) -> float | None:
        """Spread in cents (¢)."""
        spread = self.get_spread()
        if spread is None:
            return None
        return spread * 100

    def get_depth_within(self, cents: float, side: str = "both") -> float:
        """Total size within N cents of midpoint.

        Args:
            cents: Depth in cents (e.g., 2 for 2¢).
            side: "bid", "ask", or "both".
        """
        mid = self.get_midpoint()
        if mid is None:
            return 0.0

        threshold = Decimal(str(cents / 100))
        mid_dec = Decimal(str(mid))
        total = Decimal("0")

        if side in ("bid", "both"):
            for price, size in self._bids.items():
                if mid_dec - price <= threshold:
                    total += size

        if side in ("ask", "both"):
            for price, size in self._asks.items():
                if price - mid_dec <= threshold:
                    total += size

        return float(total)

    def get_weighted_imbalance(self, depth: int = 5) -> float:
        """Multi-level weighted imbalance across top N levels.

        Levels closer to the top are weighted more heavily.
        """
        bids = self.get_bids(depth)
        asks = self.get_asks(depth)

        if not bids or not asks:
            return 0.0

        bid_weighted = sum(
            level.size_float * (1.0 / (i + 1)) for i, level in enumerate(bids)
        )
        ask_weighted = sum(
            level.size_float * (1.0 / (i + 1)) for i, level in enumerate(asks)
        )

        total = bid_weighted + ask_weighted
        if total == 0:
            return 0.0
        return (bid_weighted - ask_weighted) / total

    # ── Tick-Size Helpers ──

    def round_price_down(self, price: float) -> Decimal:
        """Round price down to nearest tick."""
        p = Decimal(str(price))
        return (p / self.tick_size).to_integral_value(rounding=ROUND_DOWN) * self.tick_size

    def round_price_up(self, price: float) -> Decimal:
        """Round price up to nearest tick."""
        p = Decimal(str(price))
        return (p / self.tick_size).to_integral_value(rounding=ROUND_UP) * self.tick_size

    def is_valid_price(self, price: float) -> bool:
        """Check if price is a valid tick-size increment."""
        p = Decimal(str(price))
        remainder = p % self.tick_size
        return remainder == 0

    def __repr__(self) -> str:
        bid = self.get_best_bid()
        ask = self.get_best_ask()
        bid_str = f"{bid.price_float:.4f}x{bid.size_float:.0f}" if bid else "None"
        ask_str = f"{ask.price_float:.4f}x{ask.size_float:.0f}" if ask else "None"
        return f"OrderBook({self.token_id[:16]}, {bid_str} / {ask_str}, tick={self.tick_size})"


class BookManager:
    """Manages multiple order books keyed by token_id."""

    def __init__(self) -> None:
        self._books: dict[str, OrderBook] = {}

    def get_or_create(
        self, token_id: str, tick_size: Decimal = Decimal("0.01")
    ) -> OrderBook:
        """Get existing book or create new one."""
        if token_id not in self._books:
            self._books[token_id] = OrderBook(token_id, tick_size)
        return self._books[token_id]

    def get(self, token_id: str) -> OrderBook | None:
        """Get book if it exists."""
        return self._books.get(token_id)

    def remove(self, token_id: str) -> None:
        """Remove a book."""
        self._books.pop(token_id, None)

    def get_stale_books(self, max_age_s: float = 2.0) -> list[str]:
        """Return token_ids of books that haven't been updated recently."""
        return [
            tid for tid, book in self._books.items()
            if book.age_seconds > max_age_s
        ]

    @property
    def all_token_ids(self) -> list[str]:
        return list(self._books.keys())

    def __len__(self) -> int:
        return len(self._books)


def _normalize_snapshot_level(level: Any) -> dict[str, str]:
    """Normalize a snapshot level object into apply_snapshot() format."""
    if isinstance(level, dict):
        return {
            "price": str(level["price"]),
            "size": str(level["size"]),
        }

    return {
        "price": str(getattr(level, "price")),
        "size": str(getattr(level, "size")),
    }


def build_order_book_from_snapshot(
    token_id: str,
    *,
    bids: list[Any],
    asks: list[Any],
    tick_size: Decimal = Decimal("0.01"),
) -> OrderBook:
    """Build an OrderBook from snapshot-style bid/ask levels."""
    book = OrderBook(token_id, tick_size=tick_size)
    book.apply_snapshot(
        [_normalize_snapshot_level(level) for level in bids],
        [_normalize_snapshot_level(level) for level in asks],
    )
    return book

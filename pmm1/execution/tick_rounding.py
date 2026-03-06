"""Tick-conformant price rounding — all prices MUST go through this before submission.

Tick size is dynamic and changes near extremes. Must handle:
- Standard ticks (0.01)
- Fine ticks near 0 or 1 (0.001 or 0.0001)
- tick_size_change events from WebSocket
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)

# Common Polymarket tick sizes
TICK_STANDARD = Decimal("0.01")
TICK_FINE = Decimal("0.001")
TICK_ULTRA_FINE = Decimal("0.0001")

# Valid price range
MIN_PRICE = Decimal("0.001")
MAX_PRICE = Decimal("0.999")


def round_price(
    price: float | Decimal,
    tick_size: Decimal = TICK_STANDARD,
    direction: Literal["down", "up", "nearest"] = "nearest",
) -> Decimal:
    """Round a price to the nearest valid tick.

    Args:
        price: Raw price to round.
        tick_size: Minimum tick size for this market.
        direction: "down" for bids, "up" for asks, "nearest" for either.

    Returns:
        Tick-conformant price as Decimal.
    """
    p = Decimal(str(price))

    # Clamp to valid range
    p = max(MIN_PRICE, min(MAX_PRICE, p))

    if direction == "down":
        rounded = (p / tick_size).to_integral_value(rounding=ROUND_FLOOR) * tick_size
    elif direction == "up":
        rounded = (p / tick_size).to_integral_value(rounding=ROUND_CEILING) * tick_size
    else:  # nearest
        rounded = (p / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size

    # Ensure within bounds
    rounded = max(MIN_PRICE, min(MAX_PRICE, rounded))

    return rounded


def round_bid(price: float | Decimal, tick_size: Decimal = TICK_STANDARD) -> Decimal:
    """Round bid price DOWN to nearest tick (conservative for buyer)."""
    return round_price(price, tick_size, "down")


def round_ask(price: float | Decimal, tick_size: Decimal = TICK_STANDARD) -> Decimal:
    """Round ask price UP to nearest tick (conservative for seller)."""
    return round_price(price, tick_size, "up")


def round_size(size: float | Decimal, min_size: Decimal = Decimal("0.01")) -> Decimal:
    """Round size to valid precision."""
    s = Decimal(str(size))
    return max(min_size, (s / min_size).to_integral_value(rounding=ROUND_DOWN) * min_size)


def is_valid_tick(price: float | Decimal, tick_size: Decimal = TICK_STANDARD) -> bool:
    """Check if a price is a valid tick-size increment."""
    p = Decimal(str(price))
    if p < MIN_PRICE or p > MAX_PRICE:
        return False
    remainder = p % tick_size
    return remainder == 0


def ensure_spread(
    bid: Decimal,
    ask: Decimal,
    tick_size: Decimal = TICK_STANDARD,
    min_spread_ticks: int = 1,
) -> tuple[Decimal, Decimal]:
    """Ensure bid < ask with at least min_spread_ticks between them.

    If bid >= ask, adjust both symmetrically around midpoint.
    """
    if bid >= ask:
        mid = (bid + ask) / 2
        half_spread = tick_size * min_spread_ticks
        bid = round_bid(float(mid - half_spread), tick_size)
        ask = round_ask(float(mid + half_spread), tick_size)

    # Ensure minimum spread
    while ask - bid < tick_size * min_spread_ticks:
        bid -= tick_size
        if bid < MIN_PRICE:
            bid = MIN_PRICE
            ask = bid + tick_size * min_spread_ticks
            break

    return max(MIN_PRICE, bid), min(MAX_PRICE, ask)


def price_to_string(price: Decimal) -> str:
    """Convert price to string for API submission.

    Removes trailing zeros but keeps at least 2 decimal places.
    """
    # Normalize to remove trailing zeros
    normalized = price.normalize()
    # Ensure minimum 2 decimal places
    s = f"{normalized:.10f}".rstrip("0")
    # Ensure at least 2 decimal digits
    parts = s.split(".")
    if len(parts) == 2 and len(parts[1]) < 2:
        s = f"{parts[0]}.{parts[1]:0<2}"
    return s


def compute_gtd_expiration(effective_lifetime_s: int, server_time: int | None = None) -> int:
    """Compute GTD expiration timestamp per spec §2.

    GTD security threshold: expiration = now + 60 + N for effective lifetime of N seconds.

    Args:
        effective_lifetime_s: Desired effective lifetime in seconds.
        server_time: Server time in epoch seconds (uses local time if None).

    Returns:
        Expiration unix timestamp.
    """
    import time
    now = server_time or int(time.time())
    return now + 60 + effective_lifetime_s

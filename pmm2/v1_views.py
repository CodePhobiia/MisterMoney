"""Normalized V1 runtime views for PMM-2 integration.

PMM-2 should not consume raw PMM-1 tracker objects directly. These helpers
adapt live runtime state into stable numeric views.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LiveOrderView:
    """Stable numeric view of a live PMM-1 order for PMM-2."""

    order_id: str
    token_id: str
    condition_id: str
    side: str
    price: float
    size_open: float
    strategy: str = ""
    is_scoring: bool = False
    origin: str = ""


def adapt_live_order(order: Any) -> LiveOrderView:
    """Convert a tracked/live PMM-1 order into a numeric PMM-2 view."""

    price = getattr(order, "price_float", None)
    if price is None:
        raw_price = getattr(order, "price", 0.0) or 0.0
        price = float(raw_price)

    size_open = getattr(order, "size_open", None)
    if size_open is None:
        remaining = getattr(order, "remaining_size_float", None)
        if remaining is None:
            raw_remaining = getattr(order, "remaining_size", 0.0) or 0.0
            remaining = float(raw_remaining)
        size_open = remaining

    return LiveOrderView(
        order_id=str(getattr(order, "order_id", "") or ""),
        token_id=str(getattr(order, "token_id", "") or ""),
        condition_id=str(getattr(order, "condition_id", "") or ""),
        side=str(getattr(order, "side", "") or "").upper(),
        price=float(price),
        size_open=float(size_open),
        strategy=str(getattr(order, "strategy", "") or ""),
        is_scoring=bool(getattr(order, "is_scoring", False)),
        origin=str(getattr(order, "origin", "") or ""),
    )


def read_bot_state_nav(bot_state: Any) -> float:
    """Read canonical NAV from bot state without synthetic defaults."""

    for attr in ("nav", "wallet_balance", "total_equity"):
        if hasattr(bot_state, attr):
            value = getattr(bot_state, attr)
            if value is None:
                continue
            return float(value)

    inventory_manager = getattr(bot_state, "inventory_manager", None)
    if inventory_manager and hasattr(inventory_manager, "get_total_nav_estimate"):
        return float(inventory_manager.get_total_nav_estimate())

    return 0.0


def inventory_skew_usdc(bot_state: Any, condition_id: str, mark_price: float) -> float:
    """Estimate absolute market inventory skew in USDC from PMM-1 state."""

    if mark_price <= 0.0:
        return 0.0

    position_tracker = getattr(bot_state, "position_tracker", None)
    if position_tracker is None or not hasattr(position_tracker, "get"):
        return 0.0

    position = position_tracker.get(condition_id)
    if position is None:
        return 0.0

    net_exposure = float(getattr(position, "net_exposure", 0.0) or 0.0)
    return abs(net_exposure) * mark_price

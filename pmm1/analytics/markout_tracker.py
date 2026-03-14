"""Post-fill markout tracking for adverse selection measurement.

MM-05: Measures how much the price moved against us after each fill.
Provides per-market AS cost estimates that feed back into the spread
optimizer and Kelly sizing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class PendingMarkout:
    """A fill awaiting markout measurement."""

    token_id: str
    condition_id: str
    fill_price: float
    fill_side: str  # BUY or SELL
    fv_at_fill: float
    fill_time: float = field(default_factory=time.time)
    markout_5s: float | None = None
    markout_30s: float | None = None
    markout_5m: float | None = None


class MarkoutTracker:
    """Tracks post-fill markouts for adverse selection measurement.

    Usage:
        tracker = MarkoutTracker()
        idx = tracker.record_fill("tok", "cid", 0.55, "BUY", 0.56)
        # 5 seconds later:
        tracker.update_markout(idx, current_mid=0.54, horizon="5s")
        # Get per-market AS cost:
        as_cost = tracker.get_as_cost("cid")
    """

    def __init__(self, ewma_halflife: int = 20) -> None:
        self._pending: list[PendingMarkout] = []
        self._ewma_decay = 0.5 ** (1.0 / max(1, ewma_halflife))
        self._market_as: dict[str, float] = {}  # condition_id -> EWMA AS cost
        self._market_count: dict[str, int] = {}

    def record_fill(
        self,
        token_id: str,
        condition_id: str,
        fill_price: float,
        fill_side: str,
        fv_at_fill: float,
    ) -> int:
        """Record a fill for markout tracking. Returns index for updates."""
        pm = PendingMarkout(
            token_id=token_id,
            condition_id=condition_id,
            fill_price=fill_price,
            fill_side=fill_side,
            fv_at_fill=fv_at_fill,
        )
        self._pending.append(pm)

        # Cap pending list
        if len(self._pending) > 5000:
            self._pending = self._pending[-5000:]

        return len(self._pending) - 1

    def update_markout(self, index: int, current_mid: float, horizon: str) -> None:
        """Update a pending fill with markout at a specific horizon."""
        if index < 0 or index >= len(self._pending):
            return

        pm = self._pending[index]

        # Markout = how much mid moved in our direction
        # Positive = good (market confirmed our trade), Negative = bad (adverse selection)
        if pm.fill_side == "BUY":
            markout = current_mid - pm.fill_price
        else:
            markout = pm.fill_price - current_mid

        if horizon == "5s":
            pm.markout_5s = markout
        elif horizon == "30s":
            pm.markout_30s = markout
        elif horizon == "5m":
            pm.markout_5m = markout
            # Update EWMA AS cost on 5m markout (final)
            self._update_as_cost(pm.condition_id, markout)

    def _update_as_cost(self, condition_id: str, markout_5m: float) -> None:
        """Update per-market EWMA adverse selection cost."""
        decay = self._ewma_decay
        prev = self._market_as.get(condition_id, 0.0)
        self._market_as[condition_id] = decay * prev + (1 - decay) * markout_5m
        self._market_count[condition_id] = self._market_count.get(condition_id, 0) + 1

    def get_as_cost(self, condition_id: str) -> float:
        """Get EWMA adverse selection cost for a market.

        Returns negative value when AS is bad (market moves against us).
        Returns 0.0 if insufficient data.
        """
        if self._market_count.get(condition_id, 0) < 5:
            return 0.0
        return self._market_as.get(condition_id, 0.0)

    def get_spread_capture(self, index: int) -> float:
        """Get spread capture for a fill (mid_at_fill - fill_price for BUY)."""
        if index < 0 or index >= len(self._pending):
            return 0.0
        pm = self._pending[index]
        if pm.fill_side == "BUY":
            return pm.fv_at_fill - pm.fill_price
        return pm.fill_price - pm.fv_at_fill

    def get_status(self) -> dict[str, Any]:
        return {
            "pending_fills": len(self._pending),
            "tracked_markets": len(self._market_as),
            "markets_with_data": len(
                [c for c, n in self._market_count.items() if n >= 5]
            ),
        }

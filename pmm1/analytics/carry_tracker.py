"""Inventory carry PnL tracking — marks positions to market periodically.

Fills the gap where PnLSnapshot.inventory_carry was always 0.0.
Called every 60s from the main loop to accumulate mark-to-market
changes that represent the cost/benefit of holding positions.
"""
from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class InventoryCarryTracker:
    """Tracks mark-to-market PnL from holding positions over time.

    Each snapshot computes the MTM change since the last snapshot
    for all active positions, accumulating the carry component.
    """

    def __init__(self) -> None:
        self._previous_mtm: dict[str, float] = {}  # condition_id -> last MTM
        self._total_carry: float = 0.0
        self._market_carry: dict[str, float] = {}  # per-market carry
        self._snapshot_count: int = 0

    def snapshot(
        self,
        positions: list[Any],  # list[MarketPosition]
        get_mid: Any,  # callable(condition_id) -> float | None
    ) -> float:
        """Compute carry since last snapshot.

        Args:
            positions: Active positions from PositionTracker.get_active_positions()
            get_mid: Function returning current midpoint for a condition_id

        Returns:
            Total carry delta since last snapshot.
        """
        delta_total = 0.0
        current_mtm: dict[str, float] = {}

        for pos in positions:
            cid = pos.condition_id
            mid = get_mid(cid)
            if mid is None:
                continue

            # Compute current MTM using position's mark_to_market
            # For binary markets: MTM = yes_size * yes_price + no_size * no_price - cost_basis
            yes_price = mid
            no_price = 1.0 - mid
            mtm = pos.mark_to_market(yes_price, no_price)
            current_mtm[cid] = mtm

            # Compute delta from previous snapshot
            prev = self._previous_mtm.get(cid, mtm)  # First time: delta=0
            delta = mtm - prev
            delta_total += delta

            # Accumulate per-market
            self._market_carry[cid] = self._market_carry.get(cid, 0.0) + delta

        # Clean up positions that no longer exist
        for cid in list(self._previous_mtm.keys()):
            if cid not in current_mtm:
                del self._previous_mtm[cid]
                # Keep market_carry for attribution

        self._previous_mtm = current_mtm
        self._total_carry += delta_total
        self._snapshot_count += 1

        return delta_total

    @property
    def total_carry(self) -> float:
        """Cumulative inventory carry PnL."""
        return self._total_carry

    def get_market_carry(self, condition_id: str) -> float:
        """Get carry for a specific market."""
        return self._market_carry.get(condition_id, 0.0)

    def get_all_market_carry(self) -> dict[str, float]:
        """Get carry breakdown by market."""
        return dict(self._market_carry)

    def reset_daily(self) -> float:
        """Reset for new day, returning accumulated carry."""
        result = self._total_carry
        self._total_carry = 0.0
        self._market_carry.clear()
        self._snapshot_count = 0
        return result

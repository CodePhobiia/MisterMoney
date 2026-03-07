"""Constraint checker — validate allocation decisions against capital and slot budgets.

Constraints:
1. Total active capital <= total_capital * active_cap_frac (30%)
2. Total slots <= total_slots (48)
3. Per-market capital <= max(nav * per_market_cap_frac, per_market_cap_floor)
4. Per-event capital <= nav * per_event_cap_frac (6%)
5. Nested rule: B2 only if B1 funded, B3 only if B2 funded
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from pmm2.scorer.bundles import QuoteBundle

logger = structlog.get_logger(__name__)


class AllocationConstraints(BaseModel):
    """Capital and slot budgets."""

    total_capital: float = 100.0  # NAV available for allocation
    total_slots: int = 48  # max order slots
    per_market_cap_frac: float = 0.03  # 3% of NAV per market
    per_market_cap_floor: float = 8.0  # minimum $8 per market (scale-aware)
    per_event_cap_frac: float = 0.10  # 10% of NAV per event cluster (allows floor to work)
    active_cap_frac: float = 0.30  # max 30% of NAV actively quoted


class ConstraintChecker:
    """Validate allocation decisions against constraints."""

    def __init__(self, constraints: AllocationConstraints):
        """Initialize constraint checker.

        Args:
            constraints: allocation constraints
        """
        self.constraints = constraints
        logger.info(
            "constraint_checker_initialized",
            total_capital=constraints.total_capital,
            total_slots=constraints.total_slots,
            per_market_cap_frac=constraints.per_market_cap_frac,
            per_market_cap_floor=constraints.per_market_cap_floor,
            per_event_cap_frac=constraints.per_event_cap_frac,
            active_cap_frac=constraints.active_cap_frac,
        )

    def per_market_cap(self, nav: float) -> float:
        """Compute per-market capital limit: max(nav * frac, floor).

        Scale-aware: at $104 NAV, per_market_cap = max($3.12, $8) = $8.

        Args:
            nav: current NAV

        Returns:
            Per-market capital limit in USDC
        """
        return max(
            nav * self.constraints.per_market_cap_frac,
            self.constraints.per_market_cap_floor,
        )

    def can_add_bundle(
        self,
        bundle: QuoteBundle,
        current_capital: float,
        current_slots: int,
        market_capital: dict[str, float],  # condition_id → capital used
        event_capital: dict[str, float],  # event_id → capital used
        funded_bundles: dict[str, set[str]],  # condition_id → set of funded bundle types
        event_id: str = "",
    ) -> tuple[bool, str]:
        """Check if adding this bundle violates any constraint.

        Returns (feasible, reason).

        Checks:
        1. Total capital: current + bundle.capital <= total_capital * active_cap_frac
        2. Total slots: current_slots + bundle.slots <= total_slots
        3. Per-market: market_capital[cid] + bundle.capital <= per_market_cap(nav)
        4. Per-event: event_capital[eid] + bundle.capital <= nav * per_event_cap_frac
        5. Nested: B2 only if B1 already funded, B3 only if B2 funded

        Args:
            bundle: quote bundle to check
            current_capital: total capital currently allocated
            current_slots: total slots currently used
            market_capital: capital already allocated per market
            event_capital: capital already allocated per event
            funded_bundles: bundles already funded per market
            event_id: event ID for this bundle

        Returns:
            (feasible, reason) tuple
        """
        condition_id = bundle.market_condition_id

        # --- 1. Total capital check ---
        max_active_cap = self.constraints.total_capital * self.constraints.active_cap_frac
        new_capital = current_capital + bundle.capital_usdc
        if new_capital > max_active_cap:
            return (
                False,
                f"total_capital_exceeded: {new_capital:.2f} > {max_active_cap:.2f}",
            )

        # --- 2. Total slots check ---
        new_slots = current_slots + bundle.slots
        if new_slots > self.constraints.total_slots:
            return (
                False,
                f"total_slots_exceeded: {new_slots} > {self.constraints.total_slots}",
            )

        # --- 3. Per-market capital check ---
        per_market_limit = self.per_market_cap(self.constraints.total_capital)
        market_cap_used = market_capital.get(condition_id, 0.0)
        new_market_cap = market_cap_used + bundle.capital_usdc
        if new_market_cap > per_market_limit:
            return (
                False,
                f"per_market_cap_exceeded: {new_market_cap:.2f} > {per_market_limit:.2f}",
            )

        # --- 4. Per-event capital check ---
        if event_id:
            per_event_limit = (
                self.constraints.total_capital * self.constraints.per_event_cap_frac
            )
            event_cap_used = event_capital.get(event_id, 0.0)
            new_event_cap = event_cap_used + bundle.capital_usdc
            if new_event_cap > per_event_limit:
                return (
                    False,
                    f"per_event_cap_exceeded: {new_event_cap:.2f} > {per_event_limit:.2f}",
                )

        # --- 5. Nested bundle rule ---
        # B2 requires B1 to be funded
        # B3 requires B2 to be funded
        funded = funded_bundles.get(condition_id, set())

        if bundle.bundle_type == "B2" and "B1" not in funded:
            return (False, "nested_violation: B2 requires B1 to be funded first")

        if bundle.bundle_type == "B3" and "B2" not in funded:
            return (False, "nested_violation: B3 requires B2 to be funded first")

        # All checks passed
        return (True, "feasible")

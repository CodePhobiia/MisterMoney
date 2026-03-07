"""Greedy allocator — discrete bundle selection sorted by adjusted return.

Algorithm:
1. Filter to positive adjusted return > min threshold
2. Sort by adjusted_return descending
3. Greedily assign bundles while respecting:
   - Capital and slot budgets
   - Per-market and per-event limits
   - Nested bundle rule (B1 before B2 before B3)
4. Track skipped bundles with reasons
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from pmm2.allocator.constraints import AllocationConstraints, ConstraintChecker
from pmm2.allocator.scoring import AdjustedScore
from pmm2.scorer.bundles import QuoteBundle

logger = structlog.get_logger(__name__)


class AllocationPlan(BaseModel):
    """Output of the allocator."""

    funded_bundles: list[QuoteBundle] = []
    total_capital_used: float = 0.0
    total_slots_used: int = 0
    markets_funded: int = 0
    reward_markets_funded: int = 0
    skipped_bundles: list[tuple[str, str]] = []  # (condition_id, reason)


class GreedyAllocator:
    """Discrete greedy bundle allocator.

    Sort feasible positive bundles by adjusted return R̃.
    Greedily assign until capital or slot budget exhausted.
    Respect nested rule: can't fund B2 without B1.
    """

    def __init__(
        self, constraints: AllocationConstraints, checker: ConstraintChecker | None = None
    ):
        """Initialize greedy allocator.

        Args:
            constraints: allocation constraints
            checker: constraint checker (created if not provided)
        """
        self.constraints = constraints
        self.checker = checker or ConstraintChecker(constraints)

        logger.info(
            "greedy_allocator_initialized",
            total_capital=constraints.total_capital,
            active_cap=constraints.total_capital * constraints.active_cap_frac,
            per_market_cap=self.checker.per_market_cap(constraints.total_capital),
            per_event_cap=constraints.total_capital * constraints.per_event_cap_frac,
        )

    def allocate(
        self,
        scored_bundles: list[AdjustedScore],
        event_clusters: dict[str, str],
        min_positive_return_bps: float = 6.0,
    ) -> AllocationPlan:
        """
        Greedy allocation:
        1. Filter to positive adjusted return > min threshold
        2. Sort by adjusted_return descending
        3. For each bundle:
           a. Check nested rule (B1 must be funded before B2)
           b. Check all constraints
           c. If feasible, fund it
           d. If not, skip with reason
        4. Return AllocationPlan

        At $104 NAV:
        - active_cap = $31.20 (30%)
        - per_market = $8.00 (floor)
        - per_event = $6.24 (6%)
        - ~3-4 markets max

        Args:
            scored_bundles: list of bundles with adjusted scores
            event_clusters: map of condition_id → event_id
            min_positive_return_bps: minimum positive return in bps (default 6 bps)

        Returns:
            AllocationPlan with funded bundles and tracking info
        """
        # Convert bps to decimal
        min_return = min_positive_return_bps / 10000.0

        # --- 1. Filter to positive adjusted return ---
        positive_bundles = [
            sb for sb in scored_bundles if sb.adjusted_return >= min_return
        ]

        logger.info(
            "greedy_allocation_started",
            total_bundles=len(scored_bundles),
            positive_bundles=len(positive_bundles),
            min_return_bps=min_positive_return_bps,
        )

        if not positive_bundles:
            logger.warning("no_positive_bundles", total=len(scored_bundles))
            return AllocationPlan()

        # --- 2. Sort by adjusted return descending ---
        sorted_bundles = sorted(
            positive_bundles, key=lambda sb: sb.adjusted_return, reverse=True
        )

        # --- 3. Greedy assignment ---
        funded: list[QuoteBundle] = []
        skipped: list[tuple[str, str]] = []

        # Track state
        current_capital = 0.0
        current_slots = 0
        market_capital: dict[str, float] = {}  # condition_id → capital
        event_capital: dict[str, float] = {}  # event_id → capital
        funded_bundles: dict[str, set[str]] = {}  # condition_id → set of bundle types
        funded_markets: set[str] = set()

        for scored in sorted_bundles:
            bundle = scored.bundle
            condition_id = bundle.market_condition_id
            event_id = event_clusters.get(condition_id, "")

            # Check if we can add this bundle
            feasible, reason = self.checker.can_add_bundle(
                bundle=bundle,
                current_capital=current_capital,
                current_slots=current_slots,
                market_capital=market_capital,
                event_capital=event_capital,
                funded_bundles=funded_bundles,
                event_id=event_id,
            )

            if feasible:
                # Fund this bundle
                funded.append(bundle)
                current_capital += bundle.capital_usdc
                current_slots += bundle.slots

                # Update tracking
                market_capital[condition_id] = (
                    market_capital.get(condition_id, 0.0) + bundle.capital_usdc
                )
                if event_id:
                    event_capital[event_id] = (
                        event_capital.get(event_id, 0.0) + bundle.capital_usdc
                    )

                if condition_id not in funded_bundles:
                    funded_bundles[condition_id] = set()
                funded_bundles[condition_id].add(bundle.bundle_type)
                funded_markets.add(condition_id)

                logger.info(
                    "bundle_funded",
                    condition_id=condition_id,
                    bundle=bundle.bundle_type,
                    capital=bundle.capital_usdc,
                    adjusted_return=scored.adjusted_return,
                    total_capital=current_capital,
                    total_slots=current_slots,
                )
            else:
                # Skip with reason
                skipped.append((condition_id, reason))
                logger.debug(
                    "bundle_skipped",
                    condition_id=condition_id,
                    bundle=bundle.bundle_type,
                    reason=reason,
                )

        # --- 4. Build plan ---
        # Count reward-eligible markets
        # (We don't have market metadata here, so we approximate from bundle types)
        # Markets with B2 are likely reward-eligible
        reward_markets = sum(
            1 for cid, types in funded_bundles.items() if "B2" in types or "B1" in types
        )

        plan = AllocationPlan(
            funded_bundles=funded,
            total_capital_used=current_capital,
            total_slots_used=current_slots,
            markets_funded=len(funded_markets),
            reward_markets_funded=reward_markets,
            skipped_bundles=skipped,
        )

        logger.info(
            "greedy_allocation_complete",
            funded_bundles=len(funded),
            markets_funded=len(funded_markets),
            reward_markets=reward_markets,
            capital_used=current_capital,
            slots_used=current_slots,
            skipped=len(skipped),
        )

        return plan

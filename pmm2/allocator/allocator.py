"""Capital allocator — top-level entry point tying together all components.

Full allocation cycle:
1. Apply adjusted scoring (penalties) to all bundles
2. Run greedy allocation
3. Apply hysteresis to filter out thrashing
4. Check circuit breakers
5. Return final AllocationPlan
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from pmm2.allocator.circuit_breaker import CircuitBreaker
from pmm2.allocator.constraints import AllocationConstraints, ConstraintChecker
from pmm2.allocator.greedy import AllocationPlan, GreedyAllocator
from pmm2.allocator.hysteresis import ReallocationHysteresis
from pmm2.allocator.scoring import AdjustedScore, AdjustedScorer
from pmm2.scorer.bundles import QuoteBundle

logger = structlog.get_logger(__name__)


class CapitalAllocator:
    """Top-level allocator — ties together scoring, constraints, greedy selection, hysteresis."""

    def __init__(
        self,
        nav: float,
        constraints: AllocationConstraints | None = None,
        scorer: AdjustedScorer | None = None,
        hysteresis: ReallocationHysteresis | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        min_positive_return_bps: float = 6.0,
    ):
        """Initialize capital allocator.

        Args:
            nav: current NAV in USDC
            constraints: allocation constraints (auto-generated if None)
            scorer: adjusted scorer (default if None)
            hysteresis: reallocation hysteresis (default if None)
            circuit_breaker: circuit breaker (default if None)
        """
        self.nav = nav
        self.constraints = constraints or AllocationConstraints(total_capital=nav)
        self.scorer = scorer or AdjustedScorer()
        self.checker = ConstraintChecker(self.constraints)
        self.min_positive_return_bps = min_positive_return_bps
        self.greedy = GreedyAllocator(
            self.constraints,
            self.checker,
            min_positive_return_bps=min_positive_return_bps,
        )
        self.hysteresis = hysteresis or ReallocationHysteresis()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()

        logger.info(
            "capital_allocator_initialized",
            nav=nav,
            active_cap=self.constraints.total_capital * self.constraints.active_cap_frac,
            per_market_cap=self.checker.per_market_cap(nav),
            per_event_cap=self.constraints.total_capital * self.constraints.per_event_cap_frac,
            min_positive_return_bps=min_positive_return_bps,
        )

    def update_nav(self, nav: float) -> None:
        """Update NAV (called when wallet balance changes).

        Args:
            nav: new NAV in USDC
        """
        old_nav = self.nav
        self.nav = nav
        self.constraints.total_capital = nav

        logger.info(
            "nav_updated",
            old_nav=old_nav,
            new_nav=nav,
            active_cap=nav * self.constraints.active_cap_frac,
        )

    async def run_allocation_cycle(
        self,
        scored_bundles: list[QuoteBundle],
        current_markets: set[str],
        event_clusters: dict[str, str],
        queue_uncertainties: dict[str, float] | None = None,
        net_exposures: dict[str, float] | None = None,
        current_allocations: dict[str, float] | None = None,
        override_hysteresis: dict[str, bool] | None = None,
    ) -> AllocationPlan:
        """
        Full allocation cycle:
        1. Apply adjusted scoring (penalties) to all bundles
        2. Run greedy allocation
        3. Apply hysteresis to filter out thrashing
        4. Check circuit breakers
        5. Return final AllocationPlan

        Args:
            scored_bundles: bundles with EV scores from MarketEVScorer
            current_markets: set of condition_ids we're already quoting
            event_clusters: map of condition_id → event_id
            queue_uncertainties: map of condition_id → queue uncertainty (0-1)
            net_exposures: map of condition_id → net exposure (-1 to 1)
            current_allocations: map of condition_id → current capital allocated
            override_hysteresis: map of condition_id → override flag

        Returns:
            AllocationPlan with funded bundles
        """
        queue_uncertainties = queue_uncertainties or {}
        net_exposures = net_exposures or {}
        current_allocations = current_allocations or {}
        override_hysteresis = override_hysteresis or {}

        logger.info(
            "allocation_cycle_started",
            input_bundles=len(scored_bundles),
            current_markets=len(current_markets),
            nav=self.nav,
        )

        # --- 1. Apply adjusted scoring ---
        # Compute active_events: event_id → current capital
        active_events: dict[str, float] = {}
        for cid, cap in current_allocations.items():
            event_id = event_clusters.get(cid, "")
            if event_id:
                active_events[event_id] = active_events.get(event_id, 0.0) + cap

        adjusted_scores: list[AdjustedScore] = []
        for bundle in scored_bundles:
            condition_id = bundle.market_condition_id
            queue_uncertainty = queue_uncertainties.get(condition_id, 0.0)
            net_exposure = net_exposures.get(condition_id, 0.0)

            adjusted = self.scorer.score(
                bundle=bundle,
                current_markets=current_markets,
                event_clusters=event_clusters,
                active_events=active_events,
                queue_uncertainty=queue_uncertainty,
                net_exposure=net_exposure,
            )
            adjusted_scores.append(adjusted)

        logger.info(
            "adjusted_scoring_complete",
            adjusted_bundles=len(adjusted_scores),
        )

        # --- 2. Run greedy allocation ---
        raw_plan = self.greedy.allocate(
            scored_bundles=adjusted_scores,
            event_clusters=event_clusters,
            min_positive_return_bps=self.min_positive_return_bps,
        )

        logger.info(
            "greedy_allocation_complete",
            funded_bundles=len(raw_plan.funded_bundles),
            capital_used=raw_plan.total_capital_used,
            markets_funded=raw_plan.markets_funded,
        )

        # --- 3. Apply hysteresis ---
        # Filter bundles through hysteresis gate
        # For now, we apply simple logic: if hysteresis blocks, skip the bundle
        # In production, this would be more nuanced (partial allocations, etc.)

        filtered_bundles: list[QuoteBundle] = []
        hysteresis_skipped: list[tuple[str, str]] = []

        # First, update hysteresis state for all proposed allocations
        for bundle in raw_plan.funded_bundles:
            condition_id = bundle.market_condition_id
            target_cap = bundle.capital_usdc

            # Compute rank (position in sorted list)
            rank = next(
                (
                    i
                    for i, b in enumerate(raw_plan.funded_bundles)
                    if b.market_condition_id == condition_id
                ),
                -1,
            )

            self.hysteresis.update_cycle(
                condition_id=condition_id,
                target_cap=target_cap,
                rank=rank,
            )

        # Now check if each bundle should be allocated
        for bundle in raw_plan.funded_bundles:
            condition_id = bundle.market_condition_id
            current_cap = current_allocations.get(condition_id, 0.0)
            target_cap = bundle.capital_usdc
            rank = next(
                (
                    i
                    for i, b in enumerate(raw_plan.funded_bundles)
                    if b.market_condition_id == condition_id
                ),
                -1,
            )

            # Check for override
            override = override_hysteresis.get(condition_id, False)

            # Check circuit breaker
            if self.circuit_breaker.is_tripped(condition_id):
                logger.warning(
                    "circuit_breaker_active",
                    condition_id=condition_id,
                )
                hysteresis_skipped.append((condition_id, "circuit_breaker_tripped"))
                continue

            # Check hysteresis
            should_move, reason = self.hysteresis.should_reallocate(
                condition_id=condition_id,
                current_cap=current_cap,
                target_cap=target_cap,
                rank=rank,
                override=override,
            )

            if should_move:
                filtered_bundles.append(bundle)
                self.hysteresis.record_reallocation(condition_id, target_cap)
            else:
                hysteresis_skipped.append((condition_id, reason))
                logger.debug(
                    "hysteresis_blocked",
                    condition_id=condition_id,
                    reason=reason,
                )

        # --- 4. Build final plan ---
        # Recompute totals after hysteresis filtering
        total_capital = sum(b.capital_usdc for b in filtered_bundles)
        total_slots = sum(b.slots for b in filtered_bundles)
        funded_markets = len({b.market_condition_id for b in filtered_bundles})

        final_plan = AllocationPlan(
            funded_bundles=filtered_bundles,
            total_capital_used=total_capital,
            total_slots_used=total_slots,
            markets_funded=funded_markets,
            reward_markets_funded=raw_plan.reward_markets_funded,  # approximate
            skipped_bundles=raw_plan.skipped_bundles + hysteresis_skipped,
        )

        logger.info(
            "allocation_cycle_complete",
            funded_bundles=len(final_plan.funded_bundles),
            capital_used=final_plan.total_capital_used,
            slots_used=final_plan.total_slots_used,
            markets_funded=final_plan.markets_funded,
            hysteresis_skipped=len(hysteresis_skipped),
            total_skipped=len(final_plan.skipped_bundles),
        )

        return final_plan

    async def persist_decisions(self, db: Any, plan: AllocationPlan) -> None:
        """Write allocation decisions to allocation_decision table.

        Args:
            db: database connection
            plan: allocation plan to persist
        """
        if not plan.funded_bundles:
            logger.info("no_bundles_to_persist")
            return

        ts_iso = datetime.now(UTC).isoformat()

        rows = []
        for bundle in plan.funded_bundles:
            rows.append(
                (
                    ts_iso,
                    bundle.market_condition_id,
                    bundle.bundle_type,
                    bundle.capital_usdc,
                    bundle.slots,
                    bundle.marginal_return * 10000,  # convert to bps
                    "FUNDED",
                )
            )

        await db.execute_many(
            """
            INSERT INTO allocation_decision (
                ts, condition_id, bundle,
                capital_usdc, slots,
                marginal_return_bps, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

        logger.info("allocation_decisions_persisted", count=len(rows))

    def get_allocator_stats(self) -> dict[str, Any]:
        """Get current allocator statistics.

        Returns:
            Dict with allocator stats
        """
        return {
            "nav": self.nav,
            "active_cap_limit": self.constraints.total_capital
            * self.constraints.active_cap_frac,
            "per_market_cap": self.checker.per_market_cap(self.nav),
            "per_event_cap": self.constraints.total_capital
            * self.constraints.per_event_cap_frac,
            "total_slots": self.constraints.total_slots,
            "hysteresis_min_usdc": self.hysteresis.hysteresis_min_usdc,
            "hysteresis_frac": self.hysteresis.hysteresis_frac,
            "circuit_breaker_cooldown_sec": self.circuit_breaker.cooldown_sec,
            "circuit_breaker_toxicity_multiplier": self.circuit_breaker.toxicity_multiplier,
        }

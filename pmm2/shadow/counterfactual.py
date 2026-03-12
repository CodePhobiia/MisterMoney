"""Counterfactual comparison engine — V1 actuals vs PMM-2 what-if.

Compares every allocation cycle:
1. Market selection divergence (which markets to quote)
2. Pricing divergence (how prices differ)
3. Predicted fill improvement (better queue positioning)
4. Reward capture improvement (more reward-eligible markets)
5. Overall EV delta (expected value improvement)

Tracks launch readiness gates from spec Section 14:
- Gate 1: Positive EV delta in ≥70% of cycles
- Gate 2: Better market selection (more reward-eligible)
- Gate 3: Lower churn (fewer cancels per live minute)
- Gate 4: At least 100 cycles of data
"""

from __future__ import annotations

import statistics
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class CounterfactualEngine:
    """Compare V1 actuals vs PMM-2 counterfactuals.

    For each allocator cycle, computes:
    1. Market selection divergence
    2. Pricing divergence
    3. Predicted fill improvement
    4. Reward capture improvement
    5. Overall EV delta
    """

    def __init__(self, shadow_logger):
        """Initialize counterfactual engine.

        Args:
            shadow_logger: ShadowLogger instance for logging comparisons
        """
        self.shadow_logger = shadow_logger
        self.cycle_count: int = 0
        self.positive_ev_cycles: int = 0
        self.total_ev_delta: float = 0.0

        # Rolling metrics
        self.metrics: dict[str, list[float]] = {
            "market_overlap_pct": [],  # how many markets both agree on
            "pmm2_reward_markets": [],  # reward-eligible in PMM-2 vs V1
            "pmm2_churn_reduction": [],  # fewer cancels predicted
            "ev_delta_per_cycle": [],  # EV improvement estimate
        }

        logger.info("counterfactual_engine_initialized")

    def compare_cycle(
        self,
        v1_state: dict[str, Any],  # V1's current state snapshot
        pmm2_plan: dict[str, Any],  # PMM-2's target plan
    ) -> dict[str, Any]:
        """Compare a single allocation cycle.

        v1_state: {
            markets: set of condition_ids,
            orders: list of {token_id, side, price, size},
            scoring_count: int,
            reward_eligible_count: int,
        }

        pmm2_plan: {
            markets: set of condition_ids,
            bundles: list of QuoteBundle,
            mutations: list of OrderMutation,
            total_ev: float,
        }

        Returns comparison dict with divergence metrics.
        """
        self.cycle_count += 1

        # Extract sets of markets
        v1_markets = set(v1_state.get("markets", []))
        pmm2_markets = set(pmm2_plan.get("markets", []))

        # Calculate market overlap
        overlap = v1_markets & pmm2_markets
        union = v1_markets | pmm2_markets
        overlap_pct = len(overlap) / len(union) if union else 0.0

        # Market selection divergence
        pmm2_only = pmm2_markets - v1_markets
        v1_only = v1_markets - pmm2_markets

        # Reward market comparison (count unique markets, not bundles/orders)
        v1_reward_count = v1_state.get("reward_eligible_count", 0)
        pmm2_reward_markets = {
            b.get("market_condition_id")
            for b in pmm2_plan.get("bundles", [])
            if b.get("is_reward_eligible", False) and b.get("market_condition_id")
        }
        pmm2_reward_count = len(pmm2_reward_markets)
        reward_improvement = pmm2_reward_count - v1_reward_count

        # Churn comparison (mutations needed)
        v1_order_count = len(v1_state.get("orders", []))
        pmm2_mutations = pmm2_plan.get("mutations", [])
        pmm2_cancels = len([m for m in pmm2_mutations if m.get("action") == "cancel"])

        # Assume V1 also has some churn (estimate from order count change)
        # For now, we'll compare mutation counts
        churn_reduction = (v1_order_count - pmm2_cancels) / max(v1_order_count, 1)

        # EV delta (PMM-2 total EV vs V1's implicit EV)
        pmm2_ev = pmm2_plan.get("total_ev", 0.0)
        # V1 doesn't track EV explicitly, so we'll estimate from scoring count
        # Each scoring order might earn ~$0.01/day in rewards
        v1_scoring_count = v1_state.get("scoring_count", 0)
        v1_estimated_ev = v1_scoring_count * 0.01  # Rough estimate
        ev_delta = pmm2_ev - v1_estimated_ev

        # Track positive EV cycles
        if ev_delta > 0:
            self.positive_ev_cycles += 1

        self.total_ev_delta += ev_delta

        # Update rolling metrics
        self.metrics["market_overlap_pct"].append(overlap_pct)
        self.metrics["pmm2_reward_markets"].append(reward_improvement)
        self.metrics["pmm2_churn_reduction"].append(churn_reduction)
        self.metrics["ev_delta_per_cycle"].append(ev_delta)

        # Build comparison result
        comparison = {
            "cycle_num": self.cycle_count,
            "market_overlap_pct": overlap_pct,
            "v1_markets": list(v1_markets),
            "pmm2_markets": list(pmm2_markets),
            "pmm2_only_markets": list(pmm2_only),
            "v1_only_markets": list(v1_only),
            "v1_reward_count": v1_reward_count,
            "pmm2_reward_count": pmm2_reward_count,
            "reward_improvement": reward_improvement,
            "v1_order_count": v1_order_count,
            "pmm2_mutation_count": len(pmm2_mutations),
            "pmm2_cancel_count": pmm2_cancels,
            "churn_reduction": churn_reduction,
            "v1_estimated_ev": v1_estimated_ev,
            "pmm2_ev": pmm2_ev,
            "ev_delta": ev_delta,
        }

        # Log divergences if significant
        if overlap_pct < 0.5:
            self.shadow_logger.log_divergence(
                "market_selection",
                {
                    "overlap_pct": overlap_pct,
                    "pmm2_only": list(pmm2_only),
                    "v1_only": list(v1_only),
                },
            )

        if abs(ev_delta) > 0.01:  # More than $0.01 EV difference
            self.shadow_logger.log_divergence(
                "scoring_difference",
                {
                    "v1_ev": v1_estimated_ev,
                    "pmm2_ev": pmm2_ev,
                    "delta": ev_delta,
                },
            )

        logger.info(
            "counterfactual_cycle_compared",
            cycle=self.cycle_count,
            overlap_pct=overlap_pct,
            ev_delta=ev_delta,
            reward_improvement=reward_improvement,
        )

        return comparison

    def get_summary(self) -> dict[str, Any]:
        """Get rolling summary of shadow performance.

        Returns:
        {
            'cycles_run': 150,
            'positive_ev_pct': 72.0,  # % of cycles where PMM-2 had higher EV
            'avg_ev_delta': 0.005,
            'avg_market_overlap': 0.65,
            'avg_reward_improvement': 3.2,  # more reward markets
            'avg_churn_reduction': 0.15,  # 15% fewer cancels
            'ready_for_live': True/False,
        }
        """
        if self.cycle_count == 0:
            return {
                "cycles_run": 0,
                "positive_ev_pct": 0.0,
                "avg_ev_delta": 0.0,
                "avg_market_overlap": 0.0,
                "avg_reward_improvement": 0.0,
                "avg_churn_reduction": 0.0,
                "ready_for_live": False,
            }

        # Calculate averages from rolling metrics (last 100 cycles)
        def avg_last_n(metric_list: list[float], n: int = 100) -> float:
            recent = metric_list[-n:] if len(metric_list) > n else metric_list
            return statistics.mean(recent) if recent else 0.0

        positive_ev_pct = (self.positive_ev_cycles / self.cycle_count) * 100.0
        avg_ev_delta = self.total_ev_delta / self.cycle_count

        # Compute readiness inline to avoid circular dependency
        ready = False
        if self.cycle_count >= 100:
            gate_1 = positive_ev_pct >= 70.0
            gate_2 = avg_last_n(self.metrics["pmm2_reward_markets"]) > 0.0
            gate_3 = avg_last_n(self.metrics["pmm2_churn_reduction"]) > 0.0
            gate_4 = True  # Already checked cycle_count
            ready = gate_1 and gate_2 and gate_3 and gate_4

        summary = {
            "cycles_run": self.cycle_count,
            "positive_ev_pct": positive_ev_pct,
            "avg_ev_delta": avg_ev_delta,
            "avg_market_overlap": avg_last_n(self.metrics["market_overlap_pct"]),
            "avg_reward_improvement": avg_last_n(self.metrics["pmm2_reward_markets"]),
            "avg_churn_reduction": avg_last_n(self.metrics["pmm2_churn_reduction"]),
            "ready_for_live": ready,
        }

        return summary

    def is_ready_for_live(self) -> bool:
        """Check if PMM-2 meets the launch gates from spec Section 14.

        Gates:
        1. Positive EV delta in at least 70% of cycles
        2. Better market selection (more reward-eligible on average)
        3. Lower churn (fewer cancels per live minute)
        4. At least 100 cycles of data

        Returns:
            True if all gates passed
        """
        if self.cycle_count < 100:
            return False

        # Helper to compute average of last N values
        def avg_last_n(metric_list: list[float], n: int = 100) -> float:
            recent = metric_list[-n:] if len(metric_list) > n else metric_list
            return statistics.mean(recent) if recent else 0.0

        positive_ev_pct = (self.positive_ev_cycles / self.cycle_count) * 100.0

        # Gate 1: Positive EV in ≥70% of cycles
        gate_1 = positive_ev_pct >= 70.0

        # Gate 2: Better market selection (more reward markets)
        gate_2 = avg_last_n(self.metrics["pmm2_reward_markets"]) > 0.0

        # Gate 3: Lower churn (positive reduction)
        gate_3 = avg_last_n(self.metrics["pmm2_churn_reduction"]) > 0.0

        # Gate 4: Enough data
        gate_4 = self.cycle_count >= 100

        all_gates_passed = gate_1 and gate_2 and gate_3 and gate_4

        logger.info(
            "launch_gates_checked",
            gate_1_positive_ev=gate_1,
            gate_2_better_selection=gate_2,
            gate_3_lower_churn=gate_3,
            gate_4_enough_data=gate_4,
            ready=all_gates_passed,
        )

        return all_gates_passed

    def get_gates_status(self) -> dict[str, bool]:
        """Get detailed status of each launch gate.

        Returns:
            Dict mapping gate name to pass/fail
        """
        if self.cycle_count < 100:
            return {
                "gate_1_positive_ev": False,
                "gate_2_better_selection": False,
                "gate_3_lower_churn": False,
                "gate_4_enough_data": False,
            }

        # Helper to compute average of last N values
        def avg_last_n(metric_list: list[float], n: int = 100) -> float:
            recent = metric_list[-n:] if len(metric_list) > n else metric_list
            return statistics.mean(recent) if recent else 0.0

        positive_ev_pct = (self.positive_ev_cycles / self.cycle_count) * 100.0

        return {
            "gate_1_positive_ev": positive_ev_pct >= 70.0,
            "gate_2_better_selection": avg_last_n(self.metrics["pmm2_reward_markets"]) > 0.0,
            "gate_3_lower_churn": avg_last_n(self.metrics["pmm2_churn_reduction"]) > 0.0,
            "gate_4_enough_data": self.cycle_count >= 100,
        }

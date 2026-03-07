"""Universe scorer — composite scoring and top-K selection.

Scores markets based on:
- Volume and spread (base profitability)
- Reward eligibility (bonus)
- Fee structure (bonus)
- Risk factors (penalty)
- Extreme prices (penalty)
"""

from __future__ import annotations

import math

import structlog

from pmm2.universe.metadata import EnrichedMarket

logger = structlog.get_logger(__name__)


class UniverseScorer:
    """Score and select top markets for trading universe."""

    def __init__(self) -> None:
        """Initialize universe scorer."""
        pass

    def score_market(self, market: EnrichedMarket) -> float:
        """Compute composite score for a market.

        Scoring formula:
        - base = log(1 + volume_24h) * (1 / max(spread_cents, 0.5))
        - reward_bonus = reward_daily_rate * 10 if reward_eligible else 0
        - fee_bonus = 2.0 if fees_enabled else 0
        - risk_penalty = ambiguity_score * 5 + (1 / max(hours_to_resolution, 6)) * 2
        - extreme_penalty = 10 if mid < 0.05 or mid > 0.95 else 0
        - score = base + reward_bonus + fee_bonus - risk_penalty - extreme_penalty

        Args:
            market: EnrichedMarket to score.

        Returns:
            Composite score (higher is better).
        """
        # Base score: volume-weighted inverse spread
        volume_log = math.log1p(market.volume_24h)
        spread_safe = max(market.spread_cents, 0.5)  # Avoid division by zero
        base = volume_log * (1.0 / spread_safe)

        # Reward bonus
        reward_bonus = 0.0
        if market.reward_eligible:
            reward_bonus = market.reward_daily_rate * 10.0

        # Fee bonus (fees mean potential rebates)
        fee_bonus = 2.0 if market.fees_enabled else 0.0

        # Risk penalty
        ambiguity_penalty = market.ambiguity_score * 5.0

        # Resolution risk penalty (higher near resolution)
        hours_safe = max(market.hours_to_resolution, 6.0)
        resolution_penalty = (1.0 / hours_safe) * 2.0

        risk_penalty = ambiguity_penalty + resolution_penalty

        # Extreme price penalty (markets at $0.01 or $0.99 have no spread)
        extreme_penalty = 0.0
        if market.mid < 0.05 or market.mid > 0.95:
            extreme_penalty = 10.0

        # Compute final score
        score = base + reward_bonus + fee_bonus - risk_penalty - extreme_penalty

        return score

    def select_top(
        self,
        markets: list[EnrichedMarket],
        max_markets: int,
    ) -> list[EnrichedMarket]:
        """Score all markets and return top N.

        Args:
            markets: List of enriched markets to score.
            max_markets: Maximum number of markets to select.

        Returns:
            Top N markets sorted by score descending.
        """
        # Score all markets
        scored: list[tuple[EnrichedMarket, float]] = []
        for market in markets:
            score = self.score_market(market)
            scored.append((market, score))

        # Sort by score descending
        scored.sort(key=lambda x: x[1], reverse=True)

        # Take top N
        selected = [m for m, s in scored[:max_markets]]

        # Log summary
        reward_eligible_count = sum(1 for m in selected if m.reward_eligible)
        fee_enabled_count = sum(1 for m in selected if m.fees_enabled)

        logger.info(
            "universe_selected",
            total_candidates=len(markets),
            selected=len(selected),
            reward_eligible=reward_eligible_count,
            fee_enabled=fee_enabled_count,
            top_score=scored[0][1] if scored else 0.0,
            top_condition_id=scored[0][0].condition_id if scored else None,
        )

        return selected

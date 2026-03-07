"""Resolution cost computation — risk from market ambiguity and time to resolution.

C_res = α₁ * ambiguity + α₂ / max(hours_to_resolution, 6) + α₃ * dispute_risk + α₄ * neg_risk_placeholder
"""

from __future__ import annotations

import structlog

from pmm2.universe.metadata import EnrichedMarket

logger = structlog.get_logger(__name__)


def compute_resolution_cost(
    market: EnrichedMarket,
    alpha_1: float = 0.002,
    alpha_2: float = 0.001,
    alpha_3: float = 0.003,
    alpha_4: float = 0.002,
    dispute_risk: float = 0.0,
) -> float:
    """Compute resolution cost for a market.

    Args:
        market: enriched market metadata
        alpha_1: ambiguity coefficient (default 0.002)
        alpha_2: time coefficient (default 0.001)
        alpha_3: dispute risk coefficient (default 0.003)
        alpha_4: neg-risk placeholder coefficient (default 0.002)
        dispute_risk: manual dispute risk flag (default 0.0)

    Returns:
        Resolution cost (dimensionless, similar scale to other costs)
    """
    # Component 1: Ambiguity
    ambiguity_cost = alpha_1 * market.ambiguity_score

    # Component 2: Time to resolution (inverse)
    # Shorter time = higher risk (less time to correct mistakes)
    hours = max(market.hours_to_resolution, 6.0)  # clamp to min 6h
    time_cost = alpha_2 / hours

    # Component 3: Dispute risk (manual flag, initially 0)
    dispute_cost = alpha_3 * dispute_risk

    # Component 4: Neg-risk placeholder outcomes
    placeholder_penalty = alpha_4 if market.has_placeholder_outcomes else 0.0

    total_cost = ambiguity_cost + time_cost + dispute_cost + placeholder_penalty

    logger.debug(
        "resolution_cost_computed",
        condition_id=market.condition_id,
        ambiguity_cost=ambiguity_cost,
        time_cost=time_cost,
        dispute_cost=dispute_cost,
        placeholder_penalty=placeholder_penalty,
        total=total_cost,
    )

    return total_cost

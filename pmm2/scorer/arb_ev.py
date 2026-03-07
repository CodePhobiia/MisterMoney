"""Arbitrage EV computation — binary parity and neg-risk conversion opportunities.

Binary parity: YES + NO = 1.00 (lockable arb if sum < 1.00)
Neg-risk conversion: synthetic position creation in neg-risk markets
"""

from __future__ import annotations

import structlog

from pmm2.universe.metadata import EnrichedMarket

logger = structlog.get_logger(__name__)


def compute_arb_ev(market: EnrichedMarket) -> float:
    """Compute arbitrage EV for a market.

    This is a per-market bonus, not per-bundle.
    Most markets will have arb_ev = 0.

    Args:
        market: enriched market metadata

    Returns:
        Expected arbitrage profit in USDC
    """
    # Skip if placeholder outcomes (ambiguous)
    if market.has_placeholder_outcomes:
        logger.debug(
            "arb_ev_skip_placeholder",
            condition_id=market.condition_id,
        )
        return 0.0

    # Binary parity check
    # If YES mid + NO mid < 1.0, there's an arb
    # We need both sides to compute this, but EnrichedMarket only has one side
    # For now, we'll use mid and assume the complement is (1 - mid)
    # Binary parity: mid + (1 - mid) = 1.0 always, so no arb here
    # Real arb requires cross-market data which we don't have in this model

    # Neg-risk conversion
    # If neg-risk market, check if we can create synthetic positions
    # This also requires multi-outcome book data which we don't have
    # For now, return 0

    # TODO: Implement when we have full multi-outcome book data
    logger.debug(
        "arb_ev_computed",
        condition_id=market.condition_id,
        is_neg_risk=market.is_neg_risk,
        arb_ev=0.0,
    )

    return 0.0

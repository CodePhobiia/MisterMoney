"""Spread EV computation — expected profit from capturing bid-ask spread.

For bid: edge = reservation_price - bid_price
For ask: edge = ask_price - reservation_price

EV = P(fill) * edge * size
"""

from __future__ import annotations

import structlog

from pmm2.scorer.bundles import QuoteBundle

logger = structlog.get_logger(__name__)


def compute_spread_ev(
    bundle: QuoteBundle,
    fill_prob_bid: float,
    fill_prob_ask: float,
    reservation_price: float,
) -> float:
    """Compute spread EV for a bundle.

    Args:
        bundle: quote bundle with bid/ask prices and sizes
        fill_prob_bid: probability of bid fill over order horizon
        fill_prob_ask: probability of ask fill over order horizon
        reservation_price: our fair value / reservation price

    Returns:
        Expected spread profit in USDC
    """
    # Bid edge: how much below fair value we buy
    bid_edge = reservation_price - bundle.bid_price
    bid_ev = fill_prob_bid * bid_edge * bundle.bid_size if bid_edge > 0 else 0.0

    # Ask edge: how much above fair value we sell
    ask_edge = bundle.ask_price - reservation_price
    ask_ev = fill_prob_ask * ask_edge * bundle.ask_size if ask_edge > 0 else 0.0

    total_ev = bid_ev + ask_ev

    logger.debug(
        "spread_ev_computed",
        condition_id=bundle.market_condition_id,
        bundle=bundle.bundle_type,
        bid_edge=bid_edge,
        ask_edge=ask_edge,
        bid_ev=bid_ev,
        ask_ev=ask_ev,
        total=total_ev,
    )

    return total_ev

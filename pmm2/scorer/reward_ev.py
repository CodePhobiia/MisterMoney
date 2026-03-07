"""Liquidity reward EV computation — expected maker rewards from Polymarket.

Implements proxy scoring function:
    g(s, v) = (1 - s/v)²₊  where s = distance from mid, v = max incentive spread

Side combination with c=3.0:
    If mid in [0.10, 0.90]: Q_pair = max(min(Q_one, Q_two), max(Q_one/c, Q_two/c))
    Otherwise: Q_pair = min(Q_one, Q_two)

Expected reward:
    E_liq = pool_daily_rate * (Q_pair / (Q_pair + Q_others)) * (H_A / T_epoch) * P(scoring)
"""

from __future__ import annotations

import structlog

from pmm2.scorer.bundles import QuoteBundle
from pmm2.universe.metadata import EnrichedMarket

logger = structlog.get_logger(__name__)


def compute_reward_ev(
    market: EnrichedMarket,
    bundle: QuoteBundle,
    q_others: float = 100.0,
    p_scoring: float = 0.85,
    h_allocator: float = 1.0,
    t_epoch: float = 24.0,
) -> float:
    """Compute liquidity reward EV for a bundle.

    Args:
        market: enriched market metadata
        bundle: quote bundle
        q_others: estimated competitor scoring mass (default 100.0)
        p_scoring: probability our orders are scoring (default 0.85)
        h_allocator: allocator horizon in hours (default 1.0)
        t_epoch: epoch duration in hours (default 24.0)

    Returns:
        Expected liquidity reward in USDC
    """
    # No reward if market not eligible
    if not market.reward_eligible:
        return 0.0

    # No reward if pool rate is zero
    if market.reward_daily_rate <= 0:
        return 0.0

    # Proxy scoring function: g(s, v) = (1 - s/v)²₊
    # where s = distance from mid, v = max incentive spread
    max_spread = market.reward_max_spread / 100.0 if market.reward_max_spread > 0 else 0.05

    # Bid side
    bid_distance = abs(market.mid - bundle.bid_price)
    if bid_distance <= max_spread:
        g_bid = (1.0 - bid_distance / max_spread) ** 2
    else:
        g_bid = 0.0

    # Ask side
    ask_distance = abs(bundle.ask_price - market.mid)
    if ask_distance <= max_spread:
        g_ask = (1.0 - ask_distance / max_spread) ** 2
    else:
        g_ask = 0.0

    # Scoring mass per side: Q_i = g_i * size_i
    q_bid = g_bid * bundle.bid_size
    q_ask = g_ask * bundle.ask_size

    # Side combination with c=3.0
    c = 3.0
    if 0.10 <= market.mid <= 0.90:
        # Balanced market: max(min(Q1, Q2), max(Q1/c, Q2/c))
        q_pair = max(min(q_bid, q_ask), max(q_bid / c, q_ask / c))
    else:
        # Skewed market: min(Q1, Q2)
        q_pair = min(q_bid, q_ask)

    # Expected reward share
    # E_liq = pool_daily_rate * (Q_pair / (Q_pair + Q_others)) * (H_A / T_epoch) * P(scoring)
    if q_pair + q_others <= 0:
        reward_share = 0.0
    else:
        reward_share = q_pair / (q_pair + q_others)

    time_fraction = h_allocator / t_epoch

    expected_reward = (
        market.reward_daily_rate * reward_share * time_fraction * p_scoring
    )

    logger.debug(
        "reward_ev_computed",
        condition_id=bundle.market_condition_id,
        bundle=bundle.bundle_type,
        g_bid=g_bid,
        g_ask=g_ask,
        q_bid=q_bid,
        q_ask=q_ask,
        q_pair=q_pair,
        reward_share=reward_share,
        expected_reward=expected_reward,
    )

    return expected_reward

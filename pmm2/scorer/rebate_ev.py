"""Maker rebate EV computation — expected fee rebates from maker fills.

feeEq = size * price * fee_rate * (price * (1 - price)) ^ exp_m

Expected rebate share = our feeEq / total market feeEq (estimated from depth)
"""

from __future__ import annotations

import structlog

from pmm2.scorer.bundles import QuoteBundle
from pmm2.universe.metadata import EnrichedMarket

logger = structlog.get_logger(__name__)


def compute_rebate_ev(
    market: EnrichedMarket,
    bundle: QuoteBundle,
    expected_fills_per_hour: float = 1.0,
    total_market_depth: float = 100.0,
    exp_m: float = 1.0,
) -> float:
    """Compute maker rebate EV for a bundle.

    Args:
        market: enriched market metadata
        bundle: quote bundle
        expected_fills_per_hour: how many fills we expect per hour
        total_market_depth: estimated total market depth (for market share)
        exp_m: feeEq exponent (default 1.0)

    Returns:
        Expected rebate in USDC
    """
    # No rebate if fees not enabled
    if not market.fees_enabled:
        return 0.0

    # No rebate if fee rate is zero
    if market.fee_rate <= 0:
        return 0.0

    # Compute our feeEq for bid and ask
    # feeEq = size * price * fee_rate * (price * (1 - price)) ^ exp_m
    def fee_eq(size: float, price: float) -> float:
        variance = price * (1.0 - price)
        return size * price * market.fee_rate * (variance**exp_m)

    our_fee_eq_bid = fee_eq(bundle.bid_size, bundle.bid_price)
    our_fee_eq_ask = fee_eq(bundle.ask_size, bundle.ask_price)
    our_fee_eq_total = our_fee_eq_bid + our_fee_eq_ask

    # Estimate total market feeEq (rough proxy: total_depth * mid * fee_rate)
    # This is very rough — in practice we'd sum over all visible orders
    market_fee_eq = total_market_depth * market.mid * market.fee_rate

    # Our share of rebates
    if market_fee_eq <= 0:
        rebate_share = 0.0
    else:
        rebate_share = our_fee_eq_total / (our_fee_eq_total + market_fee_eq)

    # Expected rebate per fill = size * price * fee_rate
    # (simplified: we get back the fee we would pay)
    rebate_per_fill_bid = bundle.bid_size * bundle.bid_price * market.fee_rate
    rebate_per_fill_ask = bundle.ask_size * bundle.ask_price * market.fee_rate
    avg_rebate_per_fill = (rebate_per_fill_bid + rebate_per_fill_ask) / 2.0

    # Expected rebate = fills_per_hour * rebate_per_fill * rebate_share
    expected_rebate = expected_fills_per_hour * avg_rebate_per_fill * rebate_share

    logger.debug(
        "rebate_ev_computed",
        condition_id=bundle.market_condition_id,
        bundle=bundle.bundle_type,
        our_fee_eq=our_fee_eq_total,
        rebate_share=rebate_share,
        expected_rebate=expected_rebate,
    )

    return expected_rebate

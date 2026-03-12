"""Bundle generator — B1/B2/B3 nested quote bundles for markets.

B1: reward core — min viable two-sided at inside spread
B2: reward depth — additional depth within reward spread band
B3: edge extension — extra depth only if spread EV > 0 before rewards
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from pmm2.universe.metadata import EnrichedMarket

logger = structlog.get_logger(__name__)


class QuoteBundle(BaseModel):
    """A nested quote bundle for a market."""

    market_condition_id: str
    bundle_type: str  # "B1", "B2", "B3"

    # Capital required
    capital_usdc: float = 0.0

    # Order slots needed (2 for two-sided)
    slots: int = 2

    # Quote specs
    bid_price: float = 0.0
    bid_size: float = 0.0
    ask_price: float = 0.0
    ask_size: float = 0.0
    fill_prob_bid: float = 0.0
    fill_prob_ask: float = 0.0

    # EV components (filled by scorer)
    spread_ev: float = 0.0
    arb_ev: float = 0.0
    liq_ev: float = 0.0
    rebate_ev: float = 0.0
    tox_cost: float = 0.0
    res_cost: float = 0.0
    carry_cost: float = 0.0

    # Aggregate
    total_value: float = 0.0
    marginal_return: float = 0.0  # V / Cap

    # Market metadata needed downstream for shadow comparison / launch gates
    is_reward_eligible: bool = False

    model_config = {"frozen": False}


def generate_bundles(
    market: EnrichedMarket,
    nav: float,
    min_order_size: float = 5.0,
    per_market_cap_usdc: float | None = None,
) -> list[QuoteBundle]:
    """Generate B1/B2/B3 nested bundles for a market.

    B1: reward core — min viable two-sided at inside spread
    B2: reward depth — additional depth within reward spread band
    B3: edge extension — extra depth only if spread EV > 0 before rewards

    Scale-aware: at $104 NAV, most markets only get B1.
    Per-market cap = max(nav * 0.03, $8)

    Args:
        market: enriched market metadata
        nav: current bot NAV in USDC
        min_order_size: minimum order size in USDC

    Returns:
        List of bundles (B1 always, B2/B3 conditionally)
    """
    bundles: list[QuoteBundle] = []

    # Skip if market not accepting orders or inactive
    if not market.accepting_orders or not market.active:
        return bundles

    # Skip if no valid book
    if market.best_bid <= 0 or market.best_ask <= 0 or market.best_ask <= market.best_bid:
        return bundles

    # Per-market capital limit: use allocator truth when available so the scorer
    # does not rate bundles the allocator can never legally fund.
    per_market_cap = per_market_cap_usdc
    if per_market_cap is None:
        per_market_cap = max(nav * 0.08, 2.5 * min_order_size)

    # Tick size (convert string to float)
    try:
        tick = float(market.tick_size)
    except (ValueError, TypeError):
        tick = 0.01

    # --- B1: Reward Core ---
    # Two-sided at inside spread
    # Size: max of min_order_size or reward_min_size
    b1_size_usdc = max(min_order_size, market.reward_min_size)

    # For bid: size in shares = size_usdc / bid_price
    # For ask: size in shares = size_usdc / ask_price
    # But we want balanced two-sided exposure
    # Conservative: size at bid price (more shares on bid side is okay)
    b1_bid_size = b1_size_usdc / market.best_bid if market.best_bid > 0 else 0.0
    b1_ask_size = b1_size_usdc / market.best_ask if market.best_ask > 0 else 0.0

    # Capital required: bid_size * bid_price + ask_size * ask_price
    b1_capital = b1_bid_size * market.best_bid + b1_ask_size * market.best_ask

    if b1_capital > 0 and b1_capital <= per_market_cap:
        b1 = QuoteBundle(
            market_condition_id=market.condition_id,
            bundle_type="B1",
            capital_usdc=b1_capital,
            slots=2,
            bid_price=market.best_bid,
            bid_size=b1_bid_size,
            ask_price=market.best_ask,
            ask_size=b1_ask_size,
            is_reward_eligible=market.reward_eligible,
        )
        bundles.append(b1)
        logger.debug(
            "bundle_b1_generated",
            condition_id=market.condition_id,
            capital=b1_capital,
            bid_size=b1_bid_size,
            ask_size=b1_ask_size,
        )
    else:
        # Can't even fit B1, skip this market
        logger.debug(
            "bundle_b1_too_large",
            condition_id=market.condition_id,
            capital=b1_capital,
            limit=per_market_cap,
        )
        return bundles

    # --- B2: Reward Depth ---
    # Only generate if:
    # 1. Market is reward-eligible
    # 2. We have room after B1
    remaining_cap = per_market_cap - b1_capital
    if market.reward_eligible and remaining_cap >= b1_capital:
        # B2: one tick deeper on each side
        b2_bid_price = max(market.best_bid - tick, tick)  # clamp to min tick
        b2_ask_price = min(market.best_ask + tick, 1.0 - tick)  # clamp to max 0.99

        # Check if still within reward spread band
        # reward_max_spread is the max spread for rewards (in dollars/cents)
        # We interpret it as the max distance from mid
        mid_to_bid = market.mid - b2_bid_price
        mid_to_ask = b2_ask_price - market.mid
        max_distance = market.reward_max_spread / 100.0 if market.reward_max_spread > 0 else 0.05

        if mid_to_bid <= max_distance and mid_to_ask <= max_distance:
            # Same sizing as B1
            b2_bid_size = b1_size_usdc / b2_bid_price if b2_bid_price > 0 else 0.0
            b2_ask_size = b1_size_usdc / b2_ask_price if b2_ask_price > 0 else 0.0
            b2_capital = b2_bid_size * b2_bid_price + b2_ask_size * b2_ask_price

            if b2_capital <= remaining_cap:
                b2 = QuoteBundle(
                    market_condition_id=market.condition_id,
                    bundle_type="B2",
                    capital_usdc=b2_capital,
                    slots=2,
                    bid_price=b2_bid_price,
                    bid_size=b2_bid_size,
                    ask_price=b2_ask_price,
                    ask_size=b2_ask_size,
                    is_reward_eligible=market.reward_eligible,
                )
                bundles.append(b2)
                logger.debug(
                    "bundle_b2_generated",
                    condition_id=market.condition_id,
                    capital=b2_capital,
                )
                remaining_cap -= b2_capital
            else:
                logger.debug(
                    "bundle_b2_insufficient_cap",
                    condition_id=market.condition_id,
                    needed=b2_capital,
                    available=remaining_cap,
                )
        else:
            logger.debug(
                "bundle_b2_outside_reward_band",
                condition_id=market.condition_id,
                mid_to_bid=mid_to_bid,
                mid_to_ask=mid_to_ask,
                max_distance=max_distance,
            )

    # --- B3: Edge Extension ---
    # Only generate if:
    # 1. We have room after B1 (and B2 if it exists)
    # 2. Spread is wide enough to capture edge (spread_cents >= 2.0)
    # 3. This is evaluated later after spread_ev is computed
    #    For now, we'll generate B3 conditionally based on spread
    if remaining_cap >= b1_capital and market.spread_cents >= 2.0:
        # B3: even wider quotes (2 ticks deeper)
        b3_bid_price = max(market.best_bid - 2 * tick, tick)
        b3_ask_price = min(market.best_ask + 2 * tick, 1.0 - tick)

        # Same sizing
        b3_bid_size = b1_size_usdc / b3_bid_price if b3_bid_price > 0 else 0.0
        b3_ask_size = b1_size_usdc / b3_ask_price if b3_ask_price > 0 else 0.0
        b3_capital = b3_bid_size * b3_bid_price + b3_ask_size * b3_ask_price

        if b3_capital <= remaining_cap:
            b3 = QuoteBundle(
                market_condition_id=market.condition_id,
                bundle_type="B3",
                capital_usdc=b3_capital,
                slots=2,
                bid_price=b3_bid_price,
                bid_size=b3_bid_size,
                ask_price=b3_ask_price,
                ask_size=b3_ask_size,
                is_reward_eligible=market.reward_eligible,
            )
            bundles.append(b3)
            logger.debug(
                "bundle_b3_generated",
                condition_id=market.condition_id,
                capital=b3_capital,
            )
        else:
            logger.debug(
                "bundle_b3_insufficient_cap",
                condition_id=market.condition_id,
                needed=b3_capital,
                available=remaining_cap,
            )

    return bundles

#!/usr/bin/env python3
"""Quick test of Sprint 4 scorer implementation."""

import asyncio

from pmm1.storage.database import Database
from pmm2.queue.estimator import QueueEstimator
from pmm2.queue.hazard import FillHazard
from pmm2.scorer.bundles import generate_bundles
from pmm2.scorer.combined import MarketEVScorer
from pmm2.universe.metadata import EnrichedMarket


async def main():
    """Test scorer with a mock market."""
    # Create mock market (reward-eligible, wide spread)
    market = EnrichedMarket(
        condition_id="0xtest123",
        question="Will Bitcoin reach $100k by end of 2026?",
        token_id_yes="token_yes_123",
        token_id_no="token_no_123",
        best_bid=0.45,
        best_ask=0.55,
        mid=0.50,
        spread_cents=10.0,
        volume_24h=5000.0,
        liquidity=1000.0,
        reward_eligible=True,
        reward_daily_rate=50.0,
        reward_min_size=3.0,  # Small enough to fit in $8 cap
        reward_max_spread=15.0,
        fees_enabled=True,
        fee_rate=0.02,
        hours_to_resolution=720.0,  # 30 days
        is_neg_risk=False,
        has_placeholder_outcomes=False,
        ambiguity_score=0.1,
        tick_size="0.01",
        accepting_orders=True,
        active=True,
    )

    nav = 104.0
    print(f"Testing scorer with NAV: ${nav:.2f}")
    print(f"Market: {market.question}")
    print(f"Spread: {market.spread_cents:.1f} cents")
    print(f"Reward eligible: {market.reward_eligible}")
    print(f"Reward pool: ${market.reward_daily_rate:.2f}/day")
    print()

    # Generate bundles (use smaller min_order_size to fit in $8 cap)
    print("Generating bundles...")
    bundles = generate_bundles(market, nav=nav, min_order_size=3.0)
    print(f"Generated {len(bundles)} bundles:")
    for b in bundles:
        print(
            f"  {b.bundle_type}: bid={b.bid_price:.3f}"
            f"@{b.bid_size:.1f}, ask={b.ask_price:.3f}"
            f"@{b.ask_size:.1f}, cap=${b.capital_usdc:.2f}"
        )
    print()

    # Initialize scorer components
    db = Database("data/pmm1.db")
    await db.init()

    hazard = FillHazard()
    estimator = QueueEstimator()

    scorer = MarketEVScorer(db, hazard, estimator)

    # Score the market
    print("Scoring bundles...")
    scored = await scorer.score_market(market, nav=nav, reservation_price=0.50, min_order_size=3.0)

    print(f"\nScored {len(scored)} bundles (sorted by marginal return):")
    for b in scored:
        print(f"\n{b.bundle_type}:")
        print(f"  Capital: ${b.capital_usdc:.2f}")
        print(f"  Spread EV: ${b.spread_ev:.4f}")
        print(f"  Liq EV: ${b.liq_ev:.4f}")
        print(f"  Rebate EV: ${b.rebate_ev:.4f}")
        print(f"  Arb EV: ${b.arb_ev:.4f}")
        print(f"  Tox cost: ${b.tox_cost:.4f}")
        print(f"  Res cost: ${b.res_cost:.4f}")
        print(f"  Carry cost: ${b.carry_cost:.4f}")
        print(f"  Total value: ${b.total_value:.4f}")
        print(f"  Marginal return: {b.marginal_return * 100:.2f}%")

    if scored:
        best = scored[0]
        print(f"\n✅ Best bundle: {best.bundle_type} with {best.marginal_return * 100:.2f}% return")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Demo script for Sprint 6 allocator — shows usage patterns.

Run: python3 -m pmm2.allocator.demo
"""

import asyncio

from pmm2.allocator import CapitalAllocator
from pmm2.scorer.bundles import QuoteBundle


async def demo_allocator() -> None:
    """Demo the capital allocator with synthetic data."""
    print("=" * 60)
    print("Sprint 6: Discrete Capital Allocator Demo")
    print("=" * 60)

    # Setup: $104 NAV bot
    nav = 104.0
    print(f"\n📊 Bot NAV: ${nav:.2f}")

    # Create allocator with default constraints
    allocator = CapitalAllocator(nav=nav)

    # Show scale-aware defaults
    stats = allocator.get_allocator_stats()
    print("\n🎯 Scale-aware constraints:")
    print(f"  - Active cap limit: ${stats['active_cap_limit']:.2f} (30% of NAV)")
    print(f"  - Per-market cap: ${stats['per_market_cap']:.2f}")
    print(f"  - Per-event cap: ${stats['per_event_cap']:.2f} (6% of NAV)")
    print(f"  - Total slots: {stats['total_slots']}")
    print(f"  - Hysteresis min: ${stats['hysteresis_min_usdc']:.2f} (scale-aware)")

    # Create synthetic bundles
    print("\n📦 Creating synthetic bundles for 5 markets...")

    bundles = [
        # Market 1: High EV, reward-eligible
        QuoteBundle(
            market_condition_id="market_1",
            bundle_type="B1",
            capital_usdc=8.0,
            slots=2,
            bid_price=0.48,
            bid_size=16.7,
            ask_price=0.52,
            ask_size=15.4,
            spread_ev=0.0050,
            liq_ev=0.0030,
            total_value=0.0080,
            marginal_return=0.0200,  # 200 bps (2%, realistic with rewards)
        ),
        # Market 1: B2 (conditional on B1)
        QuoteBundle(
            market_condition_id="market_1",
            bundle_type="B2",
            capital_usdc=8.0,
            slots=2,
            bid_price=0.47,
            bid_size=17.0,
            ask_price=0.53,
            ask_size=15.1,
            spread_ev=0.0040,
            liq_ev=0.0020,
            total_value=0.0060,
            marginal_return=0.0060 / 8.0,  # 75 bps
        ),
        # Market 2: Good EV
        QuoteBundle(
            market_condition_id="market_2",
            bundle_type="B1",
            capital_usdc=8.0,
            slots=2,
            bid_price=0.30,
            bid_size=26.7,
            ask_price=0.34,
            ask_size=23.5,
            spread_ev=0.0045,
            liq_ev=0.0015,
            total_value=0.0060,
            marginal_return=0.0060 / 8.0,  # 75 bps
        ),
        # Market 3: Moderate EV
        QuoteBundle(
            market_condition_id="market_3",
            bundle_type="B1",
            capital_usdc=8.0,
            slots=2,
            bid_price=0.60,
            bid_size=13.3,
            ask_price=0.64,
            ask_size=12.5,
            spread_ev=0.0035,
            liq_ev=0.0010,
            total_value=0.0045,
            marginal_return=0.0090,  # 90 bps (0.9%)
        ),
        # Market 4: Low EV (should be skipped if budget tight)
        QuoteBundle(
            market_condition_id="market_4",
            bundle_type="B1",
            capital_usdc=8.0,
            slots=2,
            bid_price=0.75,
            bid_size=10.7,
            ask_price=0.78,
            ask_size=10.3,
            spread_ev=0.0020,
            liq_ev=0.0005,
            total_value=0.0025,
            marginal_return=0.0050,  # 50 bps (0.5%)
        ),
    ]

    # Event clusters (markets 1 and 2 in same event)
    event_clusters = {
        "market_1": "event_A",
        "market_2": "event_A",
        "market_3": "event_B",
        "market_4": "event_C",
    }

    print("  - 5 bundles created")
    print(f"  - Total potential capital: ${sum(b.capital_usdc for b in bundles):.2f}")
    print("  - Event clusters: 2 markets in event_A, others solo")

    # Run allocation cycle (fresh start, no existing positions)
    print("\n🚀 Running allocation cycle (no existing positions)...")

    plan = await allocator.run_allocation_cycle(
        scored_bundles=bundles,
        current_markets=set(),  # No existing positions
        event_clusters=event_clusters,
        current_allocations={},  # No current capital allocated
    )

    # Show results
    print("\n✅ Allocation complete!")
    print(f"  - Funded bundles: {len(plan.funded_bundles)}")
    print(f"  - Markets funded: {plan.markets_funded}")
    print(f"  - Capital used: ${plan.total_capital_used:.2f}")
    print(f"  - Slots used: {plan.total_slots_used} / {stats['total_slots']}")
    cap_util = plan.total_capital_used / stats['active_cap_limit'] * 100
    print(f"  - Capital utilization: {cap_util:.1f}%")

    print("\n📋 Funded bundles:")
    for i, bundle in enumerate(plan.funded_bundles, 1):
        print(
            f"  {i}. {bundle.market_condition_id} {bundle.bundle_type}: "
            f"${bundle.capital_usdc:.2f} @ {bundle.marginal_return * 10000:.1f} bps"
        )

    if plan.skipped_bundles:
        print(f"\n⏭️  Skipped bundles: {len(plan.skipped_bundles)}")
        for cid, reason in plan.skipped_bundles[:5]:  # Show first 5
            print(f"  - {cid}: {reason}")

    print("\n" + "=" * 60)
    print("✓ Demo complete")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(demo_allocator())

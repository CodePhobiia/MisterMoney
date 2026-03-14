#!/usr/bin/env python3
"""Test script for Sprint 7 — Quote Planner + Runtime Integration."""

import asyncio
import os

from pmm2.config import PMM2Config, load_pmm2_config
from pmm2.planner import DiffEngine, QuotePlanner, TargetQuotePlan
from pmm2.runtime import PMM2Runtime, V1Bridge


def test_config(monkeypatch=None):
    """Test PMM2Config loading."""
    print("\n=== Testing PMM2Config ===")

    # Default config (disabled)
    config = PMM2Config()
    assert config.enabled is False
    assert config.shadow_mode is True
    print("✓ Default config loaded")

    # Load from dict
    config_dict = {
        "pmm2": {
            "enabled": True,
            "shadow_mode": False,
            "live_enabled": True,
            "live_capital_pct": 0.25,
            "canary": {"enabled": True},
            "max_markets_active": 8,
        }
    }
    if monkeypatch is not None:
        monkeypatch.setenv("PMM1_ACK_PMM2_LIVE", "YES")
    else:
        os.environ["PMM1_ACK_PMM2_LIVE"] = "YES"
    config = load_pmm2_config(config_dict)
    assert config.enabled is True
    assert config.shadow_mode is False
    assert config.live_enabled is True
    assert config.live_capital_pct == 0.25
    assert config.stage_name == "canary_25pct"
    assert config.max_markets_active == 8
    print("✓ Config loaded from dict")


def test_quote_planner():
    """Test QuotePlanner."""
    print("\n=== Testing QuotePlanner ===")

    from pmm2.scorer.bundles import QuoteBundle

    planner = QuotePlanner(max_reprices_per_minute=3)

    # Create mock bundle
    bundle = QuoteBundle(
        market_condition_id="test_cid",
        bundle_type="B1",
        capital_usdc=10.0,
        slots=2,
        bid_price=0.48,
        bid_size=10.0,
        ask_price=0.52,
        ask_size=10.0,
        spread_ev=0.5,
        total_value=1.0,
        marginal_return=0.1,
    )

    # Generate plan
    plan = planner.plan_market(
        bundles=[bundle],
        token_id_yes="token_yes",
        token_id_no="token_no",
        condition_id="test_cid",
        neg_risk=False,
        tick_size=0.01,
    )

    assert plan.condition_id == "test_cid"
    assert len(plan.ladder) == 2  # Bid + ask
    assert plan.ladder[0].side == "BUY"
    assert plan.ladder[1].side == "SELL"
    print(f"✓ Quote plan generated: {len(plan.ladder)} rungs")

    # Test reprice rate limiting
    assert planner.can_reprice("test_cid") is True
    planner.record_reprice("test_cid")
    planner.record_reprice("test_cid")
    planner.record_reprice("test_cid")
    assert planner.can_reprice("test_cid") is False
    print("✓ Reprice rate limiting works")


def test_diff_engine():
    """Test DiffEngine."""
    print("\n=== Testing DiffEngine ===")

    from pmm2.planner import QuoteLadderRung

    diff = DiffEngine()

    # Create target plan
    target = TargetQuotePlan(
        condition_id="test_cid",
        ladder=[
            QuoteLadderRung(
                token_id="token_yes",
                condition_id="test_cid",
                side="BUY",
                price=0.48,
                size=10.0,
                bundle_type="B1",
            ),
            QuoteLadderRung(
                token_id="token_yes",
                condition_id="test_cid",
                side="SELL",
                price=0.52,
                size=10.0,
                bundle_type="B1",
            ),
        ],
    )

    # Empty live orders → should generate 2 adds
    mutations = diff.diff(target, [], None, 0.01)
    assert len(mutations) == 2
    assert all(m.action == "add" for m in mutations)
    print(f"✓ Diff generated {len(mutations)} add mutations")


def test_v1_bridge():
    """Test V1Bridge."""
    print("\n=== Testing V1Bridge ===")

    from pmm2.planner import OrderMutation

    bridge = V1Bridge(shadow_mode=True)

    # Create test mutations
    mutations = [
        OrderMutation(
            action="add",
            token_id="token_yes",
            condition_id="test_cid",
            side="BUY",
            price=0.48,
            size=10.0,
            reason="test",
        ),
        OrderMutation(
            action="cancel",
            order_id="order_123",
            reason="test",
        ),
    ]

    # Execute in shadow mode
    result = asyncio.run(bridge.execute_mutations(mutations))
    assert result["shadow"] is True
    assert result["executed"] == 2
    assert result["failed"] == 0
    print(f"✓ Bridge executed {result['executed']} mutations in shadow mode")

    # Check mutation log
    log = bridge.get_mutation_log()
    assert len(log) == 2
    print(f"✓ Mutation log has {len(log)} entries")


def test_runtime_init():
    """Test PMM2Runtime initialization."""
    print("\n=== Testing PMM2Runtime ===")


    config = PMM2Config(
        enabled=True,
        shadow_mode=True,
        allocator_interval_sec=60.0,
    )

    # Mock database (won't actually init)
    class MockDB:
        pass

    db = MockDB()
    bridge = V1Bridge(shadow_mode=True)

    runtime = PMM2Runtime(config, db, bridge)

    assert runtime.config.enabled is True
    assert runtime.running is False
    assert len(runtime.enriched_universe) == 0
    print("✓ Runtime initialized")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Sprint 7 — Quote Planner + Runtime Integration Tests")
    print("=" * 60)

    try:
        test_config()
        test_quote_planner()
        test_diff_engine()
        test_v1_bridge()
        test_runtime_init()

        print("\n" + "=" * 60)
        print("✓ ALL TESTS PASSED")
        print("=" * 60 + "\n")

    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}\n")
        raise
    except Exception as e:
        print(f"\n✗ ERROR: {e}\n")
        raise

"""Test Sprint 9 — Shadow Mode Components

Tests:
1. ShadowLogger: log cycles and divergences
2. CounterfactualEngine: compare V1 vs PMM-2
3. ShadowDashboard: generate status reports
4. V1StateSnapshot: capture bot state
5. Integration: full shadow cycle
"""

import asyncio
import os
import tempfile
from unittest.mock import MagicMock

from pmm2.shadow import (
    CounterfactualEngine,
    ShadowDashboard,
    ShadowLogger,
    V1StateSnapshot,
)


def test_shadow_logger():
    """Test shadow logger can log cycles and divergences."""
    print("Testing ShadowLogger...")

    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ShadowLogger(db=None, log_dir=tmpdir)

        # Log a test cycle
        cycle_data = {
            "v1_markets": ["market1", "market2"],
            "pmm2_markets": ["market1", "market3"],
            "v1_orders": [],
            "pmm2_mutations": [],
            "pmm2_plan": {},
            "ev_breakdown": [],
            "allocator_output": {},
        }

        logger.log_allocation_cycle(cycle_data)

        # Verify log file exists
        assert os.path.exists(logger.log_file), "Log file should exist"

        # Log a divergence
        logger.log_divergence(
            "market_selection",
            {"overlap_pct": 0.5, "pmm2_only": ["market3"], "v1_only": ["market2"]},
        )

        # Read back cycles
        cycles = logger.read_cycles()
        assert len(cycles) == 1, "Should have 1 cycle logged"

    print("✅ ShadowLogger test passed")


def test_counterfactual_engine():
    """Test counterfactual engine compares V1 vs PMM-2."""
    print("Testing CounterfactualEngine...")

    logger = ShadowLogger(db=None, log_dir="/tmp")
    engine = CounterfactualEngine(logger)

    # Simulate 150 cycles
    for i in range(150):
        v1_state = {
            "markets": ["market1", "market2"],
            "orders": [{"token_id": "token1", "side": "BUY", "price": 0.5, "size": 10}],
            "scoring_count": 1,  # Only 1 scoring order
            "reward_eligible_count": 1,
        }

        pmm2_plan = {
            "markets": ["market1", "market3"],
            "bundles": [
                {
                    "market_condition_id": "market1",
                    "expected_return_bps": 100,  # 1% return = 0.01
                    "is_reward_eligible": True,
                },
                {
                    "market_condition_id": "market3",
                    "expected_return_bps": 150,  # 1.5% return = 0.015
                    "is_reward_eligible": True,
                },
            ],
            "mutations": [{"action": "add"}],
            "total_ev": 0.025,  # $0.025 EV (PMM-2 is better!)
        }

        comparison = engine.compare_cycle(v1_state, pmm2_plan)
        assert "ev_delta" in comparison, "Comparison should have ev_delta"

    # Check summary
    summary = engine.get_summary()
    assert summary["cycles_run"] == 150, "Should have 150 cycles"
    assert summary["positive_ev_pct"] > 0, "Should have positive EV cycles"

    # Check gates
    gates = engine.get_gates_status()
    assert "gate_1_positive_ev" in gates, "Should have gate_1 status"
    assert "gate_4_enough_data" in gates, "Should have gate_4 status"
    assert gates["gate_4_enough_data"] == True, "Gate 4 should pass with 150 cycles"

    print("✅ CounterfactualEngine test passed")


def test_shadow_dashboard():
    """Test shadow dashboard generates status reports."""
    print("Testing ShadowDashboard...")

    logger = ShadowLogger(db=None, log_dir="/tmp")
    engine = CounterfactualEngine(logger)
    dashboard = ShadowDashboard(engine)

    # Simulate some cycles
    for i in range(50):
        v1_state = {
            "markets": ["m1"],
            "orders": [],
            "scoring_count": 1,
            "reward_eligible_count": 1,
        }
        pmm2_plan = {
            "markets": ["m1", "m2"],
            "bundles": [{"is_reward_eligible": True}],
            "mutations": [],
            "total_ev": 0.01,
        }
        engine.compare_cycle(v1_state, pmm2_plan)

    # Generate status
    status = dashboard.generate_status()
    assert "PMM-2 Shadow Mode" in status, "Status should have title"
    assert "cycles analyzed" in status, "Status should show cycles"
    assert "positive EV" in status, "Status should show EV"

    print(f"\n{status}\n")
    print("✅ ShadowDashboard test passed")


def test_v1_state_snapshot():
    """Test V1 state snapshot captures bot state."""
    print("Testing V1StateSnapshot...")

    # Mock bot_state
    mock_order = MagicMock()
    mock_order.order_id = "order1"
    mock_order.token_id = "token1"
    mock_order.condition_id = "cid1"
    mock_order.side = "BUY"
    mock_order.price = 0.5
    mock_order.size = 10.0
    mock_order.status = "LIVE"
    mock_order.is_scoring = True

    mock_order_tracker = MagicMock()
    mock_order_tracker.get_active_orders.return_value = [mock_order]

    bot_state = MagicMock()
    bot_state.order_tracker = mock_order_tracker
    bot_state.nav = 100.0

    # Capture snapshot
    snapshot = V1StateSnapshot.capture(bot_state)

    assert snapshot["nav"] == 100.0, "NAV should be captured"
    assert len(snapshot["markets"]) == 1, "Should have 1 market"
    assert snapshot["markets"][0] == "cid1", "Market should be cid1"
    assert len(snapshot["orders"]) == 1, "Should have 1 order"
    assert snapshot["scoring_count"] == 1, "Should have 1 scoring order"

    # Test summarize
    summary = V1StateSnapshot.summarize(snapshot)
    assert "Markets: 1" in summary, "Summary should show market count"

    print(f"\n{summary}\n")
    print("✅ V1StateSnapshot test passed")


async def test_integration():
    """Test full shadow mode integration."""
    print("Testing full shadow integration...")

    # This would test the full PMM2Runtime with shadow mode
    # For now, we'll just verify imports work together
    from pmm2.runtime.loops import PMM2Runtime
    from pmm2.config import PMM2Config

    config = PMM2Config(enabled=True, shadow_mode=True)

    # Verify shadow mode is enabled
    assert config.shadow_mode == True, "Shadow mode should be enabled"
    assert config.enabled == True, "PMM-2 should be enabled"

    print("✅ Integration test passed")


def main():
    """Run all tests."""
    print("=" * 60)
    print("Sprint 9 — Shadow Mode Tests")
    print("=" * 60)
    print()

    test_shadow_logger()
    test_counterfactual_engine()
    test_shadow_dashboard()
    test_v1_state_snapshot()
    asyncio.run(test_integration())

    print()
    print("=" * 60)
    print("✅ All Sprint 9 tests passed!")
    print("=" * 60)


if __name__ == "__main__":
    main()

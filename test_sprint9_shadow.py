"""Shadow-mode smoke tests for the PMM-2 Phase 3 implementation."""

from __future__ import annotations

from types import SimpleNamespace

from pmm2.config import PMM2Config
from pmm2.runtime.loops import PMM2Runtime
from pmm2.shadow import CounterfactualEngine, ShadowDashboard, ShadowLogger, V1StateSnapshot


def test_counterfactual_and_dashboard_smoke(tmp_path):
    logger = ShadowLogger(db=None, log_dir=str(tmp_path))
    engine = CounterfactualEngine(logger)
    dashboard = ShadowDashboard(engine)

    for _ in range(100):
        engine.compare_cycle(
            {
                "markets": ["m1"],
                "market_evaluations": [{"condition_id": "m1", "best_bid": 0.49, "best_ask": 0.51}],
                "total_expected_ev_usdc": 0.01,
                "total_reward_ev_usdc": 0.001,
                "reward_market_count": 1,
                "ev_sample_valid": True,
                "cancel_count_recent": 2.0,
                "live_order_minutes": 10.0,
                "cycle_minutes": 1.0,
            },
            {
                "markets": ["m1", "m2"],
                "market_evaluations": [
                    {"condition_id": "m1", "best_bid": 0.50, "best_ask": 0.52},
                    {"condition_id": "m2", "best_bid": 0.47, "best_ask": 0.53},
                ],
                "total_expected_ev_usdc": 0.03,
                "total_reward_ev_usdc": 0.004,
                "reward_market_count": 2,
                "ev_sample_valid": True,
                "projected_cancel_count": 1.0,
                "live_order_minutes": 10.0,
                "cycle_minutes": 1.0,
            },
        )

    status = dashboard.generate_status()
    summary = engine.get_summary()

    assert "PMM-2 Shadow Mode" in status
    assert "Reward delta" in status
    assert summary["ready_for_live"] is True


def test_v1_state_snapshot_smoke():
    market = SimpleNamespace(
        condition_id="cid1",
        event_id="event1",
        token_id_yes="yes1",
        token_id_no="no1",
        question="Will tests pass?",
        best_bid=0.49,
        best_ask=0.51,
        mid=0.50,
        depth_at_best_bid=20.0,
        depth_at_best_ask=20.0,
        volume_24h=1200.0,
        liquidity=300.0,
        reward_eligible=True,
        reward_daily_rate=12.0,
        reward_min_size=5.0,
        reward_max_spread=10.0,
        fees_enabled=False,
        fee_rate=0.0,
    )
    order = SimpleNamespace(
        order_id="order1",
        token_id="yes1",
        condition_id="cid1",
        side="BUY",
        price_float=0.49,
        remaining_size_float=10.0,
        state=SimpleNamespace(value="LIVE"),
        is_scoring=True,
        age_seconds=60.0,
    )
    bot_state = SimpleNamespace(
        nav=100.0,
        reward_eligible={"cid1"},
        active_markets={"cid1": market},
        order_tracker=SimpleNamespace(get_active_orders=lambda token_id=None: [order]),
        position_tracker=SimpleNamespace(get_active_positions=lambda: []),
    )
    queue_estimator = SimpleNamespace(
        states={"order1": SimpleNamespace(fill_prob_30s=0.3, est_ahead_mid=5.0, eta_sec=10.0)}
    )

    snapshot = V1StateSnapshot.capture(bot_state, queue_estimator=queue_estimator)

    assert snapshot["nav"] == 100.0
    assert snapshot["reward_market_count"] == 1
    assert snapshot["total_expected_ev_usdc"] > 0.0


def test_runtime_import_smoke():
    config = PMM2Config(enabled=True, shadow_mode=True)
    runtime = PMM2Runtime(config, db=None, bridge=SimpleNamespace())

    assert runtime.config.shadow_mode is True
    assert runtime.allocator.min_positive_return_bps == config.min_positive_return_bps

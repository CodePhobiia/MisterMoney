"""Tests for EdgeTracker — Paper 2 §5 statistical validation."""

import random

from pmm1.analytics.edge_tracker import EdgeTracker


def test_edge_tracker_empty():
    """Empty tracker has safe defaults."""
    tracker = EdgeTracker()
    summary = tracker.get_summary()
    assert summary["total_trades"] == 0
    assert summary["sprt_decision"] == "undecided"
    assert summary["edge_confidence"] == 0.1


def test_edge_tracker_low_trade_count():
    """With few trades, confidence ramps linearly."""
    tracker = EdgeTracker(min_trades=100)
    # Record 50 trades (half of min_trades)
    for _ in range(50):
        tracker.record_trade(
            predicted_p=0.60,
            market_p=0.50,
            outcome=1.0,
            pnl=0.10,
        )
    confidence = tracker.get_edge_confidence()
    # Should be between 0.1 and 0.5 (linear ramp)
    assert 0.25 < confidence < 0.45


def test_edge_tracker_confirms_edge():
    """Paper 2: SPRT confirms edge with biased outcomes."""
    tracker = EdgeTracker(min_trades=20)
    random.seed(42)

    for _ in range(500):
        outcome = 1.0 if random.random() < 0.60 else 0.0
        pnl = 0.40 if outcome == 1.0 else -0.50
        tracker.record_trade(
            predicted_p=0.60,
            market_p=0.50,
            outcome=outcome,
            pnl=pnl,
        )
        if tracker.sprt_decision != "undecided":
            break

    assert tracker.sprt_decision == "edge_confirmed"
    assert tracker.get_edge_confidence() == 1.0


def test_edge_tracker_no_edge():
    """When outcomes match market price, no edge detected."""
    tracker = EdgeTracker(min_trades=20)
    random.seed(42)

    for _ in range(300):
        outcome = 1.0 if random.random() < 0.50 else 0.0
        pnl = 0.50 if outcome == 1.0 else -0.50
        tracker.record_trade(
            predicted_p=0.55,  # Claims edge but none exists
            market_p=0.50,
            outcome=outcome,
            pnl=pnl,
        )
        if tracker.sprt_decision != "undecided":
            break

    assert tracker.sprt_decision in ("no_edge", "undecided")
    if tracker.sprt_decision == "no_edge":
        assert tracker.get_edge_confidence() == 0.1


def test_edge_tracker_brier_score():
    """Brier score tracks prediction quality."""
    tracker = EdgeTracker()
    # Perfect predictions
    tracker.record_trade(0.90, 0.50, 1.0, 0.10)
    tracker.record_trade(0.10, 0.50, 0.0, 0.10)
    bs = tracker.get_brier_score()
    assert bs < 0.05  # Very good


def test_edge_tracker_summary_completeness():
    """Summary contains all expected fields."""
    tracker = EdgeTracker()
    tracker.record_trade(0.60, 0.50, 1.0, 0.10)
    tracker.record_trade(0.60, 0.50, 0.0, -0.10)

    summary = tracker.get_summary()
    expected_keys = {
        "total_trades", "total_pnl", "win_rate",
        "brier_score", "sprt_decision", "sprt_log_ratio",
        "edge_confidence", "rolling_sharpe",
        "trades_to_significance", "per_trade_sharpe",
        "decision_history",
    }
    assert expected_keys.issubset(summary.keys())
    assert summary["total_trades"] == 2
    assert summary["win_rate"] == 0.5


def test_edge_tracker_required_trades():
    """Paper 2: ~618 trades for 5% edge at even odds."""
    tracker = EdgeTracker(target_edge=0.05)
    remaining = tracker.get_required_trades()
    assert 600 <= remaining <= 650


def test_sliding_window_resets_sprt():
    """SPRT resets after window_size trades past decision."""
    tracker = EdgeTracker(min_trades=10)
    tracker.window_size = 50
    random.seed(42)
    # First: confirm edge
    for _ in range(200):
        outcome = 1.0 if random.random() < 0.60 else 0.0
        tracker.record_trade(0.60, 0.50, outcome, 0.10 if outcome else -0.10)

    first_decision = tracker.sprt_decision
    # Should have confirmed and then possibly reset
    assert len(tracker._decision_history) >= 1 or first_decision != "undecided"


def test_running_wins_counts_outcomes_not_pnl():
    """Q-C2: _running_wins should count outcome>0.5, not pnl>0."""
    tracker = EdgeTracker()
    # Trade where outcome=1.0 (YES resolved) but pnl is negative
    tracker.record_trade(predicted_p=0.7, market_p=0.6, outcome=1.0, pnl=-0.05)
    assert tracker._running_wins == 1  # counted as win (outcome=1.0)
    # Trade where outcome=0.0 (NO resolved) but pnl is positive
    tracker.record_trade(predicted_p=0.3, market_p=0.4, outcome=0.0, pnl=0.10)
    assert tracker._running_wins == 1  # NOT counted (outcome=0.0)


def test_counters_reset_on_window_slide():
    """Q-M3: Running counters should reset when SPRT window slides."""
    tracker = EdgeTracker(min_trades=5)
    # Force a decision
    for _ in range(100):
        tracker.record_trade(predicted_p=0.8, market_p=0.5, outcome=1.0, pnl=0.1)
    # Should have decided by now
    assert tracker.sprt_decision != "undecided"
    old_decision_idx = tracker._decision_trade_index  # noqa: F841
    # Add window_size more trades to trigger window slide
    for _ in range(tracker.window_size):
        tracker.record_trade(predicted_p=0.5, market_p=0.5, outcome=1.0, pnl=0.01)
    # Counters should have reset
    assert tracker._running_total < tracker.window_size + 50  # not accumulating from start

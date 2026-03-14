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
    }
    assert expected_keys.issubset(summary.keys())
    assert summary["total_trades"] == 2
    assert summary["win_rate"] == 0.5


def test_edge_tracker_required_trades():
    """Paper 2: ~618 trades for 5% edge at even odds."""
    tracker = EdgeTracker(target_edge=0.05)
    remaining = tracker.get_required_trades()
    assert 600 <= remaining <= 650

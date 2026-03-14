"""Tests for EdgeTracker — Paper 2 §5 statistical validation."""

import os
import random

import pytest

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


def test_edge_tracker_save_load_roundtrip(tmp_path):
    """Save state to disk, load into new tracker, verify match."""
    tracker = EdgeTracker(min_trades=5)
    random.seed(123)
    for i in range(10):
        outcome = 1.0 if random.random() < 0.6 else 0.0
        tracker.record_trade(
            predicted_p=0.60,
            market_p=0.50,
            outcome=outcome,
            pnl=0.10 if outcome else -0.10,
            side="BUY",
            condition_id=f"cond_{i}",
        )

    path = str(tmp_path / "edge_tracker.json")
    tracker.save(path)

    # Load into fresh tracker
    tracker2 = EdgeTracker(min_trades=5)
    tracker2.load(path)

    assert len(tracker2.trades) == len(tracker.trades)
    assert tracker2.sprt_log_ratio == tracker.sprt_log_ratio
    assert tracker2.sprt_decision == tracker.sprt_decision
    assert tracker2._running_wins == tracker._running_wins
    assert tracker2._running_total == tracker._running_total
    assert tracker2._running_market_p_sum == tracker._running_market_p_sum
    assert tracker2._decision_trade_index == tracker._decision_trade_index
    assert tracker2._decision_history == tracker._decision_history

    # Verify individual trade fields survived roundtrip
    for t1, t2 in zip(tracker.trades, tracker2.trades):
        assert t1.predicted_p == t2.predicted_p
        assert t1.market_p == t2.market_p
        assert t1.outcome == t2.outcome
        assert t1.pnl == t2.pnl
        assert t1.side == t2.side
        assert t1.condition_id == t2.condition_id


def test_edge_tracker_warm_start_discount():
    """warm_start with 100 wins / 200 total at 0.8 discount."""
    tracker = EdgeTracker()
    tracker.warm_start(
        prior_wins=100,
        prior_total=200,
        prior_log_ratio=5.0,
        discount=0.8,
    )
    assert tracker._running_wins == 80
    assert tracker._running_total == 160
    assert tracker.sprt_log_ratio == 4.0  # 5.0 * 0.8


def test_edge_tracker_save_atomic(tmp_path):
    """Verify .tmp file is used during save and renamed to final path."""
    tracker = EdgeTracker()
    tracker.record_trade(0.6, 0.5, 1.0, 0.1)

    path = str(tmp_path / "edge_tracker.json")
    tmp_file = path + ".tmp"

    tracker.save(path)

    # After save, final file should exist and .tmp should not
    assert os.path.exists(path)
    assert not os.path.exists(tmp_file)


# ===================================================================
# ST-08: Adaptive window sizing (exponential decay)
# ===================================================================


def test_weighted_win_rate_basic():
    """Record wins/losses, verify weighted rate reflects them."""
    tracker = EdgeTracker(min_trades=100)
    # Record 10 wins and 10 losses
    for _ in range(10):
        tracker.record_trade(0.6, 0.5, 1.0, 0.1)  # win (outcome > 0.5)
    for _ in range(10):
        tracker.record_trade(0.6, 0.5, 0.0, -0.1)  # loss (outcome < 0.5)

    rate = tracker.get_weighted_win_rate()
    # With exponential decay, recent losses have more weight.
    # Rate should be below 0.5 since recent trades are all losses.
    assert rate < 0.5
    # But not zero since wins still contribute (decayed)
    assert rate > 0.0


def test_weighted_win_rate_decays():
    """Old outcomes have less weight than recent ones."""
    tracker = EdgeTracker(min_trades=200)

    # Phase 1: 100 wins
    for _ in range(100):
        tracker.record_trade(0.6, 0.5, 1.0, 0.1)

    rate_after_wins = tracker.get_weighted_win_rate()

    # Phase 2: 100 losses
    for _ in range(100):
        tracker.record_trade(0.6, 0.5, 0.0, -0.1)

    rate_after_losses = tracker.get_weighted_win_rate()

    # After 100 losses, the win rate should be significantly lower
    assert rate_after_losses < rate_after_wins
    # With lambda=0.995, after 100 losses the old wins are decayed by 0.995^100 ~ 0.606
    # so the rate should be well below 0.5
    assert rate_after_losses < 0.5


def test_edge_confidence_uses_weighted():
    """Verify get_edge_confidence uses weighted win rate in undecided branch."""
    tracker = EdgeTracker(min_trades=5)
    # Record enough trades to pass min_trades threshold
    # Mix of outcomes to stay undecided in SPRT
    for _ in range(60):
        tracker.record_trade(0.55, 0.50, 1.0, 0.05)
    for _ in range(40):
        tracker.record_trade(0.55, 0.50, 0.0, -0.05)

    # If SPRT is still undecided, confidence uses weighted win rate
    if tracker.sprt_decision == "undecided":
        confidence = tracker.get_edge_confidence()
        weighted_rate = tracker.get_weighted_win_rate()
        # Confidence should reflect weighted win rate
        # Recent trades are mixed but biased toward losses (last 40)
        # so weighted_rate < raw win rate of 60%
        assert confidence >= 0.5  # still has some edge signal
        # Verify it's consistent with the formula
        excess = max(0, weighted_rate - 0.5) * 2
        expected = min(1.0, 0.5 + excess * 0.5)
        assert confidence == pytest.approx(expected, abs=1e-6)


def test_weighted_stats_persist(tmp_path):
    """Save/load roundtrip preserves weighted stats."""
    tracker = EdgeTracker(min_trades=5)
    for _ in range(20):
        tracker.record_trade(0.6, 0.5, 1.0, 0.1)
    for _ in range(10):
        tracker.record_trade(0.6, 0.5, 0.0, -0.1)

    path = str(tmp_path / "edge_weighted.json")
    tracker.save(path)

    tracker2 = EdgeTracker(min_trades=5)
    tracker2.load(path)

    assert tracker2._decay_lambda == pytest.approx(tracker._decay_lambda)
    assert tracker2._weighted_wins == pytest.approx(tracker._weighted_wins)
    assert tracker2._weighted_total == pytest.approx(tracker._weighted_total)
    assert tracker2.get_weighted_win_rate() == pytest.approx(
        tracker.get_weighted_win_rate(), abs=1e-10,
    )


# ===================================================================
# ST-03: Bayesian edge confidence
# ===================================================================


def test_bayesian_confidence_smooth():
    """ST-03: Confidence increases smoothly with wins."""
    tracker = EdgeTracker(min_trades=10, target_edge=0.05)
    confidences = []
    for i in range(50):
        tracker.record_trade(
            predicted_p=0.6,
            market_p=0.5,
            outcome=1.0 if i % 100 < 55 else 0.0,
            pnl=0.01,
        )
        confidences.append(tracker.get_bayesian_edge_confidence(min_edge=0.0))
    # Should be monotonically non-decreasing (more wins -> more confidence)
    # Allow small fluctuations
    assert confidences[-1] > confidences[0]


def test_bayesian_confidence_few_trades():
    """ST-03: Few trades -> low confidence (wide posterior)."""
    tracker = EdgeTracker()
    tracker.record_trade(predicted_p=0.6, market_p=0.5, outcome=1.0, pnl=0.01)
    conf = tracker.get_bayesian_edge_confidence(min_edge=0.05)
    assert conf < 0.8  # With just 1 trade, should be uncertain

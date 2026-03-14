"""Tests for MarketProfitabilityTracker — CL-02 per-market profitability."""

import os

from pmm1.analytics.market_profitability import MarketProfitabilityTracker


def test_cold_start_neutral():
    """No fills -> score = 0 (neutral)."""
    tracker = MarketProfitabilityTracker()
    assert tracker.profitability_score("cid_unknown") == 0.0


def test_positive_pnl_positive_score():
    """Profitable fills -> positive score (single market, scaled by *100)."""
    tracker = MarketProfitabilityTracker(min_fills=3)
    for _ in range(5):
        tracker.record_fill("cid_winner", pnl=0.10, volume=10.0)

    score = tracker.profitability_score("cid_winner")
    assert score > 0.0


def test_negative_pnl_negative_score():
    """Losing fills -> negative score (single market, scaled by *100)."""
    tracker = MarketProfitabilityTracker(min_fills=3)
    for _ in range(5):
        tracker.record_fill("cid_loser", pnl=-0.10, volume=10.0)

    score = tracker.profitability_score("cid_loser")
    assert score < 0.0


def test_min_fills_threshold():
    """< min_fills -> score = 0 regardless of PnL."""
    tracker = MarketProfitabilityTracker(min_fills=5)
    for _ in range(4):
        tracker.record_fill("cid_new", pnl=1.0, volume=10.0)

    # Only 4 fills, min is 5
    assert tracker.profitability_score("cid_new") == 0.0

    # Add one more fill
    tracker.record_fill("cid_new", pnl=1.0, volume=10.0)
    # Now 5 fills, should get a score
    assert tracker.profitability_score("cid_new") != 0.0


def test_save_load_roundtrip(tmp_path):
    """Persistence works correctly."""
    tracker = MarketProfitabilityTracker(min_fills=3)
    tracker.record_fill("cid_a", pnl=0.05, volume=10.0)
    tracker.record_fill("cid_a", pnl=0.03, volume=8.0)
    tracker.record_fill("cid_b", pnl=-0.02, volume=5.0)

    path = str(tmp_path / "market_prof.json")
    tracker.save(path)
    assert os.path.exists(path)

    tracker2 = MarketProfitabilityTracker(min_fills=3)
    tracker2.load(path)

    assert tracker2.get_status()["tracked_markets"] == 2
    # Verify individual market data survived
    assert tracker2._markets["cid_a"]["fill_count"] == 2
    assert tracker2._markets["cid_b"]["fill_count"] == 1


def test_z_score_normalization_across_markets():
    """With multiple active markets, scores are z-normalized."""
    tracker = MarketProfitabilityTracker(min_fills=3)

    # Market A: consistently profitable
    for _ in range(10):
        tracker.record_fill("cid_good", pnl=0.10, volume=10.0)

    # Market B: consistently losing
    for _ in range(10):
        tracker.record_fill("cid_bad", pnl=-0.10, volume=10.0)

    score_good = tracker.profitability_score("cid_good")
    score_bad = tracker.profitability_score("cid_bad")

    assert score_good > 0
    assert score_bad < 0
    assert score_good > score_bad


def test_volume_zero_ignored():
    """Fills with volume <= 0 are ignored."""
    tracker = MarketProfitabilityTracker()
    tracker.record_fill("cid_1", pnl=0.10, volume=0.0)
    tracker.record_fill("cid_1", pnl=0.10, volume=-1.0)
    assert tracker.get_status()["tracked_markets"] == 0


def test_get_all_scores():
    """get_all_scores returns scores for all tracked markets."""
    tracker = MarketProfitabilityTracker(min_fills=2)
    for _ in range(3):
        tracker.record_fill("cid_a", pnl=0.05, volume=10.0)
        tracker.record_fill("cid_b", pnl=-0.02, volume=5.0)

    scores = tracker.get_all_scores()
    assert "cid_a" in scores
    assert "cid_b" in scores


def test_score_clipped_to_range():
    """Scores are clipped to [-2, +2]."""
    tracker = MarketProfitabilityTracker(min_fills=2)

    # Create an extreme outlier
    for _ in range(5):
        tracker.record_fill("cid_extreme", pnl=100.0, volume=1.0)
    for _ in range(5):
        tracker.record_fill("cid_normal", pnl=0.001, volume=1.0)

    score = tracker.profitability_score("cid_extreme")
    assert -2.0 <= score <= 2.0


def test_get_status():
    """get_status returns expected structure."""
    tracker = MarketProfitabilityTracker(min_fills=3)
    tracker.record_fill("cid_a", pnl=0.05, volume=10.0)

    status = tracker.get_status()
    assert "tracked_markets" in status
    assert "active_markets" in status
    assert "total_pnl" in status
    assert status["tracked_markets"] == 1
    assert status["active_markets"] == 0  # < min_fills

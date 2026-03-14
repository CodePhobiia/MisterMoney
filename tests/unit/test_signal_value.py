"""Tests for SignalValueTracker — CL-06 LLM signal value measurement."""

import os

from pmm1.analytics.signal_value import SignalValueTracker


def test_ic_positive_correlation():
    """LLM correctly predicts direction -> IC > 0."""
    tracker = SignalValueTracker(window=200)

    # LLM consistently shifts FV in the direction the market moves
    for i in range(30):
        market_mid = 0.50
        # LLM says price will go up -> blended_fv > market_mid
        blended_fv = 0.52
        # And price does go up
        mid_5s_after = 0.53

        tracker.record_fill(
            blended_fv=blended_fv,
            market_mid=market_mid,
            fill_price=0.49,
            side="BUY",
            pnl=0.01,
            llm_used=True,
        )
        tracker.update_post_fill(i, mid_5s_after)

    ic = tracker.compute_ic()
    # All deviations are positive (0.02) and all outcomes are positive (0.03)
    # Perfect rank correlation
    assert ic > 0.0


def test_ic_insufficient_data():
    """< 10 observations -> IC = 0."""
    tracker = SignalValueTracker()

    for i in range(5):
        tracker.record_fill(
            blended_fv=0.52,
            market_mid=0.50,
            fill_price=0.49,
            side="BUY",
            pnl=0.01,
            llm_used=True,
        )
        tracker.update_post_fill(i, 0.53)

    ic = tracker.compute_ic()
    assert ic == 0.0


def test_value_add_positive():
    """LLM improves over market mid -> positive value-add."""
    tracker = SignalValueTracker()

    # LLM correctly shifts FV to buy cheaper
    for _ in range(10):
        # Market mid is 0.50, LLM says true FV is 0.55
        # We buy at 0.48
        tracker.record_fill(
            blended_fv=0.55,
            market_mid=0.50,
            fill_price=0.48,
            side="BUY",
            pnl=0.07,
            llm_used=True,
        )

    value_add = tracker.compute_value_add()
    # For BUY: actual_edge = 0.55 - 0.48 = 0.07
    #          counterfactual_edge = 0.50 - 0.48 = 0.02
    #          per-fill add = 0.07 - 0.02 = 0.05
    # Total = 10 * 0.05 = 0.50
    assert abs(value_add - 0.50) < 0.001


def test_value_add_sell_side():
    """Value-add works correctly for SELL side."""
    tracker = SignalValueTracker()

    # LLM correctly shifts FV down, we sell at higher price
    tracker.record_fill(
        blended_fv=0.45,
        market_mid=0.50,
        fill_price=0.52,
        side="SELL",
        pnl=0.07,
        llm_used=True,
    )

    value_add = tracker.compute_value_add()
    # For SELL: actual_edge = 0.52 - 0.45 = 0.07
    #           counterfactual_edge = 0.52 - 0.50 = 0.02
    #           add = 0.07 - 0.02 = 0.05
    assert abs(value_add - 0.05) < 0.001


def test_roi_computation():
    """value_add / cost = ROI."""
    tracker = SignalValueTracker()

    # Generate $1.00 of value-add
    for _ in range(20):
        tracker.record_fill(
            blended_fv=0.55,
            market_mid=0.50,
            fill_price=0.48,
            side="BUY",
            pnl=0.07,
            llm_used=True,
        )

    # Set daily cost to $0.50
    tracker.set_daily_cost(0.50)
    roi = tracker.compute_roi()

    # value_add = 20 * 0.05 = 1.00, cost = 0.50, ROI = 2.0
    assert roi is not None
    assert abs(roi - 2.0) < 0.01


def test_roi_none_without_cost():
    """ROI is None when no cost data is set."""
    tracker = SignalValueTracker()
    tracker.record_fill(
        blended_fv=0.55,
        market_mid=0.50,
        fill_price=0.48,
        side="BUY",
        pnl=0.07,
        llm_used=True,
    )
    assert tracker.compute_roi() is None


def test_spearman_perfect():
    """Perfect positive correlation -> 1.0."""
    result = SignalValueTracker._spearman(
        [1.0, 2.0, 3.0, 4.0, 5.0],
        [10.0, 20.0, 30.0, 40.0, 50.0],
    )
    assert abs(result - 1.0) < 1e-10


def test_spearman_perfect_negative():
    """Perfect negative correlation -> -1.0."""
    result = SignalValueTracker._spearman(
        [1.0, 2.0, 3.0, 4.0, 5.0],
        [50.0, 40.0, 30.0, 20.0, 10.0],
    )
    assert abs(result - (-1.0)) < 1e-10


def test_spearman_too_few():
    """< 3 values -> 0.0."""
    assert SignalValueTracker._spearman([1.0, 2.0], [3.0, 4.0]) == 0.0


def test_save_load_roundtrip(tmp_path):
    """Save, load, verify observations preserved."""
    tracker = SignalValueTracker(window=100)
    for i in range(5):
        tracker.record_fill(
            blended_fv=0.55,
            market_mid=0.50,
            fill_price=0.48,
            side="BUY",
            pnl=0.07,
            llm_used=True,
        )

    path = str(tmp_path / "signal_value.json")
    tracker.save(path)
    assert os.path.exists(path)

    tracker2 = SignalValueTracker(window=100)
    tracker2.load(path)
    assert len(tracker2._observations) == 5
    assert tracker2._observations[0].blended_fv == 0.55
    assert tracker2._observations[0].llm_used is True


def test_window_trimming():
    """Observations are trimmed to window size."""
    tracker = SignalValueTracker(window=10)

    for _ in range(25):
        tracker.record_fill(
            blended_fv=0.55,
            market_mid=0.50,
            fill_price=0.48,
            side="BUY",
            pnl=0.07,
            llm_used=True,
        )

    # Window is 10, trim happens at 2*window=20
    assert len(tracker._observations) <= 20


def test_get_status():
    """get_status returns expected fields."""
    tracker = SignalValueTracker()
    tracker.record_fill(
        blended_fv=0.55,
        market_mid=0.50,
        fill_price=0.48,
        side="BUY",
        pnl=0.07,
        llm_used=True,
    )
    tracker.record_fill(
        blended_fv=0.50,
        market_mid=0.50,
        fill_price=0.49,
        side="BUY",
        pnl=0.01,
        llm_used=False,
    )

    status = tracker.get_status()
    assert status["total_observations"] == 2
    assert status["llm_fills"] == 1
    assert "ic" in status
    assert "value_add_usd" in status
    assert "roi" in status
    assert "is_worth_it" in status


def test_non_llm_fills_excluded_from_value_add():
    """Fills without LLM should not contribute to value-add."""
    tracker = SignalValueTracker()
    tracker.record_fill(
        blended_fv=0.55,
        market_mid=0.50,
        fill_price=0.48,
        side="BUY",
        pnl=0.07,
        llm_used=False,
    )
    assert tracker.compute_value_add() == 0.0

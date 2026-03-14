"""Tests for MM-05: MarkoutTracker adverse selection measurement."""

from __future__ import annotations

import pytest

from pmm1.analytics.markout_tracker import MarkoutTracker


def test_record_and_update():
    """Record a fill, update 5s markout, verify the value is stored."""
    tracker = MarkoutTracker()
    idx = tracker.record_fill("tok-1", "cid-1", 0.55, "BUY", 0.56)
    tracker.update_markout(idx, current_mid=0.57, horizon="5s")

    pm = tracker._pending[idx]
    assert pm.markout_5s == pytest.approx(0.02)  # 0.57 - 0.55
    assert pm.markout_30s is None  # Not yet updated


def test_buy_adverse_selection():
    """BUY fill where price drops should give negative markout."""
    tracker = MarkoutTracker()
    idx = tracker.record_fill("tok-1", "cid-1", 0.55, "BUY", 0.56)
    tracker.update_markout(idx, current_mid=0.50, horizon="5s")

    pm = tracker._pending[idx]
    assert pm.markout_5s == pytest.approx(-0.05)  # 0.50 - 0.55 = adverse


def test_sell_adverse_selection():
    """SELL fill where price rises should give negative markout."""
    tracker = MarkoutTracker()
    idx = tracker.record_fill("tok-1", "cid-1", 0.55, "SELL", 0.54)
    tracker.update_markout(idx, current_mid=0.60, horizon="5s")

    pm = tracker._pending[idx]
    assert pm.markout_5s == pytest.approx(-0.05)  # 0.55 - 0.60 = adverse


def test_ewma_as_cost():
    """Multiple fills with 5m markouts should converge the EWMA AS cost."""
    tracker = MarkoutTracker(ewma_halflife=5)
    for i in range(10):
        idx = tracker.record_fill("tok-1", "cid-1", 0.50, "BUY", 0.50)
        # Price always drops by 0.02 after 5 minutes
        tracker.update_markout(idx, current_mid=0.48, horizon="5m")

    as_cost = tracker.get_as_cost("cid-1")
    # Should converge toward -0.02 (consistent adverse selection)
    assert as_cost < 0
    assert as_cost == pytest.approx(-0.02, abs=0.005)


def test_min_data_threshold():
    """With fewer than 5 fills, get_as_cost should return 0.0."""
    tracker = MarkoutTracker()
    for i in range(4):
        idx = tracker.record_fill("tok-1", "cid-1", 0.50, "BUY", 0.50)
        tracker.update_markout(idx, current_mid=0.48, horizon="5m")

    assert tracker.get_as_cost("cid-1") == 0.0


def test_spread_capture():
    """Spread capture = fv_at_fill - fill_price for BUY."""
    tracker = MarkoutTracker()
    idx_buy = tracker.record_fill("tok-1", "cid-1", 0.48, "BUY", 0.50)
    idx_sell = tracker.record_fill("tok-2", "cid-1", 0.52, "SELL", 0.50)

    assert tracker.get_spread_capture(idx_buy) == pytest.approx(0.02)   # 0.50 - 0.48
    assert tracker.get_spread_capture(idx_sell) == pytest.approx(0.02)  # 0.52 - 0.50


def test_spread_capture_out_of_bounds():
    """Spread capture for out-of-bounds index returns 0.0."""
    tracker = MarkoutTracker()
    assert tracker.get_spread_capture(-1) == 0.0
    assert tracker.get_spread_capture(999) == 0.0


def test_update_markout_out_of_bounds():
    """Update markout for out-of-bounds index is a no-op."""
    tracker = MarkoutTracker()
    tracker.update_markout(-1, current_mid=0.50, horizon="5s")  # Should not raise
    tracker.update_markout(999, current_mid=0.50, horizon="5s")  # Should not raise


def test_get_status():
    """get_status returns expected structure."""
    tracker = MarkoutTracker()
    status = tracker.get_status()
    assert "pending_fills" in status
    assert "tracked_markets" in status
    assert "markets_with_data" in status
    assert status["pending_fills"] == 0


def test_pending_cap():
    """Pending list should be capped at 5000 entries."""
    tracker = MarkoutTracker()
    for i in range(5010):
        tracker.record_fill("tok", "cid", 0.50, "BUY", 0.50)
    assert len(tracker._pending) == 5000

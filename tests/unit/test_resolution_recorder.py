"""Tests for F01 fix: ResolutionRecorder defers edge_tracker / fv_calibrator
calls until actual market resolution is known.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pmm1.analytics.resolution_recorder import ResolutionRecorder


@pytest.fixture()
def edge_tracker() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def fv_calibrator() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def recorder(edge_tracker: MagicMock, fv_calibrator: MagicMock) -> ResolutionRecorder:
    return ResolutionRecorder(edge_tracker, fv_calibrator)


# ── record_fill must NOT call edge_tracker or fv_calibrator ──


def test_record_fill_does_not_call_edge_tracker(
    recorder: ResolutionRecorder, edge_tracker: MagicMock,
) -> None:
    recorder.record_fill(
        condition_id="cid_abc",
        predicted_p=0.55,
        market_p=0.52,
        pnl=0.03,
        side="BUY",
    )
    edge_tracker.record_trade.assert_not_called()


def test_record_fill_does_not_call_fv_calibrator(
    recorder: ResolutionRecorder, fv_calibrator: MagicMock,
) -> None:
    recorder.record_fill(
        condition_id="cid_abc",
        predicted_p=0.55,
        market_p=0.52,
        pnl=0.03,
        side="BUY",
    )
    fv_calibrator.record_sample.assert_not_called()


# ── on_market_resolved calls edge_tracker with ACTUAL outcome ──


def test_resolution_calls_edge_tracker_with_actual_outcome(
    recorder: ResolutionRecorder, edge_tracker: MagicMock
) -> None:
    recorder.record_fill(
        condition_id="cid_yes",
        predicted_p=0.60,
        market_p=0.55,
        pnl=0.05,
        side="BUY",
    )
    recorder.on_market_resolved("cid_yes", outcome=1.0)

    edge_tracker.record_trade.assert_called_once()
    call_kwargs = edge_tracker.record_trade.call_args[1]
    assert call_kwargs["outcome"] == 1.0
    assert call_kwargs["predicted_p"] == 0.60
    assert call_kwargs["market_p"] == 0.55
    assert call_kwargs["pnl"] == 0.05
    assert call_kwargs["side"] == "BUY"
    assert call_kwargs["condition_id"] == "cid_yes"


def test_resolution_calls_fv_calibrator_with_actual_outcome(
    recorder: ResolutionRecorder, fv_calibrator: MagicMock
) -> None:
    recorder.record_fill(
        condition_id="cid_no",
        predicted_p=0.40,
        market_p=0.45,
        pnl=-0.05,
        side="SELL",
    )
    recorder.on_market_resolved("cid_no", outcome=0.0)

    fv_calibrator.record_sample.assert_called_once()
    call_kwargs = fv_calibrator.record_sample.call_args[1]
    assert call_kwargs["outcome"] == 0.0
    assert call_kwargs["predicted_p"] == 0.40
    assert call_kwargs["market_p"] == 0.45


# ── Both BUY and SELL fills get the SAME resolution outcome ──


def test_both_sides_get_same_resolution_outcome(
    recorder: ResolutionRecorder, edge_tracker: MagicMock
) -> None:
    """BUY and SELL fills on the same market must both receive the
    same resolution outcome (the bug was mapping BUY->1.0 and SELL->0.0)."""
    recorder.record_fill(
        condition_id="cid_mixed",
        predicted_p=0.55,
        market_p=0.52,
        pnl=0.03,
        side="BUY",
    )
    recorder.record_fill(
        condition_id="cid_mixed",
        predicted_p=0.55,
        market_p=0.57,
        pnl=-0.02,
        side="SELL",
    )
    recorder.on_market_resolved("cid_mixed", outcome=0.0)

    assert edge_tracker.record_trade.call_count == 2
    for call in edge_tracker.record_trade.call_args_list:
        assert call[1]["outcome"] == 0.0  # Both get 0.0 (NO resolved)


# ── pending_count and pending_markets ──


def test_pending_count_tracks_fills(recorder: ResolutionRecorder) -> None:
    assert recorder.pending_count == 0
    assert recorder.pending_markets == 0

    recorder.record_fill("cid_a", 0.5, 0.5, 0.0, "BUY")
    recorder.record_fill("cid_a", 0.5, 0.5, 0.0, "SELL")
    recorder.record_fill("cid_b", 0.5, 0.5, 0.0, "BUY")

    assert recorder.pending_count == 3
    assert recorder.pending_markets == 2


# ── After resolution, fills are removed from pending ──


def test_resolution_clears_pending(recorder: ResolutionRecorder) -> None:
    recorder.record_fill("cid_x", 0.5, 0.5, 0.0, "BUY")
    recorder.record_fill("cid_x", 0.5, 0.5, 0.0, "SELL")
    recorder.record_fill("cid_y", 0.5, 0.5, 0.0, "BUY")

    assert recorder.pending_count == 3
    assert recorder.pending_markets == 2

    recorder.on_market_resolved("cid_x", outcome=1.0)

    assert recorder.pending_count == 1
    assert recorder.pending_markets == 1


# ── Resolving unknown market is a no-op ──


def test_resolve_unknown_market_is_noop(
    recorder: ResolutionRecorder, edge_tracker: MagicMock, fv_calibrator: MagicMock
) -> None:
    recorder.on_market_resolved("cid_unknown", outcome=1.0)
    edge_tracker.record_trade.assert_not_called()
    fv_calibrator.record_sample.assert_not_called()


# ── Works with None edge_tracker / fv_calibrator ──


def test_none_edge_tracker() -> None:
    rec = ResolutionRecorder(edge_tracker=None, fv_calibrator=MagicMock())
    rec.record_fill("cid_a", 0.5, 0.5, 0.0, "BUY")
    rec.on_market_resolved("cid_a", outcome=1.0)  # should not raise


def test_none_fv_calibrator() -> None:
    rec = ResolutionRecorder(edge_tracker=MagicMock(), fv_calibrator=None)
    rec.record_fill("cid_a", 0.5, 0.5, 0.0, "BUY")
    rec.on_market_resolved("cid_a", outcome=1.0)  # should not raise


def test_both_none() -> None:
    rec = ResolutionRecorder(edge_tracker=None, fv_calibrator=None)
    rec.record_fill("cid_a", 0.5, 0.5, 0.0, "BUY")
    rec.on_market_resolved("cid_a", outcome=1.0)  # should not raise
    assert rec.pending_count == 0

"""Integration tests: fills are deferred until market resolution (F01 fix).

Validates that ResolutionRecorder buffers fills at fill time and only
records to EdgeTracker when the actual binary outcome is known.
"""

from __future__ import annotations

from pmm1.analytics.edge_tracker import EdgeTracker
from pmm1.analytics.fv_calibrator import FairValueCalibrator
from pmm1.analytics.resolution_recorder import ResolutionRecorder


def _make_recorder() -> tuple[ResolutionRecorder, EdgeTracker, FairValueCalibrator]:
    """Create a ResolutionRecorder wired to real EdgeTracker and FairValueCalibrator."""
    et = EdgeTracker(min_trades=5, target_edge=0.05)
    fv = FairValueCalibrator(min_samples=10)
    recorder = ResolutionRecorder(edge_tracker=et, fv_calibrator=fv)
    return recorder, et, fv


class TestFillDoesNotRecordToEdgeTracker:
    """Fills must be buffered, not immediately pushed to EdgeTracker."""

    def test_fill_does_not_record_to_edge_tracker(self) -> None:
        recorder, et, _ = _make_recorder()

        recorder.record_fill(
            condition_id="m1",
            predicted_p=0.65,
            market_p=0.55,
            pnl=0.10,
            side="BUY",
        )

        assert len(et.trades) == 0, "Fill must NOT be recorded to EdgeTracker at fill time"
        assert recorder.pending_count == 1


class TestResolutionRecordsWithActualOutcome:
    """After resolution, pending fills are flushed with the true outcome."""

    def test_resolution_records_with_actual_outcome(self) -> None:
        recorder, et, _ = _make_recorder()

        recorder.record_fill(
            condition_id="m1",
            predicted_p=0.65,
            market_p=0.55,
            pnl=0.10,
            side="BUY",
        )
        recorder.on_market_resolved("m1", outcome=1.0)

        assert len(et.trades) == 1
        assert et.trades[0].outcome == 1.0
        assert recorder.pending_count == 0


class TestBuySideGetsCorrectOutcomeOnNoResolution:
    """BUY fills resolved as NO must record outcome=0.0, not the old buggy 1.0."""

    def test_buy_side_gets_correct_outcome_on_no_resolution(self) -> None:
        recorder, et, _ = _make_recorder()

        recorder.record_fill(
            condition_id="m1",
            predicted_p=0.65,
            market_p=0.55,
            pnl=-0.55,
            side="BUY",
        )
        recorder.on_market_resolved("m1", outcome=0.0)

        assert len(et.trades) == 1
        assert et.trades[0].outcome == 0.0, (
            "BUY fill resolved as NO must record outcome=0.0, "
            "not 1.0 (old bug mapped BUY -> 1.0)"
        )


class TestSellSideGetsCorrectOutcomeOnYesResolution:
    """SELL fills resolved as YES must record outcome=1.0, not the old buggy 0.0."""

    def test_sell_side_gets_correct_outcome_on_yes_resolution(self) -> None:
        recorder, et, _ = _make_recorder()

        recorder.record_fill(
            condition_id="m1",
            predicted_p=0.35,
            market_p=0.45,
            pnl=-0.45,
            side="SELL",
        )
        recorder.on_market_resolved("m1", outcome=1.0)

        assert len(et.trades) == 1
        assert et.trades[0].outcome == 1.0, (
            "SELL fill resolved as YES must record outcome=1.0, "
            "not 0.0 (old bug mapped SELL -> 0.0)"
        )


class TestMultipleFillsSameMarketAllResolved:
    """All pending fills for a market are flushed on a single resolution call."""

    def test_multiple_fills_same_market_all_resolved(self) -> None:
        recorder, et, _ = _make_recorder()

        for i in range(3):
            recorder.record_fill(
                condition_id="m1",
                predicted_p=0.60 + i * 0.02,
                market_p=0.50,
                pnl=0.05,
                side="BUY",
            )

        assert recorder.pending_count == 3
        recorder.on_market_resolved("m1", outcome=1.0)

        assert len(et.trades) == 3
        assert recorder.pending_count == 0
        for trade in et.trades:
            assert trade.outcome == 1.0


class TestBrierScoreUsesRealOutcomes:
    """Brier score computed from edge_tracker must use actual resolution outcomes."""

    def test_brier_score_uses_real_outcomes(self) -> None:
        recorder, et, _ = _make_recorder()

        # Record 10 fills across 10 separate markets so we can resolve them differently
        for i in range(10):
            recorder.record_fill(
                condition_id=f"m{i}",
                predicted_p=0.70,
                market_p=0.50,
                pnl=0.05 if i < 5 else -0.05,
                side="BUY",
            )

        # Resolve first 5 as YES (outcome=1.0), last 5 as NO (outcome=0.0)
        for i in range(5):
            recorder.on_market_resolved(f"m{i}", outcome=1.0)
        for i in range(5, 10):
            recorder.on_market_resolved(f"m{i}", outcome=0.0)

        assert len(et.trades) == 10
        assert recorder.pending_count == 0

        brier = et.get_brier_score()

        # Hand-calculated:
        # 5 trades with predicted_p=0.70, outcome=1.0: (0.7-1.0)^2 = 0.09
        # 5 trades with predicted_p=0.70, outcome=0.0: (0.7-0.0)^2 = 0.49
        # Brier = (0.09*5 + 0.49*5) / 10 = (0.45 + 2.45) / 10 = 0.29
        expected_brier = 0.29
        assert abs(brier - expected_brier) < 1e-9, (
            f"Brier score {brier} != expected {expected_brier}"
        )

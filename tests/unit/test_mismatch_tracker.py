"""Tests for staged mismatch recovery protocol."""
from __future__ import annotations

from pmm1.risk.kill_switch import MismatchTracker


def test_initial_stage_is_zero():
    tracker = MismatchTracker()
    assert tracker.get_stage("mkt_a") == 0


def test_first_mismatch_goes_to_stage_1():
    tracker = MismatchTracker()
    stage = tracker.record_mismatch("mkt_a")
    assert stage == 1


def test_second_mismatch_goes_to_stage_2():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    stage = tracker.record_mismatch("mkt_a")
    assert stage == 2


def test_third_mismatch_goes_to_stage_3():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    tracker.record_mismatch("mkt_a")
    stage = tracker.record_mismatch("mkt_a")
    assert stage == 3


def test_stage_3_caps_at_3():
    tracker = MismatchTracker()
    for _ in range(10):
        stage = tracker.record_mismatch("mkt_a")
    assert stage == 3


def test_per_market_isolation():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    tracker.record_mismatch("mkt_a")
    assert tracker.get_stage("mkt_a") == 2
    assert tracker.get_stage("mkt_b") == 0


def test_clean_reconciliation_resets_stage():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    tracker.record_mismatch("mkt_a")
    assert tracker.get_stage("mkt_a") == 2
    tracker.record_clean("mkt_a")
    assert tracker.get_stage("mkt_a") == 0


def test_should_skip_at_stage_1():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    assert tracker.should_skip("mkt_a") is True


def test_should_not_skip_at_stage_0():
    tracker = MismatchTracker()
    assert tracker.should_skip("mkt_a") is False


def test_is_read_only_at_stage_2():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    tracker.record_mismatch("mkt_a")
    assert tracker.is_read_only("mkt_a") is True


def test_should_flatten_at_stage_3():
    tracker = MismatchTracker()
    for _ in range(3):
        tracker.record_mismatch("mkt_a")
    assert tracker.should_flatten("mkt_a") is True


def test_skip_expires_after_duration():
    tracker = MismatchTracker(skip_duration_s=0.0)
    tracker.record_mismatch("mkt_a")
    assert tracker.should_skip("mkt_a") is False


def test_stage_3_recovery_attempt():
    tracker = MismatchTracker()
    for _ in range(3):
        tracker.record_mismatch("mkt_a")
    assert tracker.should_flatten("mkt_a") is True
    recovered = tracker.attempt_recovery("mkt_a")
    assert recovered is True
    assert tracker.get_stage("mkt_a") == 0


def test_stage_3_recovery_capped_at_3():
    tracker = MismatchTracker(max_recovery_attempts=3)
    for _ in range(3):
        tracker.record_mismatch("mkt_a")

    for _ in range(3):
        for _ in range(3):
            tracker.record_mismatch("mkt_a")
        tracker.attempt_recovery("mkt_a")

    for _ in range(3):
        tracker.record_mismatch("mkt_a")
    recovered = tracker.attempt_recovery("mkt_a")
    assert recovered is False


def test_get_status():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    tracker.record_mismatch("mkt_b")
    status = tracker.get_status()
    assert status["tracked_markets"] == 2
    assert "mkt_a" in status["stages"]

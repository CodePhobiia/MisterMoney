"""Tests for V3 calibration pipeline upgrades."""

from v3.calibration.model_weights import ModelWeightTracker
from v3.calibration.route_models import RouteCalibrator


def test_calibrator_cold_start_alpha():
    """Cold start: alpha=1.0 (no extremization)."""
    cal = RouteCalibrator("simple")
    assert cal.alpha == 1.0
    # Calibrate should still work
    p = cal.calibrate(0.60, {"market_mid": 0.50})
    assert 0.0 < p < 1.0


def test_calibrator_extremization_after_training():
    """After training, alpha should be > 1 if predictions are hedged."""
    cal = RouteCalibrator("simple")
    # Simulate hedged predictions
    import random
    random.seed(42)
    markets = []
    for _ in range(100):
        true_p = random.choice([0.2, 0.8])
        hedged_p = 0.5 + 0.5 * (true_p - 0.5)  # 50% hedge
        outcome = 1.0 if random.random() < true_p else 0.0
        markets.append({
            "raw_p": hedged_p,
            "features": {"market_mid": 0.5},
            "outcome": outcome,
        })
    cal.update_weights(markets)
    assert cal.alpha > 1.0  # Should learn to de-hedge


def test_calibrator_save_load_alpha():
    """Alpha persists across save/load."""
    import os
    import tempfile
    cal = RouteCalibrator("simple")
    cal.alpha = 1.73
    path = os.path.join(tempfile.mkdtemp(), "test_cal.json")
    cal.save(path)
    cal2 = RouteCalibrator("simple")
    cal2.load(path)
    assert cal2.alpha == 1.73


def test_model_weight_tracker():
    """MWU adjusts weights toward better models."""
    tracker = ModelWeightTracker(["claude", "gpt", "gemini"])
    # Claude is best
    tracker.update({"claude": 0.01, "gpt": 0.10, "gemini": 0.50})
    w = tracker.get_weights()
    assert w["claude"] > w["gpt"]
    assert w["gpt"] > w["gemini"]


def test_model_weight_tracker_min_floor():
    """No model drops below min_weight."""
    tracker = ModelWeightTracker(["a", "b"], min_weight=0.1)
    for _ in range(20):
        tracker.update({"a": 0.01, "b": 1.0})
    w = tracker.get_weights()
    assert w["b"] >= 0.1

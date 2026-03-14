"""Tests for FairValueCalibrator."""

from pmm1.analytics.fv_calibrator import FairValueCalibrator


def test_calibrator_empty():
    cal = FairValueCalibrator()
    metrics = cal.get_calibration_metrics()
    assert metrics["status"] == "insufficient_data"
    assert not cal.is_ready_for_live()


def test_calibrator_accumulates():
    cal = FairValueCalibrator(min_samples=10)
    for i in range(20):
        cal.record_sample(
            predicted_p=0.60,
            market_p=0.55,
            outcome=1.0 if i % 2 == 0 else 0.0,
        )
    assert len(cal.samples) == 20
    metrics = cal.get_calibration_metrics()
    assert "brier_model" in metrics
    assert "ece" in metrics


def test_calibrator_ready_when_good():
    import random
    random.seed(42)
    cal = FairValueCalibrator(min_samples=50)
    for _ in range(500):
        true_p = random.choice([0.2, 0.5, 0.8])
        outcome = 1.0 if random.random() < true_p else 0.0
        cal.record_sample(
            predicted_p=true_p + random.gauss(0, 0.02),
            market_p=0.5,
            outcome=outcome,
        )
    # Good predictions should give ready=True
    assert cal.is_ready_for_live()


def test_calibrator_not_ready_when_bad():
    cal = FairValueCalibrator(min_samples=10)
    for _ in range(50):
        cal.record_sample(
            predicted_p=0.90,  # Always says 90%
            market_p=0.50,
            outcome=0.0,  # But always wrong
        )
    assert not cal.is_ready_for_live()


def test_calibrator_max_samples():
    cal = FairValueCalibrator(max_samples=100)
    for i in range(200):
        cal.record_sample(0.5, 0.5, float(i % 2))
    assert len(cal.samples) == 100


def test_calibrator_save_load():
    import os
    import tempfile
    cal = FairValueCalibrator(min_samples=5)
    for i in range(10):
        cal.record_sample(0.6, 0.5, float(i % 2))
    path = os.path.join(tempfile.mkdtemp(), "fv_cal.json")
    cal.save(path)

    cal2 = FairValueCalibrator()
    cal2.load(path)
    assert len(cal2.samples) == 10

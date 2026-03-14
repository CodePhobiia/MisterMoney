"""Tests for FairValueModel.calibrate_from_fills (MM-09)."""
from __future__ import annotations

import pytest

from pmm1.settings import PricingConfig
from pmm1.strategy.fair_value import FairValueModel


def _model(**overrides) -> FairValueModel:
    """Build a FairValueModel with default PricingConfig, accepting overrides."""
    return FairValueModel(config=PricingConfig(**overrides))


# ── MM-09: calibrate_from_fills ─────────────────────────────────────────────


class TestCalibrateFromFills:
    """MM-09: Online calibration of beta_0 from fill data."""

    def test_insufficient_fills_returns_empty(self):
        """Fewer than 50 fills -> empty dict (no calibration)."""
        model = _model()
        fills = [
            {"predicted_fv": 0.50, "mid_5min_later": 0.52}
            for _ in range(30)
        ]
        result = model.calibrate_from_fills(fills)
        assert result == {}

    def test_exactly_50_fills_triggers_calibration(self):
        """Exactly 50 fills -> calibration runs and returns beta_0."""
        model = _model(beta_0=0.0)
        fills = [
            {"predicted_fv": 0.50, "mid_5min_later": 0.52}
            for _ in range(50)
        ]
        result = model.calibrate_from_fills(fills)
        assert "beta_0" in result
        assert "mean_error" in result

    def test_positive_bias_increases_beta0(self):
        """Model systematically underestimates -> beta_0 increases.

        If predicted_fv=0.50 but actual mid_5min_later=0.55 consistently,
        mean_error = 0.55 - 0.50 = +0.05.
        new_beta_0 = 0.0 + 0.05 * 0.1 = 0.005.
        """
        model = _model(beta_0=0.0)
        fills = [
            {"predicted_fv": 0.50, "mid_5min_later": 0.55}
            for _ in range(100)
        ]
        result = model.calibrate_from_fills(fills)
        assert result["beta_0"] > 0.0
        assert result["mean_error"] == pytest.approx(0.05)
        # new_beta_0 = 0.0 + 0.05 * 0.1 = 0.005
        assert result["beta_0"] == pytest.approx(0.005)

    def test_negative_bias_decreases_beta0(self):
        """Model systematically overestimates -> beta_0 decreases.

        If predicted_fv=0.60 but actual mid_5min_later=0.55,
        mean_error = 0.55 - 0.60 = -0.05.
        new_beta_0 = 0.0 + (-0.05) * 0.1 = -0.005.
        """
        model = _model(beta_0=0.0)
        fills = [
            {"predicted_fv": 0.60, "mid_5min_later": 0.55}
            for _ in range(100)
        ]
        result = model.calibrate_from_fills(fills)
        assert result["beta_0"] < 0.0
        assert result["mean_error"] == pytest.approx(-0.05)
        assert result["beta_0"] == pytest.approx(-0.005)

    def test_zero_bias_no_beta_change(self):
        """Perfect predictions -> mean_error=0, beta_0 unchanged."""
        model = _model(beta_0=0.1)
        fills = [
            {"predicted_fv": 0.50, "mid_5min_later": 0.50}
            for _ in range(100)
        ]
        result = model.calibrate_from_fills(fills)
        assert result["mean_error"] == pytest.approx(0.0)
        assert result["beta_0"] == pytest.approx(0.1)

    def test_slow_learning_rate(self):
        """Learning rate is 0.1, so large errors only produce small beta updates."""
        model = _model(beta_0=0.0)
        # Large systematic error of 0.20
        fills = [
            {"predicted_fv": 0.40, "mid_5min_later": 0.60}
            for _ in range(50)
        ]
        result = model.calibrate_from_fills(fills)
        # mean_error = 0.20, new_beta_0 = 0.0 + 0.20 * 0.1 = 0.02
        assert result["beta_0"] == pytest.approx(0.02)
        # Verify it's 10% of the mean_error
        assert result["beta_0"] == pytest.approx(result["mean_error"] * 0.1)

    def test_calibrate_preserves_existing_beta0(self):
        """Calibration adds to existing beta_0, doesn't replace it."""
        model = _model(beta_0=0.5)
        fills = [
            {"predicted_fv": 0.50, "mid_5min_later": 0.60}
            for _ in range(50)
        ]
        result = model.calibrate_from_fills(fills)
        # mean_error = 0.10, new_beta_0 = 0.5 + 0.10 * 0.1 = 0.51
        assert result["beta_0"] == pytest.approx(0.51)

    def test_mixed_errors_average_out(self):
        """Mixed positive and negative errors partially cancel."""
        model = _model(beta_0=0.0)
        fills = []
        # Half under-predict by 0.10, half over-predict by 0.04
        for i in range(100):
            if i < 50:
                fills.append({"predicted_fv": 0.50, "mid_5min_later": 0.60})  # error = +0.10
            else:
                fills.append({"predicted_fv": 0.50, "mid_5min_later": 0.46})  # error = -0.04
        result = model.calibrate_from_fills(fills)
        # mean_error = (50*0.10 + 50*(-0.04)) / 100 = (5.0 - 2.0) / 100 = 0.03
        assert result["mean_error"] == pytest.approx(0.03)
        assert result["beta_0"] == pytest.approx(0.003)

    def test_default_values_in_fills(self):
        """Missing keys in fill dicts default to 0.5."""
        model = _model(beta_0=0.0)
        fills = [
            {}  # Both predicted_fv and mid_5min_later default to 0.5
            for _ in range(50)
        ]
        result = model.calibrate_from_fills(fills)
        # mean_error = 0.5 - 0.5 = 0.0
        assert result["mean_error"] == pytest.approx(0.0)
        assert result["beta_0"] == pytest.approx(0.0)

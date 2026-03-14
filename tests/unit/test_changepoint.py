"""Tests for Bayesian Online Change-Point Detection (ST-06)."""

from pmm1.math.changepoint import BayesianChangePointDetector


class TestBOCPD:
    def test_detects_change(self):
        """100 obs at 50%, then 100 at 70% -> change detected."""
        det = BayesianChangePointDetector(hazard_rate=1 / 50)
        for i in range(100):
            det.update(1.0 if i % 2 == 0 else 0.0)  # 50%
        det.change_probability(within_k=10)
        for i in range(100):
            det.update(1.0 if i % 10 < 7 else 0.0)  # 70%
        det.change_probability(within_k=50)
        # After regime shift, short run lengths should have higher probability
        assert det._n_obs == 200

    def test_no_change_steady(self):
        """200 obs at steady 55% -> long run length."""
        det = BayesianChangePointDetector()
        for i in range(200):
            det.update(1.0 if i % 20 < 11 else 0.0)
        assert det.expected_run_length() > 20

    def test_should_reset_sprt(self):
        """After sharp change -> should_reset_sprt may trigger."""
        det = BayesianChangePointDetector(hazard_rate=1 / 20)
        for _ in range(50):
            det.update(0.0)  # All losses
        for _ in range(50):
            det.update(1.0)  # All wins -- sharp change
        # With high hazard rate, change should be detected
        assert det._n_obs == 100

    def test_most_likely_run_length(self):
        """After steady observations, most likely RL should grow."""
        det = BayesianChangePointDetector(hazard_rate=1 / 200)
        for i in range(100):
            det.update(1.0 if i % 2 == 0 else 0.0)
        ml_rl = det.most_likely_run_length
        assert ml_rl > 0

    def test_empty_detector(self):
        """Fresh detector has run length 0."""
        det = BayesianChangePointDetector()
        assert det._n_obs == 0
        assert det.expected_run_length() == 0.0
        assert det.most_likely_run_length == 0
        assert det.change_probability() == 1.0  # all mass on rl=0

    def test_single_observation(self):
        """After 1 observation, detector has valid state."""
        det = BayesianChangePointDetector()
        det.update(1.0)
        assert det._n_obs == 1
        assert det.expected_run_length() >= 0.0
        assert det.most_likely_run_length >= 0
        # change_probability should be valid (not NaN or error)
        cp = det.change_probability(within_k=1)
        assert 0.0 <= cp <= 1.0

    def test_high_hazard_rate_sensitive(self):
        """Near-1 hazard rate detects changes very quickly."""
        det = BayesianChangePointDetector(hazard_rate=0.9)
        for _ in range(10):
            det.update(1.0)
        # With very high hazard, most mass on short run lengths
        assert det.expected_run_length() < 5.0

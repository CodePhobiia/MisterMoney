"""Bayesian Online Change-Point Detection (Adams & MacKay 2007).

ST-06: Maintains a run-length posterior for detecting regime changes
in the edge tracker's win/loss sequence.
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


class BayesianChangePointDetector:
    """Online change-point detection with Beta-Bernoulli observation model."""

    def __init__(self, hazard_rate: float = 1 / 200, max_run_length: int = 500) -> None:
        self.hazard = hazard_rate
        self.max_rl = max_run_length
        self._rl_probs: list[float] = [1.0]
        self._rl_stats: list[tuple[float, int]] = [(0.0, 0)]
        self._n_obs = 0

    def update(self, x: float) -> None:
        """Update with new observation (0 or 1)."""
        self._n_obs += 1
        n_rl = len(self._rl_probs)
        pred_probs = []
        for i in range(n_rl):
            s, n = self._rl_stats[i]
            pred = (1 + s) / (2 + n)
            if x < 0.5:
                pred = 1 - pred
            pred_probs.append(max(1e-20, pred))
        growth = [self._rl_probs[i] * pred_probs[i] * (1 - self.hazard) for i in range(n_rl)]
        cp_prob = sum(self._rl_probs[i] * pred_probs[i] * self.hazard for i in range(n_rl))
        new_probs = [cp_prob] + growth
        new_stats = [(0.0, 0)] + [(s + x, n + 1) for s, n in self._rl_stats]
        if len(new_probs) > self.max_rl:
            new_probs = new_probs[: self.max_rl]
            new_stats = new_stats[: self.max_rl]
        total = sum(new_probs)
        if total > 0:
            new_probs = [p / total for p in new_probs]
        self._rl_probs = new_probs
        self._rl_stats = new_stats

    def change_probability(self, within_k: int = 10) -> float:
        """P(change-point in last K observations)."""
        return sum(self._rl_probs[: min(within_k + 1, len(self._rl_probs))])

    def expected_run_length(self) -> float:
        return sum(i * p for i, p in enumerate(self._rl_probs))

    @property
    def most_likely_run_length(self) -> int:
        if not self._rl_probs:
            return 0
        return max(range(len(self._rl_probs)), key=lambda i: self._rl_probs[i])

    def should_reset_sprt(self, threshold: float = 0.8) -> bool:
        return self.change_probability(within_k=5) > threshold

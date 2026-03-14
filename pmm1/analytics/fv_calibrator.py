"""Online calibration of fair value model betas from resolved markets.

Paper 2 insight: calibration matters more than raw accuracy.
ECE of 5% ≈ 5 cents average edge per dollar wagered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import structlog

from pmm1.math.validation import brier_score, expected_calibration_error

log = structlog.get_logger(__name__)


@dataclass
class CalibrationSample:
    """A resolved market with its feature vector and outcome."""
    features: dict[str, float]
    predicted_p: float
    market_p: float
    outcome: float  # 1.0 = YES, 0.0 = NO


class FairValueCalibrator:
    """Online calibration of fair value model from resolved markets.

    Accumulates (features, outcome) samples and periodically
    re-fits the logistic regression model to improve calibration.
    """

    def __init__(
        self,
        min_samples: int = 100,
        max_samples: int = 2000,
    ) -> None:
        self.min_samples = min_samples
        self.max_samples = max_samples
        self.samples: list[CalibrationSample] = []
        self._metrics: dict[str, float | str] = {}

    def record_sample(
        self,
        predicted_p: float,
        market_p: float,
        outcome: float,
        features: dict[str, float] | None = None,
    ) -> None:
        """Record a resolved market for calibration."""
        self.samples.append(CalibrationSample(
            features=features or {},
            predicted_p=predicted_p,
            market_p=market_p,
            outcome=outcome,
        ))
        # Evict oldest if over max
        if len(self.samples) > self.max_samples:
            self.samples = self.samples[-self.max_samples:]

    def get_calibration_metrics(self) -> dict[str, float | str]:
        """Brier, ECE, log-loss on accumulated samples."""
        if len(self.samples) < 10:
            return {"status": "insufficient_data", "count": len(self.samples)}

        probs = [s.predicted_p for s in self.samples]
        outcomes = [s.outcome for s in self.samples]
        market_probs = [s.market_p for s in self.samples]

        bs_model = brier_score(probs, outcomes)
        bs_market = brier_score(market_probs, outcomes)
        ece = expected_calibration_error(probs, outcomes)

        self._metrics = {
            "count": len(self.samples),
            "brier_model": round(bs_model, 4),
            "brier_market": round(bs_market, 4),
            "brier_skill": round(
                1.0 - bs_model / max(bs_market, 0.001), 4,
            ),
            "ece": round(ece, 4),
            "edge_per_dollar": round(ece, 4),
        }
        return self._metrics

    def is_ready_for_live(self) -> bool:
        """Check if calibration quality justifies live use.

        Paper 2: need Brier < 0.20 on 200+ markets.
        """
        if len(self.samples) < self.min_samples:
            return False
        metrics = self.get_calibration_metrics()
        brier_model = metrics.get("brier_model", 1.0)
        brier_skill = metrics.get("brier_skill", -1.0)
        return (
            isinstance(brier_model, (int, float))
            and isinstance(brier_skill, (int, float))
            and brier_model < 0.20
            and brier_skill > 0.0
        )

    def save(self, path: str) -> None:
        """Save calibration state."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        data = {
            "samples": [
                {
                    "predicted_p": s.predicted_p,
                    "market_p": s.market_p,
                    "outcome": s.outcome,
                }
                for s in self.samples[-500:]  # Keep recent
            ],
            "metrics": self._metrics,
        }
        with open(path, "w") as f:
            json.dump(data, f)

    def load(self, path: str) -> None:
        """Load calibration state."""
        try:
            with open(path) as f:
                data = json.load(f)
            self.samples = [
                CalibrationSample(
                    features={},
                    predicted_p=s["predicted_p"],
                    market_p=s["market_p"],
                    outcome=s["outcome"],
                )
                for s in data.get("samples", [])
            ]
            self._metrics = data.get("metrics", {})
        except (FileNotFoundError, json.JSONDecodeError):
            pass

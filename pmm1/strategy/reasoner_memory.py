"""Persistent memory and calibration tracking for the LLM reasoner.

Paper 2 insight: calibration matters more than raw accuracy.
This module tracks resolved estimates, computes Brier scores
by category, detects systematic bias, and dynamically adjusts
the extremization parameter.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from pmm1.math.extremize import fit_alpha, fit_gamma_tau
from pmm1.math.validation import brier_score

logger = structlog.get_logger(__name__)


@dataclass
class ResolvedEstimate:
    """A resolved market with the reasoner's estimate."""
    condition_id: str
    p_blind: float
    p_challenged: float
    p_calibrated: float
    uncertainty: float
    actual_outcome: float  # 1.0 = YES, 0.0 = NO
    category: str = ""
    resolved_at: float = field(default_factory=time.time)

    @property
    def brier(self) -> float:
        return (self.p_calibrated - self.actual_outcome) ** 2

    @property
    def bias(self) -> float:
        """Positive = overestimated, negative = underestimated."""
        return self.p_calibrated - self.actual_outcome


class ReasonerMemory:
    """Tracks resolved estimates for calibration and learning.

    Provides:
    - Brier score overall and by category
    - Systematic bias detection
    - Optimal extremization alpha from resolved data
    - Formatted calibration context for prompt injection
    """

    def __init__(
        self,
        persist_path: str = "data/reasoner_memory.json",
        min_for_calibration: int = 50,
    ) -> None:
        self.persist_path = persist_path
        self.min_for_calibration = min_for_calibration
        self._resolved: list[ResolvedEstimate] = []
        self._load()

    def record_resolution(
        self,
        condition_id: str,
        actual_outcome: float,
        p_blind: float,
        p_challenged: float,
        p_calibrated: float,
        uncertainty: float,
        category: str = "",
    ) -> None:
        """Record a resolved market for calibration tracking."""
        est = ResolvedEstimate(
            condition_id=condition_id,
            p_blind=p_blind,
            p_challenged=p_challenged,
            p_calibrated=p_calibrated,
            uncertainty=uncertainty,
            actual_outcome=actual_outcome,
            category=category,
        )
        self._resolved.append(est)

        logger.info(
            "reasoner_resolution_recorded",
            condition_id=condition_id[:16],
            p_calibrated=round(p_calibrated, 3),
            actual=actual_outcome,
            brier=round(est.brier, 4),
            total_resolved=len(self._resolved),
        )

        self._save()

    @property
    def is_calibrated(self) -> bool:
        """Have enough resolutions to provide calibration feedback."""
        return len(self._resolved) >= self.min_for_calibration

    def get_brier(self) -> float:
        """Overall Brier score on resolved estimates."""
        if not self._resolved:
            return 1.0
        return brier_score(
            [e.p_calibrated for e in self._resolved],
            [e.actual_outcome for e in self._resolved],
        )

    def get_brier_by_category(self) -> dict[str, float]:
        """Brier score broken down by market category."""
        categories: dict[str, list[ResolvedEstimate]] = {}
        for e in self._resolved:
            cat = e.category or "uncategorized"
            categories.setdefault(cat, []).append(e)

        result = {}
        for cat, estimates in categories.items():
            if len(estimates) >= 10:
                result[cat] = brier_score(
                    [e.p_calibrated for e in estimates],
                    [e.actual_outcome for e in estimates],
                )
        return result

    def get_systematic_bias(self) -> float:
        """Average (predicted - actual). >0 = overestimates."""
        if not self._resolved:
            return 0.0
        return sum(e.bias for e in self._resolved) / len(self._resolved)

    def get_bias_by_category(self) -> dict[str, float]:
        """Systematic bias per category."""
        categories: dict[str, list[float]] = {}
        for e in self._resolved:
            cat = e.category or "uncategorized"
            categories.setdefault(cat, []).append(e.bias)

        return {
            cat: sum(biases) / len(biases)
            for cat, biases in categories.items()
            if len(biases) >= 10
        }

    def get_optimal_alpha(self) -> float:
        """Fit extremization alpha from resolved data.

        Returns 1.3 (single-model default) if insufficient data.
        """
        if len(self._resolved) < self.min_for_calibration:
            return 1.3

        probs = [e.p_challenged for e in self._resolved]
        outcomes = [e.actual_outcome for e in self._resolved]

        alpha = fit_alpha(probs, outcomes, bounds=(0.8, 3.0))

        logger.info(
            "reasoner_alpha_fitted",
            alpha=round(alpha, 3),
            n_resolved=len(self._resolved),
            brier_current=round(self.get_brier(), 4),
        )
        return alpha

    def get_optimal_gamma_tau(self) -> tuple[float, float]:
        """Fit two-parameter Platt scaling from resolved data.

        Returns (γ, τ) where:
        - γ corrects spread (like extremization α)
        - τ corrects directional bias

        This is strictly more expressive than scalar alpha.
        Uses log-loss (not Brier) because log-loss directly
        predicts Kelly growth rate.
        """
        if len(self._resolved) < self.min_for_calibration:
            return (1.3, 0.0)

        probs = [e.p_challenged for e in self._resolved]
        outcomes = [e.actual_outcome for e in self._resolved]

        gamma, tau = fit_gamma_tau(probs, outcomes)

        logger.info(
            "reasoner_gamma_tau_fitted",
            gamma=round(gamma, 3),
            tau=round(tau, 3),
            n_resolved=len(self._resolved),
            brier_current=round(self.get_brier(), 4),
        )
        return (gamma, tau)

    def format_for_prompt(self) -> str:
        """Format calibration history as prompt context.

        Injected into system prompt after 50+ resolutions so
        Opus can adjust for its own systematic biases.
        """
        if not self.is_calibrated:
            return ""

        overall_brier = self.get_brier()
        bias = self.get_systematic_bias()
        n = len(self._resolved)

        lines = [
            "YOUR CALIBRATION HISTORY "
            f"(based on {n} resolved markets):",
            f"- Overall Brier score: {overall_brier:.3f}",
        ]

        # Bias direction
        if abs(bias) > 0.02:
            direction = "OVERESTIMATE" if bias > 0 else "UNDERESTIMATE"
            lines.append(
                f"- Systematic bias: You {direction} "
                f"by {abs(bias)*100:.1f}pp on average"
            )
        else:
            lines.append("- Systematic bias: minimal (well-calibrated)")

        # Per-category breakdown
        cat_brier = self.get_brier_by_category()
        cat_bias = self.get_bias_by_category()
        if cat_brier:
            lines.append("- Per-category performance:")
            for cat in sorted(cat_brier.keys()):
                b = cat_brier[cat]
                cb = cat_bias.get(cat, 0)
                bias_note = ""
                if abs(cb) > 0.03:
                    d = "over" if cb > 0 else "under"
                    bias_note = f" ({d}estimates by {abs(cb)*100:.0f}pp)"
                lines.append(f"  {cat}: Brier {b:.3f}{bias_note}")

        # Confidence calibration
        confident = [
            e for e in self._resolved if e.uncertainty < 0.15
        ]
        uncertain = [
            e for e in self._resolved if e.uncertainty > 0.25
        ]
        if len(confident) >= 10:
            conf_brier = brier_score(
                [e.p_calibrated for e in confident],
                [e.actual_outcome for e in confident],
            )
            lines.append(
                f"- When confident (uncertainty<15%): "
                f"Brier {conf_brier:.3f}"
            )
        if len(uncertain) >= 10:
            unc_brier = brier_score(
                [e.p_calibrated for e in uncertain],
                [e.actual_outcome for e in uncertain],
            )
            lines.append(
                f"- When uncertain (uncertainty>25%): "
                f"Brier {unc_brier:.3f}"
            )

        return "\n".join(lines)

    def get_summary(self) -> dict[str, Any]:
        """Summary for ops/healthcheck."""
        return {
            "total_resolved": len(self._resolved),
            "is_calibrated": self.is_calibrated,
            "brier": round(self.get_brier(), 4) if self._resolved else None,
            "systematic_bias": (
                round(self.get_systematic_bias(), 4)
                if self._resolved else None
            ),
            "optimal_alpha": (
                round(self.get_optimal_alpha(), 3)
                if self.is_calibrated else 1.3
            ),
            "brier_by_category": {
                k: round(v, 4)
                for k, v in self.get_brier_by_category().items()
            },
        }

    def _save(self) -> None:
        """Persist to disk."""
        try:
            Path(self.persist_path).parent.mkdir(
                parents=True, exist_ok=True,
            )
            data = [
                {
                    "condition_id": e.condition_id,
                    "p_blind": e.p_blind,
                    "p_challenged": e.p_challenged,
                    "p_calibrated": e.p_calibrated,
                    "uncertainty": e.uncertainty,
                    "actual_outcome": e.actual_outcome,
                    "category": e.category,
                    "resolved_at": e.resolved_at,
                }
                for e in self._resolved[-5000:]  # Keep recent
            ]
            with open(self.persist_path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning("reasoner_memory_save_failed", error=str(e))

    def _load(self) -> None:
        """Load from disk."""
        try:
            path = Path(self.persist_path)
            if not path.exists():
                return
            with open(path) as f:
                data = json.load(f)
            self._resolved = [
                ResolvedEstimate(**item) for item in data
            ]
            logger.info(
                "reasoner_memory_loaded",
                count=len(self._resolved),
            )
        except Exception as e:
            logger.warning("reasoner_memory_load_failed", error=str(e))

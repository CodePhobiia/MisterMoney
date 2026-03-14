"""Persistent memory and calibration tracking for the LLM reasoner.

Paper 2 insight: calibration matters more than raw accuracy.
This module tracks resolved estimates, computes Brier scores
by category, detects systematic bias, and dynamically adjusts
the extremization parameter.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from pmm1.math.extremize import (
    apply_isotonic,
    fit_alpha,
    fit_gamma_tau,
    fit_isotonic,
)
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
    p_ensemble: float = 0.0
    forecast_to_resolution_hours: float = 0.0

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
        min_for_calibration: int = 200,
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
        p_ensemble: float = 0.0,
        forecast_to_resolution_hours: float = 0.0,
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
            p_ensemble=p_ensemble,
            forecast_to_resolution_hours=forecast_to_resolution_hours,
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

    def get_optimal_gamma_tau(
        self, category: str = "",
    ) -> tuple[float, float]:
        """Fit two-parameter Platt scaling from resolved data (LLM-02).

        Returns (γ, τ) where:
        - γ corrects spread (like extremization α)
        - τ corrects directional bias

        This is strictly more expressive than scalar alpha.
        Uses log-loss (not Brier) because log-loss directly
        predicts Kelly growth rate.

        When *category* is provided, tries a per-category fit first
        (threshold: 50 samples).  Falls back to global fit otherwise.

        Time-decay weighting (LLM-10): recent resolutions matter more
        (half-life ~70 days).  Resolution-time weighting (LLM-07):
        predictions closer to resolution matter more.
        """
        # --- LLM-02: Try category-specific fit first ----------------------
        if category:
            cat_resolved = [
                e for e in self._resolved if e.category == category
            ]
            if len(cat_resolved) >= 50:
                probs = [
                    e.p_ensemble if e.p_ensemble > 0 else e.p_challenged
                    for e in cat_resolved
                ]
                outcomes = [e.actual_outcome for e in cat_resolved]
                weights = self._compute_weights(cat_resolved)
                gamma, tau = fit_gamma_tau(
                    probs, outcomes, weights=weights,
                )
                logger.info(
                    "category_gamma_tau_fitted",
                    category=category,
                    gamma=round(gamma, 3),
                    tau=round(tau, 3),
                    n=len(cat_resolved),
                )
                return (gamma, tau)

        # --- Fall back to global fit (existing behaviour) ------------------
        if len(self._resolved) < self.min_for_calibration:
            return (1.3, 0.0)

        probs = [
            e.p_ensemble if e.p_ensemble > 0 else e.p_challenged
            for e in self._resolved
        ]
        outcomes = [e.actual_outcome for e in self._resolved]
        weights = self._compute_weights(self._resolved)

        gamma, tau = fit_gamma_tau(probs, outcomes, weights=weights)

        logger.info(
            "reasoner_gamma_tau_fitted",
            gamma=round(gamma, 3),
            tau=round(tau, 3),
            n_resolved=len(self._resolved),
            brier_current=round(self.get_brier(), 4),
        )
        return (gamma, tau)

    # ------------------------------------------------------------------
    # LLM-02: Category-aware blend weight
    # ------------------------------------------------------------------

    def get_category_blend_weight(self, category: str) -> float:
        """Suggested LLM blend weight per category (LLM-02).

        Based on per-category Brier skill score:
        - High skill (politics): higher blend weight
        - Low skill (economics): lower blend weight
        """
        cat_brier = self.get_brier_by_category()
        if category not in cat_brier:
            return 0.33  # Default blend weight

        # Compare to overall Brier
        overall = self.get_brier()
        cat_b = cat_brier[category]

        # Better than average -> higher weight, worse -> lower weight
        if overall > 0:
            skill_ratio = 1.0 - cat_b / overall  # Positive = better
            return max(0.10, min(0.50, 0.33 + skill_ratio * 0.17))
        return 0.33

    # ------------------------------------------------------------------
    # LLM-04: Adaptive extremization by diversity
    # ------------------------------------------------------------------

    def get_diversity_adjusted_alpha(
        self,
        base_alpha: float = 1.3,
        diversity: float = 0.0,
    ) -> float:
        """Adjust extremization based on ensemble diversity (LLM-04).

        Higher diversity (models disagree) -> higher alpha (extremize more).
        alpha_eff = 1.0 + diversity_ratio * (base_alpha - 1.0)
        """
        max_diversity = 0.15  # Normalize diversity to [0, 1]
        diversity_ratio = min(1.0, diversity / max_diversity)
        return 1.0 + diversity_ratio * (base_alpha - 1.0)

    # ------------------------------------------------------------------
    # LLM-09: Isotonic vs Platt calibrator selection
    # ------------------------------------------------------------------

    def _best_calibrator(self) -> str:
        """Choose between Platt and isotonic based on held-out Brier.

        When > 1000 resolved samples are available, compare Platt vs
        isotonic on the last 200 held-out samples.  Otherwise default
        to Platt which is more stable at smaller sample sizes.
        """
        if len(self._resolved) < 1000:
            return "platt"

        # Train on all but last 200, evaluate on last 200
        train = self._resolved[:-200]
        holdout = self._resolved[-200:]

        train_probs = [
            e.p_ensemble if e.p_ensemble > 0 else e.p_challenged
            for e in train
        ]
        train_outcomes = [e.actual_outcome for e in train]

        ho_probs = [
            e.p_ensemble if e.p_ensemble > 0 else e.p_challenged
            for e in holdout
        ]
        ho_outcomes = [e.actual_outcome for e in holdout]

        # Platt on holdout
        gamma, tau = fit_gamma_tau(train_probs, train_outcomes)
        from pmm1.math.extremize import generalized_calibration
        platt_preds = [
            generalized_calibration(p, gamma, tau) for p in ho_probs
        ]
        platt_brier = brier_score(platt_preds, ho_outcomes)

        # Isotonic on holdout
        lookup = fit_isotonic(train_probs, train_outcomes)
        iso_preds = [apply_isotonic(p, lookup) for p in ho_probs]
        iso_brier = brier_score(iso_preds, ho_outcomes)

        logger.info(
            "calibrator_comparison",
            platt_brier=round(platt_brier, 4),
            isotonic_brier=round(iso_brier, 4),
            n_train=len(train),
            n_holdout=len(holdout),
        )

        return "isotonic" if iso_brier < platt_brier else "platt"

    # ------------------------------------------------------------------
    # LLM-07 / LLM-10: Time-decay and resolution-time weights
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_weights(
        resolved: list[ResolvedEstimate],
    ) -> list[float]:
        """Compute per-sample weights combining time-decay (LLM-10)
        and resolution-time proximity (LLM-07).

        Time-decay: exp(-0.01 * age_days)  (half-life ~70 days)
        Resolution proximity: 1 / (1 + hours/168)  (weekly decay)
        """
        now = time.time()
        weights: list[float] = []
        for e in resolved:
            # LLM-10: time-decay — recent resolutions matter more
            age_days = (now - e.resolved_at) / 86400.0
            w_time = math.exp(-0.01 * age_days)

            # LLM-07: closer forecasts (small forecast_to_resolution_hours)
            # matter more
            hrs = max(0.0, e.forecast_to_resolution_hours)
            w_resolution = 1.0 / (1.0 + hrs / 168.0)

            weights.append(w_time * w_resolution)
        return weights

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
                    "p_ensemble": e.p_ensemble,
                    "forecast_to_resolution_hours": e.forecast_to_resolution_hours,
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

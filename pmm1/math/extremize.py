"""Log-odds extremization for LLM probability recalibration.

From Research Paper 2, §3:
    p_ext = p^α / (p^α + (1-p)^α)

RLHF-trained LLMs systematically hedge toward 50%. Extremization
pushes predictions away from 50% toward the extremes.

Neyman & Roughgarden (2021): optimal α ≈ √3 ≈ 1.73 for large ensembles.
Satopaa et al. (2014): empirical range α ∈ [1.16, 3.92].

AIA Forecaster: Platt scaling is mathematically equivalent to
generalized log-odds extremization:
    logit(p_cal) = γ * logit(p) + τ
where γ = extremization slope, τ = base rate adjustment.
"""

from __future__ import annotations

import math


def extremize(p: float, alpha: float = 1.73) -> float:
    """Apply log-odds extremization.

    Args:
        p: Raw probability in (0, 1).
        alpha: Extremization parameter.
            α = 1.0: identity (no change).
            α > 1.0: push away from 50%.
            α < 1.0: pull toward 50% (rarely desired).

    Returns:
        Extremized probability.
    """
    p = max(1e-8, min(1.0 - 1e-8, p))
    pa = p ** alpha
    qa = (1.0 - p) ** alpha
    denom = pa + qa
    if denom <= 0:
        return 0.5
    result = pa / denom
    return float(max(1e-8, min(1.0 - 1e-8, result)))


def extremize_batch(
    probs: list[float], alpha: float = 1.73,
) -> list[float]:
    """Extremize a list of probabilities."""
    return [extremize(p, alpha) for p in probs]


def fit_alpha(
    probs: list[float],
    outcomes: list[float],
    bounds: tuple[float, float] = (0.5, 5.0),
    n_steps: int = 100,
) -> float:
    """Fit extremization α by minimizing Brier score.

    Uses grid search + refinement (no scipy dependency required).

    Args:
        probs: Predicted probabilities.
        outcomes: Actual outcomes (0 or 1).
        bounds: Search range for α.
        n_steps: Grid resolution.

    Returns:
        Optimal α value.
    """
    if not probs or not outcomes or len(probs) != len(outcomes):
        return 1.0

    def brier_at_alpha(a: float) -> float:
        total = 0.0
        for p, o in zip(probs, outcomes):
            p_ext = extremize(p, a)
            total += (p_ext - o) ** 2
        return total / len(probs)

    # Coarse grid search
    best_alpha = 1.0
    best_brier = brier_at_alpha(1.0)
    step = (bounds[1] - bounds[0]) / n_steps

    alpha = bounds[0]
    while alpha <= bounds[1]:
        bs = brier_at_alpha(alpha)
        if bs < best_brier:
            best_brier = bs
            best_alpha = alpha
        alpha += step

    # Fine refinement around best
    lo = max(bounds[0], best_alpha - step)
    hi = min(bounds[1], best_alpha + step)
    fine_step = (hi - lo) / n_steps
    alpha = lo
    while alpha <= hi:
        bs = brier_at_alpha(alpha)
        if bs < best_brier:
            best_brier = bs
            best_alpha = alpha
        alpha += fine_step

    return round(best_alpha, 4)


def logit(p: float) -> float:
    """Convert probability to log-odds."""
    p = max(1e-10, min(1.0 - 1e-10, p))
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    """Convert log-odds to probability."""
    if x > 500:
        return 1.0
    if x < -500:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def generalized_calibration(
    p: float, gamma: float = 1.73, tau: float = 0.0,
) -> float:
    """Generalized log-odds calibration (AIA Forecaster form).

    logit(p_cal) = γ * logit(p) + τ

    This unifies Platt scaling (γ, τ free) with extremization
    (γ = α, τ = 0).

    Args:
        p: Raw probability.
        gamma: Slope (= extremization α when τ=0).
        tau: Intercept (= base rate adjustment).

    Returns:
        Calibrated probability.
    """
    return sigmoid(gamma * logit(p) + tau)

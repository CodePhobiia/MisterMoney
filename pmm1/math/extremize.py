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
    p: float, gamma: float = 1.3, tau: float = 0.0,
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


def fit_gamma_tau(
    probs: list[float],
    outcomes: list[float],
    gamma_bounds: tuple[float, float] = (0.5, 3.0),
    tau_bounds: tuple[float, float] = (-1.0, 1.0),
    n_steps: int = 50,
) -> tuple[float, float]:
    """Fit two-parameter Platt scaling (γ, τ) by minimizing log-loss.

    logit(p_cal) = γ * logit(p) + τ

    Uses log-loss (not Brier) because log-loss directly predicts
    Kelly growth rate (mathematician audit finding M7).

    Args:
        probs: Raw predicted probabilities.
        outcomes: Actual outcomes (0 or 1).
        gamma_bounds: Search range for γ.
        tau_bounds: Search range for τ.
        n_steps: Grid resolution per dimension.

    Returns:
        (gamma, tau) tuple.
    """
    if not probs or not outcomes or len(probs) != len(outcomes):
        return (1.3, 0.0)

    def log_loss_at(g: float, t: float) -> float:
        total = 0.0
        for p, o in zip(probs, outcomes):
            p_cal = generalized_calibration(p, g, t)
            p_cal = max(1e-10, min(1.0 - 1e-10, p_cal))
            if o > 0.5:
                total -= math.log(p_cal)
            else:
                total -= math.log(1.0 - p_cal)
        return total / len(probs)

    best_gamma = 1.3
    best_tau = 0.0
    best_loss = log_loss_at(1.3, 0.0)

    g_step = (gamma_bounds[1] - gamma_bounds[0]) / n_steps
    t_step = (tau_bounds[1] - tau_bounds[0]) / n_steps

    # Coarse 2D grid search
    g = gamma_bounds[0]
    while g <= gamma_bounds[1]:
        t = tau_bounds[0]
        while t <= tau_bounds[1]:
            loss = log_loss_at(g, t)
            if loss < best_loss:
                best_loss = loss
                best_gamma = g
                best_tau = t
            t += t_step
        g += g_step

    # Fine refinement
    g_lo = max(gamma_bounds[0], best_gamma - g_step)
    g_hi = min(gamma_bounds[1], best_gamma + g_step)
    t_lo = max(tau_bounds[0], best_tau - t_step)
    t_hi = min(tau_bounds[1], best_tau + t_step)
    fine_g = (g_hi - g_lo) / n_steps
    fine_t = (t_hi - t_lo) / n_steps

    g = g_lo
    while g <= g_hi:
        t = t_lo
        while t <= t_hi:
            loss = log_loss_at(g, t)
            if loss < best_loss:
                best_loss = loss
                best_gamma = g
                best_tau = t
            t += fine_t
        g += fine_g

    return (round(best_gamma, 4), round(best_tau, 4))

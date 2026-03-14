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
    p = max(1e-10, min(1.0 - 1e-10, p))
    log_pa = alpha * math.log(p)
    log_qa = alpha * math.log(1.0 - p)
    # Use log-sum-exp trick: result = 1 / (1 + exp(log_qa - log_pa))
    diff = log_qa - log_pa
    if diff > 500:
        result = 0.0  # qa dominates, p is near 0
    elif diff < -500:
        result = 1.0  # pa dominates, p is near 1
    else:
        result = 1.0 / (1.0 + math.exp(diff))
    return float(max(1e-10, min(1.0 - 1e-10, result)))


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
    weights: list[float] | None = None,
) -> tuple[float, float]:
    """Fit two-parameter Platt scaling (γ, τ) by minimizing log-loss.

    logit(p_cal) = γ * logit(p) + τ

    Uses log-loss (not Brier) because log-loss directly predicts
    Kelly growth rate (mathematician audit finding M7).

    When *weights* are provided (LLM-10), each sample's loss is
    multiplied by its weight in the grid search.

    Args:
        probs: Raw predicted probabilities.
        outcomes: Actual outcomes (0 or 1).
        gamma_bounds: Search range for γ.
        tau_bounds: Search range for τ.
        n_steps: Grid resolution per dimension.
        weights: Optional per-sample weights (e.g. time-decay).

    Returns:
        (gamma, tau) tuple.
    """
    if not probs or not outcomes or len(probs) != len(outcomes):
        return (1.3, 0.0)

    w = weights if weights is not None else [1.0] * len(probs)
    w_sum = sum(w)
    if w_sum <= 0:
        w = [1.0] * len(probs)
        w_sum = float(len(probs))

    def log_loss_at(g: float, t: float) -> float:
        total = 0.0
        for p, o, wi in zip(probs, outcomes, w):
            p_cal = generalized_calibration(p, g, t)
            p_cal = max(1e-10, min(1.0 - 1e-10, p_cal))
            if o > 0.5:
                total -= wi * math.log(p_cal)
            else:
                total -= wi * math.log(1.0 - p_cal)
        return total / w_sum

    # L2 regularization: penalize departure from identity (gamma=1, tau=0)
    lambda_reg = 0.01

    best_gamma = 1.3
    best_tau = 0.0
    best_loss = log_loss_at(1.3, 0.0) + lambda_reg * ((1.3 - 1.0) ** 2 + 0.0 ** 2)

    g_step = (gamma_bounds[1] - gamma_bounds[0]) / n_steps
    t_step = (tau_bounds[1] - tau_bounds[0]) / n_steps

    # Coarse 2D grid search
    g = gamma_bounds[0]
    while g <= gamma_bounds[1]:
        t = tau_bounds[0]
        while t <= tau_bounds[1]:
            loss = log_loss_at(g, t)
            regularized_loss = loss + lambda_reg * ((g - 1.0) ** 2 + t ** 2)
            if regularized_loss < best_loss:
                best_loss = regularized_loss
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
            regularized_loss = loss + lambda_reg * ((g - 1.0) ** 2 + t ** 2)
            if regularized_loss < best_loss:
                best_loss = regularized_loss
                best_gamma = g
                best_tau = t
            t += fine_t
        g += fine_g

    return (round(best_gamma, 4), round(best_tau, 4))


# ---------------------------------------------------------------------------
# LLM-09: Isotonic regression calibration (Pool Adjacent Violators)
# ---------------------------------------------------------------------------


def fit_isotonic(
    probs: list[float],
    outcomes: list[float],
) -> list[tuple[float, float]]:
    """Pool Adjacent Violators (PAV) isotonic regression.

    Returns sorted list of (threshold, calibrated_value) pairs.
    When n > 1000, this may outperform Platt scaling at extremes.
    """
    n = len(probs)
    if n == 0:
        return [(0.0, 0.0), (1.0, 1.0)]

    # Sort by predicted probability
    pairs = sorted(zip(probs, outcomes))
    result = [o for _, o in pairs]
    weights_pav = [1.0] * n

    # PAV algorithm
    i = 0
    while i < n - 1:
        if result[i] > result[i + 1]:
            # Pool: merge i and i+1
            w = weights_pav[i] + weights_pav[i + 1]
            val = (
                weights_pav[i] * result[i]
                + weights_pav[i + 1] * result[i + 1]
            ) / w
            result[i] = val
            result[i + 1] = val
            weights_pav[i] = w
            weights_pav[i + 1] = w
            # Check backward
            while i > 0 and result[i - 1] > result[i]:
                w = weights_pav[i - 1] + weights_pav[i]
                val = (
                    weights_pav[i - 1] * result[i - 1]
                    + weights_pav[i] * result[i]
                ) / w
                result[i - 1] = val
                result[i] = val
                weights_pav[i - 1] = w
                weights_pav[i] = w
                i -= 1
        i += 1

    # Build lookup table
    lookup: list[tuple[float, float]] = []
    for idx, (p, _) in enumerate(pairs):
        lookup.append((p, result[idx]))

    return lookup


def apply_isotonic(
    p: float,
    lookup: list[tuple[float, float]],
) -> float:
    """Apply isotonic calibration to a single probability."""
    if not lookup:
        return p
    if p <= lookup[0][0]:
        return lookup[0][1]
    if p >= lookup[-1][0]:
        return lookup[-1][1]
    # Linear interpolation
    for i in range(len(lookup) - 1):
        if lookup[i][0] <= p <= lookup[i + 1][0]:
            span = lookup[i + 1][0] - lookup[i][0]
            t = (p - lookup[i][0]) / max(1e-10, span)
            return lookup[i][1] + t * (lookup[i + 1][1] - lookup[i][1])
    return p

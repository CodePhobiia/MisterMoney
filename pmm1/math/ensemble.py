"""Ensemble methods for combining multiple LLM forecasts.

From Research Paper 2, §4:
    Log-odds pooling: logit(p_ens) = Σ w_k * logit(p_k)
    MWU: w_k(t+1) = w_k(t) * exp(-η * loss_k(t)) / Z(t)
    BMA: P(M_k|D) ∝ P(M_k) * exp(-N * LogLoss_k)

Log-odds pooling is theoretically superior for prediction markets
because it properly handles extreme probabilities and satisfies
external Bayesianity.
"""

from __future__ import annotations

import math


def _logit(p: float) -> float:
    """Probability to log-odds."""
    p = max(1e-10, min(1.0 - 1e-10, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    """Log-odds to probability."""
    if x > 500:
        return 1.0
    if x < -500:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def log_pool(
    probs: list[float], weights: list[float] | None = None,
) -> float:
    """Logarithmic opinion pool in log-odds space.

    Superior to linear pooling for prediction markets:
    - Properly handles extreme probabilities
    - Satisfies external Bayesianity
    - Order of aggregation and updating doesn't matter

    Args:
        probs: List of probability estimates from different models.
        weights: Optional weights (normalized internally).
            If None, equal weights.

    Returns:
        Pooled probability estimate.
    """
    if not probs:
        return 0.5

    n = len(probs)
    if weights is None:
        w = [1.0 / n] * n
    else:
        total = sum(weights)
        if total <= 0:
            w = [1.0 / n] * n
        else:
            w = [wi / total for wi in weights]

    logit_sum = sum(wi * _logit(pi) for wi, pi in zip(w, probs))
    return _sigmoid(logit_sum)


def linear_pool(
    probs: list[float], weights: list[float] | None = None,
) -> float:
    """Simple weighted average (linear opinion pool).

    Guaranteed to improve on average individual error via
    Krogh-Vedelsby ambiguity decomposition:
        Ensemble Error = Avg Error - Diversity

    Args:
        probs: Probability estimates.
        weights: Optional weights.

    Returns:
        Weighted average probability.
    """
    if not probs:
        return 0.5

    n = len(probs)
    if weights is None:
        w = [1.0 / n] * n
    else:
        total = sum(weights)
        if total <= 0:
            w = [1.0 / n] * n
        else:
            w = [wi / total for wi in weights]

    return sum(wi * pi for wi, pi in zip(w, probs))


def inverse_brier_weights(brier_scores: list[float]) -> list[float]:
    """Compute weights inversely proportional to Brier scores.

    Better-calibrated models get higher weight.

    Args:
        brier_scores: Brier score for each model (lower = better).

    Returns:
        Normalized weights.
    """
    if not brier_scores:
        return []

    # Invert: lower Brier → higher weight
    inv = [1.0 / max(bs, 1e-6) for bs in brier_scores]
    total = sum(inv)
    return [w / total for w in inv]


def update_weights_mwu(
    weights: list[float],
    losses: list[float],
    eta: float = 0.1,
    min_weight: float = 0.05,
    round_number: int | None = None,
) -> list[float]:
    """Multiplicative Weights Update for online model adaptation.

    With η = sqrt(2 ln K / T) for K models over T rounds,
    regret ≤ sqrt(T ln K / 2).

    For 3 LLMs and 100 questions, per-question regret ≈ 0.074.

    When round_number is provided, eta is computed optimally as
    sqrt(2 ln(K) / T) per the M6 learning-rate decay finding.

    Args:
        weights: Current model weights.
        losses: Brier loss for each model on this observation.
        eta: Learning rate (overridden when round_number is set).
        min_weight: Floor to prevent weight collapse.
        round_number: Current round (1-based). If provided, eta
            is computed as sqrt(2 ln(K) / T) for optimal regret.

    Returns:
        Updated normalized weights with floor enforced.
    """
    if not weights or not losses:
        return weights

    # M6: adaptive learning rate decay
    if round_number is not None and round_number > 0:
        n_models = len(weights)
        if n_models >= 2:
            eta = min(1.0, math.sqrt(
                2 * math.log(n_models) / round_number
            ))

    n = len(weights)
    updated = []
    for w, loss in zip(weights, losses):
        new_w = w * math.exp(-eta * loss)
        updated.append(new_w)

    # Normalize
    total = sum(updated)
    if total <= 0:
        return [1.0 / n] * n
    normalized = [w / total for w in updated]

    # Enforce minimum weight floor
    # Q-H3: Iterative projection to ensure floor invariant
    if min_weight > 0:
        for _iter in range(10):  # max iterations to prevent infinite loop
            below_floor = [i for i, w in enumerate(normalized) if w < min_weight]
            if not below_floor:
                break
            deficit = sum(min_weight - normalized[i] for i in below_floor)
            above_floor = [i for i, w in enumerate(normalized) if w > min_weight]
            if not above_floor:
                break
            per_model_deduct = deficit / len(above_floor)
            for i in below_floor:
                normalized[i] = min_weight
            for i in above_floor:
                normalized[i] -= per_model_deduct
        # Final normalization
        total = sum(normalized)
        if total > 0:
            normalized = [w / total for w in normalized]

    return normalized


def bayesian_model_weights(
    cumulative_log_losses: list[float],
    prior: list[float] | None = None,
) -> list[float]:
    """Bayesian model averaging weights from cumulative performance.

    P(M_k | D) ∝ P(M_k) × exp(-N × LogLoss_k)

    The connection to Kelly is direct: log score measures the
    information advantage exploitable in trading.

    Args:
        cumulative_log_losses: Total log-loss for each model.
        prior: Prior model probabilities (default: uniform).

    Returns:
        Posterior model weights.
    """
    if not cumulative_log_losses:
        return []

    n = len(cumulative_log_losses)
    if prior is None:
        prior = [1.0 / n] * n

    # Compute unnormalized posteriors
    max_neg_loss = max(-ll for ll in cumulative_log_losses)
    posteriors = []
    for p_k, ll_k in zip(prior, cumulative_log_losses):
        # Subtract max for numerical stability
        log_post = math.log(max(p_k, 1e-10)) - ll_k - max_neg_loss
        posteriors.append(math.exp(log_post))

    total = sum(posteriors)
    if total <= 0:
        return [1.0 / n] * n
    return [p / total for p in posteriors]


def ensemble_diversity(
    probs: list[float], weights: list[float] | None = None,
) -> float:
    """Measure ensemble diversity (Krogh-Vedelsby ambiguity).

    Higher diversity means the ensemble benefits more from aggregation.
    With pairwise correlation ρ=0.5 among 3 models, diversity
    reduces variance by only 33% vs 67% with uncorrelated models.

    Returns:
        Weighted variance of predictions around the ensemble mean.
    """
    if not probs or len(probs) < 2:
        return 0.0

    n = len(probs)
    if weights is None:
        w = [1.0 / n] * n
    else:
        total = sum(weights)
        w = [wi / total for wi in weights] if total > 0 else [1.0 / n] * n

    mean_p = sum(wi * pi for wi, pi in zip(w, probs))
    diversity = sum(wi * (pi - mean_p) ** 2 for wi, pi in zip(w, probs))
    return diversity

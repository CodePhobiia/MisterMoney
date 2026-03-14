"""Statistical validation for edge detection and performance tracking.

From Research Paper 2, §5:
    Power analysis: N = 6.18 × p_m(1-p_m) / δ²
    SPRT: Sequential test with early stopping (~50% fewer trades)
    Sharpe: SR_trade = δ / sqrt(p_m(1-p_m))

The fundamental tension: small edges require large sample sizes.
A 5% edge at even odds needs ~620 trades to confirm.
"""

from __future__ import annotations

import math


def required_sample_size(
    edge: float,
    p_market: float = 0.5,
    alpha: float = 0.05,
    power: float = 0.80,
) -> int:
    """Number of trades needed to confirm edge with significance.

    N = (z_α + z_β)² × p_m(1-p_m) / δ²

    Args:
        edge: Suspected edge (δ = |p_true - p_market|).
        p_market: Market price.
        alpha: Significance level (default 0.05).
        power: Statistical power (default 0.80).

    Returns:
        Required number of trades.
    """
    if edge <= 0:
        return 999999

    # z-scores: one-sided test (Paper 2 uses (z_α + z_β)² = 6.18)
    z_alpha = _z_score(1.0 - alpha)
    z_beta = _z_score(power)

    n = ((z_alpha + z_beta) ** 2 * p_market * (1.0 - p_market)) / (edge ** 2)
    return max(1, math.ceil(n))


def per_trade_sharpe(edge: float, p_market: float = 0.5) -> float:
    """Per-trade Sharpe ratio.

    SR_trade = δ / sqrt(p_m(1-p_m))

    At even odds with 5% edge: SR = 0.10.

    Args:
        edge: Edge per trade.
        p_market: Market price.

    Returns:
        Per-trade Sharpe ratio.
    """
    variance = p_market * (1.0 - p_market)
    if variance <= 0:
        return 0.0
    return edge / math.sqrt(variance)


def annualized_sharpe(
    per_trade_sr: float, trades_per_year: int = 500,
) -> float:
    """Annualized Sharpe from per-trade Sharpe.

    SR_annual = SR_trade × sqrt(N_trades/year)

    With 500 trades/year and 5% edge: SR_annual = 2.24.
    Context: S&P 500 long-term ≈ 0.4, top quant funds ≈ 1.0-2.0.
    """
    return per_trade_sr * math.sqrt(trades_per_year)


def rolling_sharpe(returns: list[float], window: int = 100) -> float:
    """Rolling Sharpe ratio over recent trades.

    Args:
        returns: Per-trade returns (positive = profit).
        window: Number of recent trades to consider.

    Returns:
        Sharpe ratio (mean / std of returns).
    """
    if len(returns) < 2:
        return 0.0

    recent = returns[-window:]
    n = len(recent)
    mean = sum(recent) / n
    var = sum((r - mean) ** 2 for r in recent) / (n - 1)

    if var <= 0:
        return 0.0 if mean == 0 else float("inf") if mean > 0 else float("-inf")

    return mean / math.sqrt(var)


def sprt_update(
    log_ratio: float,
    outcome: float,
    p_true: float,
    p_null: float,
    upper_bound: float = 2.77,
    lower_bound: float = -1.56,
) -> tuple[float, str]:
    """Sequential Probability Ratio Test update.

    After each trade, update the log-likelihood ratio.
    Stop early when evidence is conclusive (~50% fewer trades than
    fixed-sample tests).

    Boundaries for α=0.05, β=0.20:
        Accept H1 (edge exists): Λ ≥ ln((1-β)/α) ≈ 2.77
        Accept H0 (no edge):     Λ ≤ ln(β/(1-α)) ≈ -1.56

    Args:
        log_ratio: Current cumulative log-likelihood ratio.
        outcome: Observed outcome (0 or 1).
        p_true: Probability under H1 (edge exists).
        p_null: Probability under H0 (no edge = market price).
        upper_bound: Accept H1 boundary.
        lower_bound: Accept H0 boundary.

    Returns:
        (updated_log_ratio, decision) where decision is
        "edge_confirmed", "no_edge", or "undecided".
    """
    p1 = max(1e-10, min(1.0 - 1e-10, p_true))
    p0 = max(1e-10, min(1.0 - 1e-10, p_null))

    if outcome > 0.5:
        lr = math.log(p1 / p0)
    else:
        lr = math.log((1.0 - p1) / (1.0 - p0))

    log_ratio += lr

    if log_ratio >= upper_bound:
        return (log_ratio, "edge_confirmed")
    elif log_ratio <= lower_bound:
        return (log_ratio, "no_edge")
    else:
        return (log_ratio, "undecided")


def sprt_update_glr(
    log_ratio: float,
    outcome: float,
    running_wins: int,
    running_total: int,
    p_null: float,
    upper_bound: float = 2.77,
    lower_bound: float = -1.56,
) -> tuple[float, str]:
    """SPRT with running MLE as adaptive alternative.

    Solves the composite hypothesis problem: instead of testing
    against a fixed p_true, uses the running MLE (empirical win
    rate) as H1. This correctly detects edges of any size.

    Args:
        log_ratio: Current cumulative log-likelihood ratio.
        outcome: Observed outcome (0 or 1).
        running_wins: Total wins so far.
        running_total: Total trades so far.
        p_null: Probability under H0 (no edge = market price).
        upper_bound: Accept H1 boundary.
        lower_bound: Accept H0 boundary.

    Returns:
        (updated_log_ratio, decision).
    """
    if running_total < 2:
        return (log_ratio, "undecided")

    p_mle = max(0.01, min(0.99, running_wins / running_total))

    # If MLE is very close to null, not enough evidence
    if abs(p_mle - p_null) < 0.005:
        return (log_ratio, "undecided")

    return sprt_update(
        log_ratio, outcome, p_true=p_mle, p_null=p_null,
        upper_bound=upper_bound, lower_bound=lower_bound,
    )


def brier_score(probs: list[float], outcomes: list[float]) -> float:
    """Brier score: mean squared error of probability forecasts.

    BS = (1/N) Σ (f_i - o_i)²

    Lower is better. Perfect = 0.0, random at 50% = 0.25.

    Args:
        probs: Predicted probabilities.
        outcomes: Actual outcomes (0 or 1).

    Returns:
        Brier score.
    """
    if not probs or len(probs) != len(outcomes):
        return 1.0

    total = sum((p - o) ** 2 for p, o in zip(probs, outcomes))
    return total / len(probs)


def expected_calibration_error(
    probs: list[float],
    outcomes: list[float],
    n_bins: int = 15,
) -> float:
    """Expected Calibration Error (ECE).

    Measures how well predicted probabilities match observed
    frequencies. ECE of 5% ≈ 5 cents average edge per dollar.

    Args:
        probs: Predicted probabilities.
        outcomes: Actual outcomes (0 or 1).
        n_bins: Number of calibration bins.

    Returns:
        ECE (lower is better).
    """
    if not probs or len(probs) != len(outcomes):
        return 1.0

    n = len(probs)
    bin_width = 1.0 / n_bins
    ece = 0.0

    for i in range(n_bins):
        lo = i * bin_width
        hi = (i + 1) * bin_width

        bin_probs = []
        bin_outcomes = []
        for p, o in zip(probs, outcomes):
            if (p > lo and p <= hi) or (i == 0 and p >= lo and p <= hi):
                bin_probs.append(p)
                bin_outcomes.append(o)

        if not bin_probs:
            continue

        avg_prob = sum(bin_probs) / len(bin_probs)
        avg_outcome = sum(bin_outcomes) / len(bin_outcomes)
        ece += (len(bin_probs) / n) * abs(avg_outcome - avg_prob)

    return ece


def log_loss(probs: list[float], outcomes: list[float]) -> float:
    """Log loss (negative log-likelihood).

    Directly predicts Kelly growth rate:
    A forecaster with cumulative log loss L_you vs market's L_market
    compounds wealth at exp(L_market - L_you) relative to the market.

    Args:
        probs: Predicted probabilities.
        outcomes: Actual outcomes.

    Returns:
        Average log loss (lower = better).
    """
    if not probs or len(probs) != len(outcomes):
        return 100.0

    total = 0.0
    for p, o in zip(probs, outcomes):
        p = max(1e-10, min(1.0 - 1e-10, p))
        if o > 0.5:
            total -= math.log(p)
        else:
            total -= math.log(1.0 - p)

    return total / len(probs)


def _z_score(percentile: float) -> float:
    """Approximate z-score for common percentiles.

    Uses rational approximation (Abramowitz & Stegun).
    """
    if percentile <= 0 or percentile >= 1:
        return 0.0

    # Beasley-Springer-Moro approximation
    p = percentile
    if p < 0.5:
        p = 1.0 - p
        sign = -1.0
    else:
        sign = 1.0

    t = math.sqrt(-2.0 * math.log(1.0 - p))
    c0 = 2.515517
    c1 = 0.802853
    c2 = 0.010328
    d1 = 1.432788
    d2 = 0.189269
    d3 = 0.001308

    z = t - (c0 + c1 * t + c2 * t * t) / (
        1.0 + d1 * t + d2 * t * t + d3 * t * t * t
    )
    return sign * z

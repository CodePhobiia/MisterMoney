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


def rolling_sharpe(
    returns: list[float],
    window: int = 100,
    lo_correction: bool = False,
    annualization_factor: int = 500,
) -> float:
    """Rolling Sharpe ratio over recent trades.

    Args:
        returns: Per-trade returns (positive = profit).
        window: Number of recent trades to consider.
        lo_correction: If True, apply Lo (2002) autocorrelation correction.
        annualization_factor: Trades per year for annualization when using
            Lo correction.

    Returns:
        Sharpe ratio (mean / std of returns).  When lo_correction is True
        the returned value is the annualized, autocorrelation-corrected SR.
    """
    if len(returns) < 2:
        return 0.0

    recent = returns[-window:]
    n = len(recent)
    mean = sum(recent) / n
    var = sum((r - mean) ** 2 for r in recent) / (n - 1)

    if var <= 0:
        return 0.0 if mean == 0 else 10.0 if mean > 0 else -10.0

    raw_sr = mean / math.sqrt(var)

    if lo_correction:
        corrected, _ = lo_corrected_sharpe(
            recent, annualization_factor=annualization_factor,
        )
        return corrected

    return raw_sr


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
    upper_bound: float = 3.84,
    lower_bound: float = -1.56,
) -> tuple[float, str]:
    """GLR-based sequential test using batch likelihood ratio.

    Instead of accumulating incremental log-ratios with a shifting MLE
    (which inflates the statistic to ~80% false positive rate), we
    recompute the full batch log-likelihood ratio at each step:

        GLR = n * KL(p_mle || p_null)

    where p_mle is estimated from ALL observations in the current window.
    The upper_bound default of 3.84 corresponds to the chi-squared(1)
    critical value at alpha=0.05, which is the correct threshold for
    a composite-alternative GLR test (unlike the Wald boundary of 2.77
    which only applies to simple-vs-simple SPRT).
    """
    if running_total < 2:
        return (0.0, "undecided")

    p_mle = running_wins / running_total
    p0 = max(1e-10, min(1.0 - 1e-10, p_null))

    # If MLE is very close to null, no evidence either way
    if abs(p_mle - p0) < 0.005:
        return (0.0, "undecided")

    # Clamp MLE away from boundaries
    p_mle = max(1e-10, min(1.0 - 1e-10, p_mle))

    # Batch GLR = n * KL(p_mle || p_null)
    kl = p_mle * math.log(p_mle / p0) + (1.0 - p_mle) * math.log(
        (1.0 - p_mle) / (1.0 - p0)
    )
    glr = running_total * kl

    # Apply chi-squared-calibrated boundary
    if glr >= upper_bound:
        # Check direction: is the edge in favor of the trader?
        if p_mle > p0:
            return (glr, "edge_confirmed")
        else:
            return (glr, "no_edge")

    # For the no-edge direction, check if evidence strongly favors null
    # With batch GLR, we need a different approach for H0 acceptance:
    # If after sufficient samples, GLR is still very low, accept H0
    if running_total >= 200 and glr < 0.5:
        return (glr, "no_edge")

    return (glr, "undecided")


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


def glr_to_pvalue(glr_stat: float) -> float:
    """Convert GLR statistic to chi-squared(1) p-value.

    Uses Wilson-Hilferty approximation for chi-squared survival function.
    """
    if glr_stat <= 0:
        return 1.0
    # Wilson-Hilferty approximation for chi-squared(1) CDF
    z = (glr_stat ** (1 / 3) - (1 - 2 / 9)) / math.sqrt(2 / 9)
    # Standard normal survival function approximation
    return 0.5 * math.erfc(z / math.sqrt(2))


class CusumDetector:
    """Two-sided CUSUM for detecting edge appearance/disappearance.

    Upper CUSUM detects positive edge (reinforce).
    Lower CUSUM detects edge disappearance (defensive mode).

    ARL0 ~500, designed to detect 5% shift in win rate.
    """

    def __init__(
        self, mu0: float = 0.5, k: float = 0.025, h: float = 4.0,
    ) -> None:
        self.mu0 = mu0  # null mean (break-even win rate)
        self.k = k       # allowance parameter (half the shift to detect)
        self.h = h       # threshold (controls ARL0)
        self.S_up = 0.0
        self.S_dn = 0.0
        self._alarm_up = False
        self._alarm_dn = False

    def update(self, x: float) -> None:
        """Update with new observation (1.0 = win, 0.0 = loss)."""
        self.S_up = max(0.0, self.S_up + x - self.mu0 - self.k)
        self.S_dn = max(0.0, self.S_dn - x + self.mu0 - self.k)
        if self.S_up >= self.h:
            self._alarm_up = True
        if self.S_dn >= self.h:
            self._alarm_dn = True

    def reset(self) -> None:
        self.S_up = 0.0
        self.S_dn = 0.0
        self._alarm_up = False
        self._alarm_dn = False

    @property
    def edge_alarm(self) -> bool:
        """True if positive edge detected."""
        return self._alarm_up

    @property
    def no_edge_alarm(self) -> bool:
        """True if edge disappearance detected."""
        return self._alarm_dn

    @property
    def status(self) -> str:
        if self._alarm_dn:
            return "edge_lost"
        if self._alarm_up:
            return "edge_detected"
        return "monitoring"


def lo_corrected_sharpe(
    returns: list[float],
    annualization_factor: int = 500,
    max_lag: int = 5,
) -> tuple[float, float]:
    """Sharpe ratio with Lo (2002) autocorrelation correction.

    Standard annualization overstates Sharpe by up to 65% with positive
    autocorrelation. This correction accounts for serial correlation.

    Returns:
        (corrected_sharpe, standard_error)
    """
    n = len(returns)
    if n < max_lag + 2:
        return (0.0, float("inf"))

    mean_r = sum(returns) / n
    var_r = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    if var_r < 1e-15:
        return (0.0, float("inf"))

    std_r = var_r ** 0.5
    sr_per_trade = mean_r / std_r

    # Compute empirical autocorrelations up to max_lag
    autocorrs: list[float] = []
    for lag in range(1, max_lag + 1):
        cov = sum(
            (returns[i] - mean_r) * (returns[i - lag] - mean_r)
            for i in range(lag, n)
        ) / (n - 1)
        autocorrs.append(cov / var_r)

    # Lo correction: eta(q) = q + 2 * sum_{k=1}^{q-1} (q-k) * rho_k
    q = annualization_factor
    eta = float(q)
    for lag_idx, rho_k in enumerate(autocorrs):
        k = lag_idx + 1
        if k < q:
            eta += 2 * (q - k) * rho_k

    # Prevent negative eta (pathological case)
    eta = max(1.0, eta)

    # Corrected annualized Sharpe
    # Naive: SR_annual = SR_per_trade * sqrt(q)
    # Lo:    SR_annual = SR_per_trade * q / sqrt(eta)
    # When eta == q (iid), this equals the naive formula.
    corrected_sr = sr_per_trade * q / eta ** 0.5

    # Standard error of Sharpe ratio
    se = ((1 + 0.5 * sr_per_trade ** 2) / n) ** 0.5

    return (corrected_sr, se)


def brier_decomposition(
    probs: list[float],
    outcomes: list[float],
    n_bins: int = 10,
) -> tuple[float, float, float]:
    """Murphy (1973) Brier score decomposition.

    BS = reliability - resolution + uncertainty

    - Reliability (calibration error): lower is better
    - Resolution (forecast sharpness): higher is better
    - Uncertainty (inherent task difficulty): not controllable

    Returns:
        (reliability, resolution, uncertainty)
    """
    n = len(probs)
    if n == 0:
        return (0.0, 0.0, 0.25)

    base_rate = sum(outcomes) / n
    uncertainty = base_rate * (1 - base_rate)

    # Bin forecasts
    bins: dict[int, list[tuple[float, float]]] = {}
    for p, o in zip(probs, outcomes):
        b = min(int(p * n_bins), n_bins - 1)
        bins.setdefault(b, []).append((p, o))

    reliability = 0.0
    resolution = 0.0

    for bin_items in bins.values():
        n_k = len(bin_items)
        if n_k == 0:
            continue
        avg_forecast = sum(p for p, _ in bin_items) / n_k
        avg_outcome = sum(o for _, o in bin_items) / n_k

        reliability += n_k * (avg_forecast - avg_outcome) ** 2
        resolution += n_k * (avg_outcome - base_rate) ** 2

    reliability /= n
    resolution /= n

    return (reliability, resolution, uncertainty)


def beta_sf(x: float, a: float, b: float) -> float:
    """Survival function of the Beta distribution: P(X > x).

    Uses the regularized incomplete beta function approximation.
    For ST-03 Bayesian edge confidence.
    """
    if x <= 0:
        return 1.0
    if x >= 1:
        return 0.0
    return 1.0 - _regularized_incomplete_beta(x, a, b)


def _regularized_incomplete_beta(
    x: float, a: float, b: float, max_iter: int = 300,
) -> float:
    """Regularized incomplete beta function I(x; a, b).

    Uses the continued fraction representation (Numerical Recipes
    ``betacf``, Press et al.) evaluated via the modified Lentz method.
    """
    if x < 0 or x > 1:
        return 0.0
    if x == 0:
        return 0.0
    if x == 1:
        return 1.0

    # Use symmetry relation when x > (a+1)/(a+b+2) for faster convergence
    if x > (a + 1) / (a + b + 2):
        return 1.0 - _regularized_incomplete_beta(1 - x, b, a, max_iter)

    # Front factor: x^a * (1-x)^b / (a * B(a,b))
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(
        lbeta + a * math.log(max(1e-300, x)) + b * math.log(max(1e-300, 1 - x)),
    ) / a

    # Evaluate continued fraction by modified Lentz method.
    # CF = 1 / (1 + d_1/(1 + d_2/(1 + ...)))
    # d_{2m+1} = -(a+m)(a+b+m) x / ((a+2m)(a+2m+1))
    # d_{2m}   = m(b-m) x / ((a+2m-1)(a+2m))
    #
    # NR's betacf evaluates this CF. The result I_x(a,b) = front * betacf.
    #
    # Modified Lentz: f = b_0 = 1, then for each a_j, b_j = 1:
    #   C_j = b_j + a_j/C_{j-1}
    #   D_j = 1/(b_j + a_j*D_{j-1})
    #   delta_j = C_j * D_j
    #   f = f * delta_j
    eps = 1e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0

    # Start Lentz: b_0 = 1, so f = C_0 = 1, D_0 = 1/(1-0) = 1
    # But if 1 - qab*x/qap == 0 we need the eps trick
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < eps:
        d = eps
    d = 1.0 / d
    f = d

    for m in range(1, max_iter + 1):
        m2 = 2 * m

        # Even step: a_{2m} = m(b-m)x / ((a+2m-1)(a+2m))
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < eps:
            d = eps
        c = 1.0 + aa / c
        if abs(c) < eps:
            c = eps
        d = 1.0 / d
        f *= c * d

        # Odd step: a_{2m+1} = -(a+m)(a+b+m)x / ((a+2m)(a+2m+1))
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < eps:
            d = eps
        c = 1.0 + aa / c
        if abs(c) < eps:
            c = eps
        d = 1.0 / d
        delta = c * d
        f *= delta

        if abs(delta - 1.0) < 1e-14:
            break

    return front * f


def evalue_update(
    e_prev: float,
    outcome: float,
    running_wins: int,
    running_total: int,
    p_null: float = 0.5,
) -> tuple[float, str]:
    """E-value sequential test with anytime-valid guarantees (ST-07).

    E-process: product of likelihood ratios p_mle(x) / p_null(x).
    Threshold: 1/alpha = 20 for alpha=0.05.

    Returns (e_value, decision) where decision is 'edge_confirmed',
    'no_edge', or 'undecided'.
    """
    if running_total < 1:
        return (1.0, "undecided")

    # MLE of win rate
    p_mle = max(0.01, min(0.99, running_wins / running_total))
    p_null = max(0.01, min(0.99, p_null))

    # Likelihood ratio for this observation
    if outcome > 0.5:
        lr = p_mle / p_null
    else:
        lr = (1 - p_mle) / (1 - p_null)

    e_new = e_prev * lr

    # Anytime-valid threshold: 1/alpha = 20 for alpha=0.05
    if e_new >= 20.0:
        return (e_new, "edge_confirmed")
    elif e_new <= 1 / 20.0:
        return (e_new, "no_edge")

    return (e_new, "undecided")


def pav_calibrate(probs: list[float], outcomes: list[float]) -> list[float]:
    """Pool Adjacent Violators for calibration (ST-10).

    Returns calibrated probabilities (same length as input).
    More robust than ECE at distribution tails.
    """
    n = len(probs)
    if n == 0:
        return []

    # Sort by predicted probability
    order = sorted(range(n), key=lambda i: probs[i])
    sorted_outcomes = [outcomes[i] for i in order]

    # PAV algorithm
    result = list(sorted_outcomes)
    weights = [1] * n
    i = 0
    while i < n - 1:
        if result[i] > result[i + 1]:
            w = weights[i] + weights[i + 1]
            val = (weights[i] * result[i] + weights[i + 1] * result[i + 1]) / w
            result[i] = val
            result[i + 1] = val
            weights[i] = w
            weights[i + 1] = w
            while i > 0 and result[i - 1] > result[i]:
                w = weights[i - 1] + weights[i]
                val = (weights[i - 1] * result[i - 1] + weights[i] * result[i]) / w
                result[i - 1] = val
                result[i] = val
                weights[i - 1] = w
                weights[i] = w
                i -= 1
        i += 1

    # Unsort
    calibrated = [0.0] * n
    for idx, orig_idx in enumerate(order):
        calibrated[orig_idx] = result[idx]

    return calibrated


def max_calibration_error(
    probs: list[float], outcomes: list[float], n_bins: int = 15,
) -> float:
    """Maximum Calibration Error across bins (ST-10)."""
    if not probs:
        return 0.0
    bins: dict[int, list[tuple[float, float]]] = {}
    for p, o in zip(probs, outcomes):
        b = min(int(p * n_bins), n_bins - 1)
        bins.setdefault(b, []).append((p, o))

    max_err = 0.0
    for items in bins.values():
        avg_p = sum(p for p, _ in items) / len(items)
        avg_o = sum(o for _, o in items) / len(items)
        max_err = max(max_err, abs(avg_p - avg_o))
    return max_err


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

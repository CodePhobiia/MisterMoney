"""Tests for statistical validation — Paper 2 §5 worked examples."""

import json
import math

from pmm1.math.validation import (
    CusumDetector,
    annualized_sharpe,
    brier_decomposition,
    brier_score,
    expected_calibration_error,
    glr_to_pvalue,
    lo_corrected_sharpe,
    log_loss,
    per_trade_sharpe,
    required_sample_size,
    rolling_sharpe,
    sprt_update,
    sprt_update_glr,
)


def test_sample_size_paper2_table():
    """Paper 2 table: 5% edge at even odds → ~618 trades."""
    n = required_sample_size(edge=0.05, p_market=0.50)
    assert 600 <= n <= 650


def test_sample_size_3pct_edge():
    """Paper 2: 3% edge at even odds → ~1717 trades."""
    n = required_sample_size(edge=0.03, p_market=0.50)
    assert 1600 <= n <= 1800


def test_sample_size_10pct_edge():
    """Paper 2: 10% edge at even odds → ~155 trades."""
    n = required_sample_size(edge=0.10, p_market=0.50)
    assert 140 <= n <= 170


def test_sample_size_high_price():
    """Paper 2: 5% edge at p_m=0.80 → ~396 trades."""
    n = required_sample_size(edge=0.05, p_market=0.80)
    assert 370 <= n <= 420


def test_sample_size_extreme_price():
    """Paper 2: 10% edge at p_m=0.95 → ~30 trades."""
    n = required_sample_size(edge=0.10, p_market=0.95)
    assert 20 <= n <= 40


def test_per_trade_sharpe():
    """Paper 2: 5% edge at even odds → SR = 0.10."""
    sr = per_trade_sharpe(0.05, 0.50)
    assert abs(sr - 0.10) < 0.01


def test_annualized_sharpe_paper2():
    """Paper 2: 500 trades/year, 5% edge → SR_annual = 2.24."""
    sr_trade = per_trade_sharpe(0.05, 0.50)
    sr_annual = annualized_sharpe(sr_trade, 500)
    assert abs(sr_annual - 2.24) < 0.1


def test_annualized_sharpe_context():
    """Paper 2: S&P 500 long-term ≈ 0.4, top quants ≈ 1.0-2.0."""
    sr = annualized_sharpe(per_trade_sharpe(0.05, 0.50), 500)
    assert sr > 2.0  # Should be excellent


def test_rolling_sharpe_positive():
    """Positive returns → positive Sharpe."""
    returns = [0.05, 0.03, 0.07, 0.04, 0.06] * 20
    sr = rolling_sharpe(returns)
    assert sr > 0


def test_rolling_sharpe_negative():
    """Negative returns → negative Sharpe."""
    returns = [-0.05, -0.03, -0.07, -0.04, -0.06] * 20
    sr = rolling_sharpe(returns)
    assert sr < 0


def test_rolling_sharpe_window():
    """Only uses last N trades."""
    # Old trades bad, recent trades good
    returns = [-0.10] * 50 + [0.05] * 50
    sr = rolling_sharpe(returns, window=50)
    assert sr > 0  # Should reflect recent good performance


def test_sprt_edge_confirmed():
    """Paper 2: SPRT confirms edge when Λ ≥ 2.77."""
    # Simulate a biased coin at 58% when market says 50%
    import random

    random.seed(42)
    log_ratio = 0.0
    decision = "undecided"
    trades = 0

    while decision == "undecided" and trades < 1000:
        outcome = 1.0 if random.random() < 0.58 else 0.0
        log_ratio, decision = sprt_update(
            log_ratio, outcome, p_true=0.58, p_null=0.50,
        )
        trades += 1

    assert decision == "edge_confirmed"
    assert trades < 500  # SPRT should be faster than fixed-sample


def test_sprt_no_edge():
    """SPRT rejects edge when no edge exists."""
    import random

    random.seed(42)
    log_ratio = 0.0
    decision = "undecided"

    # 200 fair coin flips, tested against 55% hypothesis
    for _ in range(200):
        outcome = 1.0 if random.random() < 0.50 else 0.0
        log_ratio, decision = sprt_update(
            log_ratio, outcome, p_true=0.55, p_null=0.50,
        )
        if decision != "undecided":
            break

    # With fair coin and 55% H1, SPRT should either reject or be undecided
    assert decision in ("no_edge", "undecided")


def test_sprt_boundaries():
    """SPRT boundaries are correct for α=0.05, β=0.20."""
    # Upper: ln((1-β)/α) = ln(0.80/0.05) = ln(16) ≈ 2.77
    upper = math.log(0.80 / 0.05)
    assert abs(upper - 2.77) < 0.01

    # Lower: ln(β/(1-α)) = ln(0.20/0.95) ≈ -1.56
    lower = math.log(0.20 / 0.95)
    assert abs(lower - (-1.56)) < 0.01


def test_brier_score_perfect():
    """Perfect predictions → Brier = 0."""
    bs = brier_score([1.0, 0.0, 1.0], [1.0, 0.0, 1.0])
    assert abs(bs) < 1e-10


def test_brier_score_random():
    """50% predictions → Brier = 0.25."""
    bs = brier_score([0.5, 0.5, 0.5, 0.5], [1.0, 0.0, 1.0, 0.0])
    assert abs(bs - 0.25) < 1e-10


def test_brier_score_bad():
    """Completely wrong → Brier = 1.0."""
    bs = brier_score([0.0, 1.0], [1.0, 0.0])
    assert abs(bs - 1.0) < 1e-10


def test_ece_well_calibrated():
    """Well-calibrated predictions have low ECE."""
    # Predictions match outcome frequencies
    probs = [0.2] * 50 + [0.8] * 50
    import random

    random.seed(42)
    outcomes = (
        [1.0 if random.random() < 0.2 else 0.0 for _ in range(50)]
        + [1.0 if random.random() < 0.8 else 0.0 for _ in range(50)]
    )
    ece = expected_calibration_error(probs, outcomes)
    assert ece < 0.15  # Should be low


def test_ece_miscalibrated():
    """Miscalibrated predictions have high ECE."""
    # Says 90% but true rate is 50%
    probs = [0.90] * 100
    import random

    random.seed(42)
    outcomes = [1.0 if random.random() < 0.50 else 0.0 for _ in range(100)]
    ece = expected_calibration_error(probs, outcomes)
    assert ece > 0.30  # Should be high


def test_log_loss_perfect():
    """Perfect predictions have near-zero log loss."""
    ll = log_loss([0.99, 0.01, 0.99], [1.0, 0.0, 1.0])
    assert ll < 0.02


def test_log_loss_kelly_connection():
    """Paper 2: log score directly predicts Kelly profitability.

    A forecaster with lower log loss compounds wealth faster.
    """
    # Better forecaster
    ll_good = log_loss([0.70, 0.30], [1.0, 0.0])
    # Worse forecaster
    ll_bad = log_loss([0.55, 0.45], [1.0, 0.0])
    assert ll_good < ll_bad

    # Wealth ratio = exp(ll_bad - ll_good)
    wealth_ratio = math.exp(ll_bad - ll_good)
    assert wealth_ratio > 1.0  # Better forecaster makes more


def test_paper2_worked_example_200_trades():
    """Paper 2: 200 trades, 58% win rate at 50% odds.

    Z-statistic = 2.26, p-value ≈ 0.012.
    Edge = 8%, 95% CI [1.1%, 14.9%].
    """
    n = 200
    win_rate = 0.58
    p_market = 0.50
    edge = win_rate - p_market

    se = math.sqrt(p_market * (1 - p_market) / n)
    z = edge / se

    assert abs(z - 2.26) < 0.05
    assert abs(se - 0.0354) < 0.001

    # Annualized Sharpe
    sr_trade = per_trade_sharpe(edge, p_market)
    sr_annual = annualized_sharpe(sr_trade, n)
    assert abs(sr_annual - 2.26) < 0.1


def test_rolling_sharpe_capped_positive():
    """F02: Constant positive returns produce finite Sharpe <= 10.0."""
    sr = rolling_sharpe([0.05] * 100)
    assert math.isfinite(sr)
    assert sr <= 10.0


def test_rolling_sharpe_capped_negative():
    """F02: Constant negative returns produce finite Sharpe >= -10.0."""
    sr = rolling_sharpe([-0.05] * 100)
    assert math.isfinite(sr)
    assert sr >= -10.0


def test_rolling_sharpe_json_serializable():
    """F02: rolling_sharpe result is JSON-serializable (no inf/nan)."""
    sr = rolling_sharpe([1.0] * 50)
    # Must not raise
    json.dumps({"sharpe": sr})


def test_sprt_glr_detects_small_edge():
    """GLR test detects a 5% edge given enough observations.

    A 5% edge at even odds needs ~620 trades for fixed-sample significance
    (Paper 2 table). The batch GLR with chi-squared boundary needs a similar
    number. We allow up to 800 trades for the sequential test.
    """
    import random

    random.seed(42)
    log_ratio = 0.0
    wins = 0
    total = 0
    decision = "undecided"
    for _ in range(800):
        outcome = 1.0 if random.random() < 0.55 else 0.0
        total += 1
        if outcome > 0.5:
            wins += 1
        log_ratio, decision = sprt_update_glr(
            log_ratio, outcome, wins, total, p_null=0.50,
        )
        if decision != "undecided":
            break
    assert decision == "edge_confirmed"


def test_sprt_glr_false_positive_rate():
    """Monte Carlo: GLR should have <10% FPR under H0."""
    import random

    random.seed(42)
    false_positives = 0
    trials = 2000
    for _ in range(trials):
        p_null = 0.50
        wins = 0
        total = 0
        log_ratio = 0.0
        decision = "undecided"
        for _ in range(300):
            outcome = 1.0 if random.random() < p_null else 0.0
            total += 1
            if outcome > 0.5:
                wins += 1
            log_ratio, decision = sprt_update_glr(
                log_ratio, outcome, wins, total, p_null=p_null,
            )
            if decision != "undecided":
                break
        if decision == "edge_confirmed":
            false_positives += 1
    fpr = false_positives / trials
    assert fpr < 0.10, f"False positive rate {fpr:.3f} exceeds 10%"


# ── ST-09: GLR p-value export ─────────────────────────────────────


def test_glr_to_pvalue():
    """GLR=0 → p=1.0, GLR=3.84 → p≈0.05, GLR=6.63 → p≈0.01."""
    assert glr_to_pvalue(0.0) == 1.0
    assert glr_to_pvalue(-1.0) == 1.0

    p_384 = glr_to_pvalue(3.84)
    assert 0.03 <= p_384 <= 0.07, f"Expected ~0.05, got {p_384}"

    p_663 = glr_to_pvalue(6.63)
    assert 0.005 <= p_663 <= 0.02, f"Expected ~0.01, got {p_663}"


# ── ST-01: CUSUM edge disappearance detector ──────────────────────


def test_cusum_detects_shift():
    """100 obs at 50% (no alarm), then 200 at 55% → edge_alarm fires."""
    import random

    random.seed(42)
    det = CusumDetector(mu0=0.5, k=0.025, h=4.0)

    # Phase 1: alternating wins/losses (exactly 50%, no alarm possible)
    for i in range(100):
        det.update(1.0 if i % 2 == 0 else 0.0)
    assert not det.edge_alarm, "False alarm during fair-coin phase"

    # Phase 2: biased coin at 55% — should eventually trigger
    for _ in range(200):
        det.update(1.0 if random.random() < 0.55 else 0.0)

    assert det.edge_alarm, "CUSUM failed to detect 5% upward shift"
    assert det.status in ("edge_detected", "edge_lost")


def test_cusum_no_false_alarm():
    """500 obs at exactly 50% — no alarm (ARL0 test).

    Uses alternating wins/losses so the sequence is perfectly balanced.
    """
    det = CusumDetector(mu0=0.5, k=0.025, h=4.0)

    for i in range(500):
        det.update(1.0 if i % 2 == 0 else 0.0)

    assert not det.edge_alarm, "False positive on fair coin (upper)"
    assert not det.no_edge_alarm, "False positive on fair coin (lower)"
    assert det.status == "monitoring"


# ── ST-04: Sharpe autocorrelation correction (Lo 2002) ────────────


def test_lo_corrected_sharpe_no_autocorrelation():
    """With iid returns, Lo correction should be minimal."""
    import random

    random.seed(96)
    # iid normal-ish returns with small positive mean
    returns = [random.gauss(0.001, 0.01) for _ in range(500)]

    corrected, se = lo_corrected_sharpe(returns, annualization_factor=500)
    n = len(returns)
    mean_r = sum(returns) / n
    var_r = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    naive_annual = (mean_r / var_r ** 0.5) * 500 ** 0.5

    # With iid data the correction should leave the SR roughly the same
    ratio = corrected / naive_annual if abs(naive_annual) > 1e-10 else 1.0
    assert 0.85 <= ratio <= 1.15, (
        f"Expected minimal correction, got ratio={ratio:.3f}"
    )
    assert se > 0 and math.isfinite(se)


def test_lo_corrected_sharpe_positive_autocorrelation():
    """With known rho≈0.1, corrected SR should be meaningfully lower."""
    import random

    random.seed(7)
    n = 1000
    # Generate AR(1) returns with rho ≈ 0.3 to make the effect strong
    rho = 0.3
    returns = [random.gauss(0.002, 0.01)]
    for _ in range(n - 1):
        r = 0.002 + rho * (returns[-1] - 0.002) + random.gauss(0, 0.01)
        returns.append(r)

    corrected, _ = lo_corrected_sharpe(returns, annualization_factor=500)
    mean_r = sum(returns) / len(returns)
    var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    naive_annual = (mean_r / var_r ** 0.5) * 500 ** 0.5

    # Corrected should be noticeably lower than naive
    assert corrected < naive_annual * 0.95, (
        f"Expected >=5% reduction, naive={naive_annual:.3f}, "
        f"corrected={corrected:.3f}"
    )


# ── ST-05: Brier score decomposition (Murphy 1973) ────────────────


def test_brier_decomposition_sums_to_brier():
    """reliability - resolution + uncertainty ≈ brier_score."""
    import random

    random.seed(42)
    probs = [random.random() for _ in range(200)]
    outcomes = [1.0 if random.random() < p else 0.0 for p in probs]

    rel, res, unc = brier_decomposition(probs, outcomes, n_bins=10)
    reconstructed = rel - res + unc
    actual = brier_score(probs, outcomes)

    assert abs(reconstructed - actual) < 0.02, (
        f"Decomposition mismatch: {reconstructed:.4f} vs {actual:.4f}"
    )


def test_brier_decomposition_perfect_calibration():
    """Perfect forecaster → reliability ≈ 0."""
    import random

    random.seed(42)
    # Use a large sample so empirical frequencies converge
    probs = []
    outcomes = []
    for _ in range(5000):
        p = random.choice([0.2, 0.4, 0.6, 0.8])
        o = 1.0 if random.random() < p else 0.0
        probs.append(p)
        outcomes.append(o)

    rel, res, unc = brier_decomposition(probs, outcomes, n_bins=10)
    assert rel < 0.005, f"Expected reliability ≈ 0, got {rel:.4f}"
    assert res > 0, "Resolution should be positive for a sharp forecaster"
    assert 0.0 < unc < 0.26, f"Unexpected uncertainty: {unc:.4f}"

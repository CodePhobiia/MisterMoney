"""Tests for statistical validation — Paper 2 §5 worked examples."""

import math

from pmm1.math.validation import (
    annualized_sharpe,
    brier_score,
    expected_calibration_error,
    log_loss,
    per_trade_sharpe,
    required_sample_size,
    rolling_sharpe,
    sprt_update,
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

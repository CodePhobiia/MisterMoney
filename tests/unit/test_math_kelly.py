"""Tests for Kelly criterion module — Paper 2 §1 worked examples."""

from pmm1.math.kelly import (
    diversity_discount,
    drawdown_constrained_kelly,
    fractional_kelly,
    fractional_kelly_growth_rate,
    information_advantage,
    kelly_bet_dollars,
    kelly_fraction_auto,
    kelly_fraction_no,
    kelly_fraction_yes,
    kelly_growth_rate,
    shrinkage_factor,
)


def test_kelly_yes_paper2_example():
    """Paper 2: market 60%, estimate 75% → f* = 0.15/0.40 = 37.5%."""
    f = kelly_fraction_yes(0.75, 0.60)
    assert abs(f - 0.375) < 0.001


def test_kelly_no_basic():
    """NO bet: market 60%, estimate 40% → f*_NO = 0.20/0.60 = 33.3%."""
    f = kelly_fraction_no(0.40, 0.60)
    assert abs(f - 1 / 3) < 0.001


def test_kelly_no_edge():
    """No edge: market = estimate → f* = 0."""
    f = kelly_fraction_yes(0.50, 0.50)
    assert f == 0.0


def test_kelly_wrong_side():
    """Wrong side YES: estimate < market → f*_YES = 0."""
    f = kelly_fraction_yes(0.40, 0.60)
    assert f == 0.0


def test_kelly_auto_yes():
    """Auto-detect YES side."""
    side, f = kelly_fraction_auto(0.75, 0.60)
    assert side == "YES"
    assert abs(f - 0.375) < 0.001


def test_kelly_auto_no():
    """Auto-detect NO side."""
    side, f = kelly_fraction_auto(0.40, 0.60)
    assert side == "NO"
    assert abs(f - 1 / 3) < 0.001


def test_kelly_auto_no_trade():
    """No trade when probabilities match."""
    side, f = kelly_fraction_auto(0.50, 0.50)
    assert side == "NO_TRADE"
    assert f == 0.0


def test_fractional_kelly_half():
    """Paper 2: half-Kelly captures 75% growth, 25% variance."""
    side, f = fractional_kelly(0.75, 0.60, lambda_frac=0.5)
    assert side == "YES"
    # 0.5 * 0.375 = 0.1875
    assert abs(f - 0.1875) < 0.001


def test_fractional_kelly_quarter():
    """Quarter-Kelly (default)."""
    side, f = fractional_kelly(0.75, 0.60, lambda_frac=0.25)
    assert side == "YES"
    # 0.25 * 0.375 = 0.09375
    assert abs(f - 0.09375) < 0.001


def test_kelly_growth_rate_kl_divergence():
    """Growth rate = KL divergence D_KL(p || p_m)."""
    g = kelly_growth_rate(0.75, 0.60)
    # D_KL = 0.75 * ln(0.75/0.60) + 0.25 * ln(0.25/0.40)
    import math

    expected = 0.75 * math.log(0.75 / 0.60) + 0.25 * math.log(0.25 / 0.40)
    assert abs(g - expected) < 0.0001


def test_kelly_growth_rate_small_edge():
    """For small edges near 50%: g* ≈ 2δ²."""
    g = kelly_growth_rate(0.55, 0.50)
    approx = 2 * (0.05) ** 2
    # Should be close but not exact (approximation)
    assert abs(g - approx) < 0.001


def test_fractional_kelly_growth_rate():
    """g(λ) ≈ g* × λ(2-λ). Half-Kelly: 75% of optimal."""
    g_full = kelly_growth_rate(0.75, 0.60)
    g_half = fractional_kelly_growth_rate(0.75, 0.60, 0.5)
    # λ(2-λ) = 0.5 * 1.5 = 0.75
    assert abs(g_half / g_full - 0.75) < 0.001


def test_kelly_bet_dollars_paper2():
    """Paper 2 table: $1000 bankroll, market 50%, estimate 60%,
    half-Kelly → f*=0.20, sized=0.10 → $100.
    Need max_position_nav=0.20 to avoid 5% default cap."""
    side, amount = kelly_bet_dollars(
        p_true=0.60,
        p_market=0.50,
        nav=1000.0,
        lambda_frac=0.5,
        adverse_selection_lambda=1.0,
        min_edge=0.03,
        max_position_nav=0.20,
    )
    assert side == "YES"
    assert abs(amount - 100.0) < 1.0


def test_kelly_bet_below_min_edge():
    """Below minimum edge → NO_TRADE."""
    side, amount = kelly_bet_dollars(
        p_true=0.52,
        p_market=0.50,
        nav=1000.0,
        min_edge=0.03,
    )
    assert side == "NO_TRADE"
    assert amount == 0.0


def test_kelly_bet_adverse_selection_discount():
    """Adverse selection λ=0.4 reduces effective edge."""
    # Raw edge = 15%, adjusted = 6%, above 3% min
    side, amount = kelly_bet_dollars(
        p_true=0.65,
        p_market=0.50,
        nav=1000.0,
        lambda_frac=0.25,
        adverse_selection_lambda=0.4,
        min_edge=0.03,
    )
    assert side == "YES"
    assert amount > 0
    # Compare to no discount
    _, amount_full = kelly_bet_dollars(
        p_true=0.65,
        p_market=0.50,
        nav=1000.0,
        lambda_frac=0.25,
        adverse_selection_lambda=1.0,
        min_edge=0.03,
    )
    assert amount < amount_full


def test_kelly_bet_max_position_cap():
    """Position capped at max_position_nav."""
    side, amount = kelly_bet_dollars(
        p_true=0.95,
        p_market=0.50,
        nav=1000.0,
        lambda_frac=0.5,
        adverse_selection_lambda=1.0,
        min_edge=0.03,
        max_position_nav=0.05,
    )
    assert amount <= 50.0  # 5% of $1000


def test_kelly_boundary_prices():
    """Edge cases: market at 0 or 1."""
    assert kelly_fraction_yes(0.50, 0.0) == 0.0
    assert kelly_fraction_yes(0.50, 1.0) == 0.0
    assert kelly_fraction_no(0.50, 0.0) == 0.0
    assert kelly_fraction_no(0.50, 1.0) == 0.0


def test_multi_bet_kelly_adjustment():
    """Correlation adjustment reduces position size."""
    from pmm1.math.kelly import multi_bet_kelly_adjustment

    f = 0.10
    # Single position: no change
    assert multi_bet_kelly_adjustment(f, 1) == f
    # 10 positions, rho=0.05: f / 1.45
    adj = multi_bet_kelly_adjustment(f, 10, rho=0.05)
    assert 0.06 < adj < 0.08
    # 10 positions, rho=0.20: f / 2.8
    adj2 = multi_bet_kelly_adjustment(f, 10, rho=0.20)
    assert adj2 < adj
    # Zero correlation: no change
    assert multi_bet_kelly_adjustment(f, 10, rho=0.0) == f


# ── KP-02: Baker-McHale shrinkage ──


def test_shrinkage_low_data():
    """Low data: n=5, sigma=0.3, edge=0.05 -> shrinkage < 0.5 (low confidence)."""
    s = shrinkage_factor(edge=0.05, sigma_p=0.3, n_obs=5)
    assert s < 0.5


def test_shrinkage_high_data():
    """n=500, sigma=0.05, edge=0.05 -> shrinkage > 0.8 (high confidence)."""
    s = shrinkage_factor(edge=0.05, sigma_p=0.05, n_obs=500)
    assert s > 0.8


def test_shrinkage_edge_zero():
    """Zero edge -> floor of 0.1."""
    s = shrinkage_factor(edge=0.0, sigma_p=0.1, n_obs=100)
    assert s == 0.1


def test_shrinkage_too_few_obs():
    """n < 5 -> floor of 0.1."""
    s = shrinkage_factor(edge=0.05, sigma_p=0.1, n_obs=3)
    assert s == 0.1


# ── KP-04: Drawdown-constrained Kelly ──


def test_drawdown_constrained_basic():
    """dd=0.01, threshold=0.025, sigma=0.30 -> f_max in (0, 1)."""
    f = drawdown_constrained_kelly(current_dd=0.01, tier_threshold=0.025, sigma_portfolio=0.30)
    assert 0.0 < f < 1.0


def test_drawdown_no_budget():
    """dd=0.024, threshold=0.025, sigma=0.30 -> very small f_max (budget nearly exhausted)."""
    f = drawdown_constrained_kelly(current_dd=0.024, tier_threshold=0.025, sigma_portfolio=0.30)
    assert f < 0.2


# ── KP-05: Diversity discount ──


def test_diversity_discount_high():
    """diversity=0.15 (max) -> discount=0.2 (most discounted)."""
    d = diversity_discount(0.15)
    assert abs(d - 0.2) < 0.001


def test_diversity_discount_zero():
    """diversity=0 -> discount=1.0 (no discount)."""
    d = diversity_discount(0.0)
    assert abs(d - 1.0) < 0.001


def test_diversity_discount_half():
    """diversity = 0.075 (half of max) -> discount = 0.6."""
    d = diversity_discount(0.075)
    assert abs(d - 0.6) < 0.001


# ── KP-10: Information advantage ──


def test_information_advantage_positive():
    """Model better than market -> positive advantage."""

    # Model predicts 0.8, market 0.5, outcome YES (1.0) — model is better
    n = 20
    model_probs = [0.8] * n
    market_probs = [0.5] * n
    outcomes = [1.0] * n
    ia = information_advantage(model_probs, market_probs, outcomes)
    assert ia > 0


def test_information_advantage_even():
    """Same predictions -> near zero."""
    n = 20
    probs = [0.6] * n
    outcomes = [1.0] * n
    ia = information_advantage(probs, probs, outcomes)
    assert abs(ia) < 0.001


def test_information_advantage_too_few():
    """Fewer than 10 observations -> 0.0."""
    ia = information_advantage([0.8] * 5, [0.5] * 5, [1.0] * 5)
    assert ia == 0.0

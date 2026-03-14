"""Tests for ensemble methods — Paper 2 §4 worked examples."""

from pmm1.math.ensemble import (
    bayesian_model_weights,
    ensemble_diversity,
    inverse_brier_weights,
    linear_pool,
    log_pool,
    update_weights_mwu,
)


def test_log_pool_paper2_example():
    """Paper 2: Claude 0.65, GPT 0.72, Gemini 0.58 → log-pool ≈ 0.652."""
    result = log_pool([0.65, 0.72, 0.58])
    assert abs(result - 0.652) < 0.01


def test_linear_pool_paper2_example():
    """Paper 2: simple average → 0.650."""
    result = linear_pool([0.65, 0.72, 0.58])
    assert abs(result - 0.650) < 0.001


def test_log_pool_handles_extremes():
    """Log pooling properly handles extreme probabilities.

    Paper 2: if one model assigns 99.9% and another 50%,
    log pooling yields ~97%, while linear gives only ~75%.
    """
    log_result = log_pool([0.999, 0.50])
    linear_result = linear_pool([0.999, 0.50])
    assert log_result > linear_result
    assert log_result > 0.90  # Much more influenced by the confident model
    assert abs(linear_result - 0.7495) < 0.01


def test_log_pool_with_weights():
    """Weighted log-odds pooling."""
    # Weight Claude 2x more
    result = log_pool([0.65, 0.72, 0.58], weights=[2.0, 1.0, 1.0])
    # Claude's 0.65 should pull the result toward 0.65
    assert 0.63 < result < 0.68


def test_log_pool_single():
    """Single model → identity."""
    assert abs(log_pool([0.70]) - 0.70) < 1e-6


def test_log_pool_empty():
    """Empty → 0.5."""
    assert log_pool([]) == 0.5


def test_inverse_brier_weights():
    """Paper 2: Claude 0.18, GPT 0.15, Gemini 0.22."""
    weights = inverse_brier_weights([0.18, 0.15, 0.22])
    assert len(weights) == 3
    # GPT has best (lowest) Brier → highest weight
    assert weights[1] > weights[0]
    assert weights[1] > weights[2]
    # Claude better than Gemini
    assert weights[0] > weights[2]
    # Weights sum to 1
    assert abs(sum(weights) - 1.0) < 1e-6


def test_inverse_brier_weighted_ensemble():
    """Paper 2 table: inverse-Brier weighted → 0.659."""
    weights = inverse_brier_weights([0.18, 0.15, 0.22])
    result = linear_pool([0.65, 0.72, 0.58], weights=weights)
    assert abs(result - 0.659) < 0.01


def test_mwu_basic():
    """MWU updates weights toward better-performing models."""
    weights = [1 / 3, 1 / 3, 1 / 3]
    # Model 0 had low loss, model 2 had high loss
    losses = [0.01, 0.10, 0.50]
    updated = update_weights_mwu(weights, losses, eta=0.1)
    assert updated[0] > updated[1]  # Model 0 gained weight
    assert updated[1] > updated[2]  # Model 2 lost weight
    assert abs(sum(updated) - 1.0) < 1e-6


def test_mwu_min_weight_floor():
    """MWU enforces minimum weight to prevent collapse."""
    weights = [0.8, 0.1, 0.1]
    # Heavy loss on model 0
    losses = [1.0, 0.01, 0.01]
    updated = update_weights_mwu(
        weights, losses, eta=1.0, min_weight=0.05,
    )
    # Model 0 should be at or above floor
    assert updated[0] >= 0.05
    assert abs(sum(updated) - 1.0) < 1e-6


def test_mwu_regret_bound():
    """Paper 2: for 3 LLMs and 100 rounds, per-question
    regret ≈ 0.074 with optimal η."""
    import math

    k = 3
    t = 100
    _eta_optimal = math.sqrt(2 * math.log(k) / t)
    regret_bound = math.sqrt(t * math.log(k) / 2)
    per_question = regret_bound / t
    assert abs(per_question - 0.074) < 0.01


def test_bayesian_model_weights():
    """BMA weights models by cumulative log-loss."""
    # Model 0 has much lower cumulative log-loss (better)
    log_losses = [5.0, 10.0, 15.0]
    weights = bayesian_model_weights(log_losses)
    assert weights[0] > weights[1]
    assert weights[1] > weights[2]
    assert abs(sum(weights) - 1.0) < 1e-6


def test_bayesian_model_weights_equal():
    """Equal log-losses → equal weights."""
    weights = bayesian_model_weights([10.0, 10.0, 10.0])
    for w in weights:
        assert abs(w - 1 / 3) < 1e-6


def test_ensemble_diversity():
    """Paper 2: diversity measures disagreement among models.

    With pairwise ρ=0.5 among 3 models, diversity reduces
    variance by 33% vs 67% with uncorrelated models.
    """
    # High diversity (disagreement)
    div_high = ensemble_diversity([0.30, 0.50, 0.70])
    # Low diversity (agreement)
    div_low = ensemble_diversity([0.49, 0.50, 0.51])
    assert div_high > div_low
    assert div_high > 0


def test_ensemble_diversity_single():
    """Single model → zero diversity."""
    assert ensemble_diversity([0.60]) == 0.0


def test_log_pool_symmetric():
    """Log pooling is symmetric: order doesn't matter."""
    r1 = log_pool([0.30, 0.70])
    r2 = log_pool([0.70, 0.30])
    assert abs(r1 - r2) < 1e-6
    # Equal weights → should be near 50% (log-odds of 30% and 70% cancel)
    assert abs(r1 - 0.50) < 1e-6


def test_mwu_adaptive_eta():
    """MWU with round_number uses optimal eta."""
    weights = [0.5, 0.5]
    losses = [0.1, 0.5]
    # With round_number, eta is computed optimally
    updated = update_weights_mwu(
        weights, losses, round_number=100,
    )
    assert updated[0] > updated[1]
    assert abs(sum(updated) - 1.0) < 1e-6

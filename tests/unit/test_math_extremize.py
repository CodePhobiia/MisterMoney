"""Tests for extremization module — Paper 2 §3."""

from pmm1.math.extremize import (
    apply_isotonic,
    extremize,
    extremize_batch,
    fit_alpha,
    fit_gamma_tau,
    fit_isotonic,
    generalized_calibration,
    logit,
    sigmoid,
)


def test_extremize_identity():
    """α=1.0 is identity: no change."""
    assert abs(extremize(0.60, alpha=1.0) - 0.60) < 1e-6
    assert abs(extremize(0.30, alpha=1.0) - 0.30) < 1e-6


def test_extremize_pushes_away_from_50():
    """α>1 pushes predictions away from 50%."""
    p = 0.60
    ext = extremize(p, alpha=1.73)
    assert ext > p  # pushed further from 50%

    p = 0.40
    ext = extremize(p, alpha=1.73)
    assert ext < p  # pushed further from 50% (below)


def test_extremize_preserves_50():
    """50% is fixed point of extremization for any α."""
    assert abs(extremize(0.50, alpha=1.73) - 0.50) < 1e-6
    assert abs(extremize(0.50, alpha=3.00) - 0.50) < 1e-6


def test_extremize_symmetric():
    """extremize(p) + extremize(1-p) = 1."""
    for p in [0.3, 0.4, 0.6, 0.7, 0.8, 0.9]:
        ext_p = extremize(p, 1.73)
        ext_q = extremize(1.0 - p, 1.73)
        assert abs(ext_p + ext_q - 1.0) < 1e-6


def test_extremize_default_alpha():
    """Default α = √3 ≈ 1.73 (Neyman & Roughgarden optimal)."""
    # p=0.60 with α=1.73 should push to ~0.634
    ext = extremize(0.60)
    assert ext > 0.60
    assert ext < 0.70  # shouldn't overshoot wildly


def test_extremize_batch():
    """Batch extremization."""
    probs = [0.40, 0.50, 0.60, 0.70]
    exts = extremize_batch(probs, alpha=1.73)
    assert len(exts) == 4
    assert exts[0] < 0.40  # pushed below
    assert abs(exts[1] - 0.50) < 1e-6  # 50% unchanged
    assert exts[2] > 0.60  # pushed above
    assert exts[3] > 0.70  # pushed above


def test_fit_alpha_on_calibrated_data():
    """If predictions are already calibrated, α should be near 1.0."""
    probs = [0.2, 0.4, 0.6, 0.8] * 25  # 100 samples
    outcomes = []
    import random
    random.seed(42)
    for p in probs:
        outcomes.append(1.0 if random.random() < p else 0.0)

    alpha = fit_alpha(probs, outcomes)
    # Should be near 1.0 (±0.5) since data is already calibrated
    assert 0.5 <= alpha <= 2.0


def test_fit_alpha_on_hedged_data():
    """If predictions are hedged toward 50%, α should be > 1."""
    # Simulated hedged predictions: true probabilities are extreme,
    # but model outputs are pulled toward 50%
    true_probs = [0.1, 0.2, 0.3, 0.7, 0.8, 0.9] * 20
    hedged_probs = [0.5 + 0.5 * (p - 0.5) for p in true_probs]  # 50% hedging
    import random
    random.seed(42)
    outcomes = [1.0 if random.random() < tp else 0.0 for tp in true_probs]

    alpha = fit_alpha(hedged_probs, outcomes)
    # Should find α > 1 to correct the hedging
    assert alpha > 1.0


def test_logit_sigmoid_roundtrip():
    """logit and sigmoid are inverse functions."""
    for p in [0.1, 0.3, 0.5, 0.7, 0.9]:
        assert abs(sigmoid(logit(p)) - p) < 1e-6


def test_generalized_calibration_identity():
    """γ=1, τ=0 is identity."""
    for p in [0.2, 0.5, 0.8]:
        assert abs(generalized_calibration(p, gamma=1.0, tau=0.0) - p) < 1e-6


def test_generalized_calibration_extremization():
    """γ>1, τ=0 is equivalent to extremization."""
    p = 0.60
    ext = extremize(p, alpha=1.73)
    gen = generalized_calibration(p, gamma=1.73, tau=0.0)
    assert abs(ext - gen) < 1e-6


def test_generalized_calibration_base_rate():
    """τ shifts the base rate."""
    # Positive τ shifts toward YES
    p = 0.50
    cal = generalized_calibration(p, gamma=1.0, tau=0.5)
    assert cal > 0.50

    # Negative τ shifts toward NO
    cal = generalized_calibration(p, gamma=1.0, tau=-0.5)
    assert cal < 0.50


def test_extremize_clipping():
    """Extreme inputs don't cause numerical issues."""
    assert 0 < extremize(0.001) < 0.01
    assert 0.99 < extremize(0.999) < 1.0
    assert 0 < extremize(1e-10) < 1.0
    assert 0 < extremize(1.0 - 1e-10) < 1.0


# ------------------------------------------------------------------
# Phase 4B: LLM-09 (Isotonic) + LLM-10 (Weighted fit_gamma_tau)
# ------------------------------------------------------------------


def test_isotonic_monotonic():
    """LLM-09: Isotonic output is monotonically non-decreasing."""
    import random
    random.seed(123)

    # Generate noisy calibration data
    probs = sorted([random.random() for _ in range(200)])
    outcomes = [1.0 if random.random() < p else 0.0 for p in probs]

    lookup = fit_isotonic(probs, outcomes)

    # The calibrated values must be monotonically non-decreasing
    for i in range(len(lookup) - 1):
        assert lookup[i][1] <= lookup[i + 1][1] + 1e-10, (
            f"Monotonicity violated at index {i}: "
            f"{lookup[i][1]} > {lookup[i + 1][1]}"
        )


def test_isotonic_perfect():
    """LLM-09: Perfect calibration -> outputs close to inputs."""
    # Data where outcome exactly matches probability bins
    probs = [0.0] * 50 + [1.0] * 50
    outcomes = [0.0] * 50 + [1.0] * 50

    lookup = fit_isotonic(probs, outcomes)

    # For perfectly calibrated data, calibrated values should match
    for p_in, p_out in lookup:
        assert abs(p_in - p_out) < 1e-6, (
            f"Perfect calibration should not change: "
            f"input={p_in}, output={p_out}"
        )


def test_fit_gamma_tau_with_weights():
    """LLM-10: Weighted fit produces different result than unweighted."""
    import random
    random.seed(55)

    probs = [0.5 + 0.3 * (random.random() - 0.5) for _ in range(100)]
    outcomes = [1.0 if random.random() < p else 0.0 for p in probs]

    # Uniform weights = same as unweighted
    gamma_unw, tau_unw = fit_gamma_tau(probs, outcomes)
    gamma_uw, tau_uw = fit_gamma_tau(
        probs, outcomes, weights=[1.0] * 100,
    )
    assert abs(gamma_unw - gamma_uw) < 1e-3
    assert abs(tau_unw - tau_uw) < 1e-3

    # Heavily weight the first half (which may have different distribution)
    # vs the second half
    weights_front = [10.0] * 50 + [0.01] * 50
    weights_back = [0.01] * 50 + [10.0] * 50

    gamma_f, tau_f = fit_gamma_tau(probs, outcomes, weights=weights_front)
    gamma_b, tau_b = fit_gamma_tau(probs, outcomes, weights=weights_back)

    # With different weighting, at least one parameter should differ
    param_diff = abs(gamma_f - gamma_b) + abs(tau_f - tau_b)
    assert param_diff > 0.001, (
        f"Extreme weight differences should produce different fits: "
        f"front=({gamma_f}, {tau_f}), back=({gamma_b}, {tau_b})"
    )


def test_apply_isotonic_interpolation():
    """LLM-09: Values between lookup points are interpolated."""
    # Simple lookup: two points
    lookup = [(0.2, 0.3), (0.8, 0.7)]

    # At the exact points
    assert abs(apply_isotonic(0.2, lookup) - 0.3) < 1e-6
    assert abs(apply_isotonic(0.8, lookup) - 0.7) < 1e-6

    # Midpoint should interpolate
    mid = apply_isotonic(0.5, lookup)
    assert abs(mid - 0.5) < 1e-6  # Linear interp: 0.3 + (0.5-0.2)/(0.8-0.2) * (0.7-0.3) = 0.5

    # Below range -> first value
    assert abs(apply_isotonic(0.1, lookup) - 0.3) < 1e-6

    # Above range -> last value
    assert abs(apply_isotonic(0.9, lookup) - 0.7) < 1e-6

    # Quarter point
    quarter = apply_isotonic(0.35, lookup)
    expected = 0.3 + (0.35 - 0.2) / (0.8 - 0.2) * (0.7 - 0.3)
    assert abs(quarter - expected) < 1e-6

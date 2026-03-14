"""Tests for ReasonerMemory."""

import os
import random
import tempfile

from pmm1.strategy.reasoner_memory import ReasonerMemory


def test_empty_memory():
    mem = ReasonerMemory(persist_path="/tmp/test_mem_empty.json", min_for_calibration=5)
    assert not mem.is_calibrated
    assert mem.get_brier() == 1.0
    assert mem.format_for_prompt() == ""


def test_record_and_brier():
    path = os.path.join(tempfile.mkdtemp(), "mem.json")
    mem = ReasonerMemory(persist_path=path, min_for_calibration=3)

    # Perfect predictions
    mem.record_resolution("m1", 1.0, 0.9, 0.9, 0.9, 0.1, "politics")
    mem.record_resolution("m2", 0.0, 0.1, 0.1, 0.1, 0.1, "politics")
    mem.record_resolution("m3", 1.0, 0.8, 0.8, 0.8, 0.1, "sports")

    assert mem.is_calibrated
    assert mem.get_brier() < 0.05


def test_systematic_bias():
    path = os.path.join(tempfile.mkdtemp(), "mem.json")
    mem = ReasonerMemory(persist_path=path, min_for_calibration=3)

    # Always overestimates
    for i in range(10):
        mem.record_resolution(f"m{i}", 0.0, 0.7, 0.7, 0.7, 0.1, "test")

    bias = mem.get_systematic_bias()
    assert bias > 0.5  # Overestimates by a lot


def test_optimal_alpha():
    path = os.path.join(tempfile.mkdtemp(), "mem.json")
    mem = ReasonerMemory(persist_path=path, min_for_calibration=5)

    import random
    random.seed(42)
    for i in range(100):
        true_p = random.choice([0.2, 0.8])
        outcome = 1.0 if random.random() < true_p else 0.0
        hedged = 0.5 + 0.5 * (true_p - 0.5)
        mem.record_resolution(f"m{i}", outcome, hedged, hedged, hedged, 0.15)

    alpha = mem.get_optimal_alpha()
    assert alpha > 1.0  # Should learn to de-hedge


def test_format_for_prompt():
    path = os.path.join(tempfile.mkdtemp(), "mem.json")
    mem = ReasonerMemory(persist_path=path, min_for_calibration=3)

    for i in range(10):
        mem.record_resolution(f"m{i}", float(i % 2), 0.5, 0.5, 0.5, 0.2, "test")

    prompt = mem.format_for_prompt()
    assert "CALIBRATION HISTORY" in prompt
    assert "Brier score" in prompt


def test_save_load():
    path = os.path.join(tempfile.mkdtemp(), "mem.json")
    mem = ReasonerMemory(persist_path=path, min_for_calibration=2)
    mem.record_resolution("m1", 1.0, 0.8, 0.8, 0.8, 0.1)
    mem.record_resolution("m2", 0.0, 0.2, 0.2, 0.2, 0.1)

    mem2 = ReasonerMemory(persist_path=path, min_for_calibration=2)
    assert len(mem2._resolved) == 2
    assert mem2.is_calibrated


def test_brier_by_category():
    path = os.path.join(tempfile.mkdtemp(), "mem.json")
    mem = ReasonerMemory(persist_path=path, min_for_calibration=3)

    for i in range(15):
        mem.record_resolution(f"s{i}", float(i % 2), 0.5, 0.5, 0.5, 0.2, "sports")
        mem.record_resolution(f"p{i}", float(i % 2), 0.6, 0.6, 0.6, 0.2, "politics")

    by_cat = mem.get_brier_by_category()
    assert "sports" in by_cat
    assert "politics" in by_cat


def test_1000_entries_survive_save_load():
    """F04: 1000 entries survive save/load round-trip (retention is 5000, not 500)."""
    path = os.path.join(tempfile.mkdtemp(), "mem.json")
    mem = ReasonerMemory(persist_path=path, min_for_calibration=2)

    for i in range(1000):
        mem.record_resolution(
            f"m{i}", float(i % 2), 0.5, 0.5, 0.5, 0.2, "test",
        )

    # Reload from disk
    mem2 = ReasonerMemory(persist_path=path, min_for_calibration=2)
    assert len(mem2._resolved) == 1000


def test_get_summary_default_alpha():
    """F12/F15: Empty ReasonerMemory returns optimal_alpha == 1.3."""
    path = os.path.join(tempfile.mkdtemp(), "mem_alpha.json")
    mem = ReasonerMemory(persist_path=path, min_for_calibration=50)
    summary = mem.get_summary()
    assert summary["optimal_alpha"] == 1.3


def test_calibration_needs_200_samples():
    """ML-M3: verify is_calibrated=False at 150 samples (default min=200)."""
    path = os.path.join(tempfile.mkdtemp(), "mem_200.json")
    mem = ReasonerMemory(persist_path=path)  # uses default min_for_calibration=200

    for i in range(150):
        mem.record_resolution(
            f"m{i}", float(i % 2), 0.5, 0.5, 0.5, 0.2, "test",
        )

    assert not mem.is_calibrated, (
        f"Expected is_calibrated=False at 150 samples, "
        f"but got True (min_for_calibration={mem.min_for_calibration})"
    )


# ------------------------------------------------------------------
# Phase 4B: LLM-02, LLM-04, LLM-07, LLM-09, LLM-10 tests
# ------------------------------------------------------------------


def test_category_gamma_tau():
    """LLM-02: 50+ sports resolutions -> per-category gamma/tau."""
    path = os.path.join(tempfile.mkdtemp(), "mem_cat_gt.json")
    mem = ReasonerMemory(persist_path=path, min_for_calibration=200)

    random.seed(99)
    for i in range(60):
        # Sports: hedged predictions
        true_p = random.choice([0.2, 0.8])
        outcome = 1.0 if random.random() < true_p else 0.0
        hedged = 0.5 + 0.4 * (true_p - 0.5)
        mem.record_resolution(
            f"sport{i}", outcome, hedged, hedged, hedged, 0.15,
            category="sports", p_ensemble=hedged,
        )

    # With only 60 sports samples, category fit should trigger (>= 50)
    gamma, tau = mem.get_optimal_gamma_tau(category="sports")
    # Should find gamma != default 1.3 (data is hedged, needs de-hedging)
    assert isinstance(gamma, float)
    assert isinstance(tau, float)
    # The gamma should push toward extremization since data is hedged
    assert gamma >= 0.5


def test_category_blend_weight():
    """LLM-02: Category with good Brier -> higher weight."""
    path = os.path.join(tempfile.mkdtemp(), "mem_cat_blend.json")
    mem = ReasonerMemory(persist_path=path, min_for_calibration=3)

    random.seed(42)
    # Politics: great predictions (close to outcome)
    for i in range(15):
        outcome = float(i % 2)
        p = 0.9 if outcome == 1.0 else 0.1
        mem.record_resolution(
            f"p{i}", outcome, p, p, p, 0.1, category="politics",
        )

    # Economics: bad predictions (always 0.5)
    for i in range(15):
        outcome = float(i % 2)
        mem.record_resolution(
            f"e{i}", outcome, 0.5, 0.5, 0.5, 0.2, category="economics",
        )

    w_politics = mem.get_category_blend_weight("politics")
    w_economics = mem.get_category_blend_weight("economics")

    # Politics has better Brier -> should get higher blend weight
    assert w_politics > w_economics
    assert 0.10 <= w_politics <= 0.50
    assert 0.10 <= w_economics <= 0.50


def test_category_fallback_to_global():
    """LLM-02: Category with < 50 samples -> falls back to global fit."""
    path = os.path.join(tempfile.mkdtemp(), "mem_cat_fallback.json")
    mem = ReasonerMemory(persist_path=path, min_for_calibration=5)

    random.seed(77)
    # Only 10 sports samples (< 50 threshold)
    for i in range(10):
        outcome = float(i % 2)
        mem.record_resolution(
            f"s{i}", outcome, 0.5, 0.5, 0.5, 0.2,
            category="sports", p_ensemble=0.5,
        )

    # But 200+ total
    for i in range(200):
        true_p = random.choice([0.3, 0.7])
        outcome = 1.0 if random.random() < true_p else 0.0
        hedged = 0.5 + 0.5 * (true_p - 0.5)
        mem.record_resolution(
            f"g{i}", outcome, hedged, hedged, hedged, 0.15,
            category="general", p_ensemble=hedged,
        )

    # Category fit should fall back to global (< 50 sports samples)
    gamma_cat, tau_cat = mem.get_optimal_gamma_tau(category="sports")
    gamma_global, tau_global = mem.get_optimal_gamma_tau(category="")

    # Should get the global result since sports < 50
    assert gamma_cat == gamma_global
    assert tau_cat == tau_global


def test_diversity_adjusted_alpha():
    """LLM-04: High diversity -> alpha closer to base_alpha."""
    path = os.path.join(tempfile.mkdtemp(), "mem_div.json")
    mem = ReasonerMemory(persist_path=path, min_for_calibration=5)

    # Zero diversity -> alpha should be 1.0
    alpha_zero = mem.get_diversity_adjusted_alpha(
        base_alpha=1.3, diversity=0.0,
    )
    assert abs(alpha_zero - 1.0) < 1e-6

    # Max diversity (0.15) -> alpha should be base_alpha
    alpha_max = mem.get_diversity_adjusted_alpha(
        base_alpha=1.3, diversity=0.15,
    )
    assert abs(alpha_max - 1.3) < 1e-6

    # Medium diversity -> alpha between 1.0 and base_alpha
    alpha_mid = mem.get_diversity_adjusted_alpha(
        base_alpha=1.3, diversity=0.075,
    )
    assert 1.0 < alpha_mid < 1.3

    # Higher diversity => higher alpha (more extremization)
    alpha_low = mem.get_diversity_adjusted_alpha(
        base_alpha=2.0, diversity=0.03,
    )
    alpha_high = mem.get_diversity_adjusted_alpha(
        base_alpha=2.0, diversity=0.12,
    )
    assert alpha_high > alpha_low

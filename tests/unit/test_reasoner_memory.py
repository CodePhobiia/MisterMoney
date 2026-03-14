"""Tests for ReasonerMemory."""

import os
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

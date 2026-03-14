"""Tests for CL-05: TradePostMortem loss classification."""

from __future__ import annotations

import os
import tempfile

from pmm1.analytics.post_mortem import LossCategory, TradePostMortem


def test_classify_profitable():
    """pnl > 0 should classify as PROFITABLE."""
    pm = TradePostMortem()
    result = pm.classify_fill(
        pnl=0.05,
        spread_capture=0.02,
        adverse_selection_5s=-0.01,
    )
    assert result == LossCategory.PROFITABLE


def test_classify_adverse_selection():
    """Large adverse selection cost should classify as ADVERSE_SELECTION."""
    pm = TradePostMortem()
    result = pm.classify_fill(
        pnl=-0.10,
        spread_capture=0.02,
        adverse_selection_5s=-0.08,  # AS exceeds half of spread_capture
    )
    assert result == LossCategory.ADVERSE_SELECTION


def test_classify_llm_error():
    """FV error > 15% should classify as LLM_ERROR."""
    pm = TradePostMortem()
    result = pm.classify_fill(
        pnl=-0.10,
        spread_capture=0.02,
        adverse_selection_5s=-0.005,  # Small AS, won't trigger AS category
        fair_value_error=0.25,  # 25 pp error
    )
    assert result == LossCategory.LLM_ERROR


def test_classify_carry_loss():
    """Hold time > 4h should classify as CARRY_LOSS."""
    pm = TradePostMortem()
    result = pm.classify_fill(
        pnl=-0.05,
        spread_capture=0.02,
        adverse_selection_5s=-0.005,  # Small AS
        fair_value_error=0.05,  # Small FV error
        hold_time_hours=6.0,
    )
    assert result == LossCategory.CARRY_LOSS


def test_format_for_prompt():
    """After 20+ classified fills with losses, format_for_prompt returns non-empty."""
    pm = TradePostMortem()
    # Add 15 losing fills (adverse selection)
    for _ in range(15):
        pm.classify_fill(
            pnl=-0.10,
            spread_capture=0.02,
            adverse_selection_5s=-0.08,
        )
    # Add 5 profitable fills
    for _ in range(5):
        pm.classify_fill(
            pnl=0.05,
            spread_capture=0.02,
            adverse_selection_5s=-0.01,
        )
    result = pm.format_for_prompt()
    assert result != ""
    assert "LOSS ATTRIBUTION" in result
    assert "adverse_selection" in result


def test_format_for_prompt_empty_when_few():
    """format_for_prompt returns empty string when fewer than 10 losses."""
    pm = TradePostMortem()
    for _ in range(5):
        pm.classify_fill(pnl=-0.10, spread_capture=0.02, adverse_selection_5s=-0.08)
    assert pm.format_for_prompt() == ""


def test_save_load_roundtrip():
    """Save and load should preserve counts and amounts."""
    pm = TradePostMortem()
    for _ in range(10):
        pm.classify_fill(pnl=-0.10, spread_capture=0.02, adverse_selection_5s=-0.08)
    for _ in range(5):
        pm.classify_fill(pnl=0.05, spread_capture=0.02, adverse_selection_5s=-0.01)

    path = os.path.join(tempfile.mkdtemp(), "post_mortem.json")
    pm.save(path)

    pm2 = TradePostMortem()
    pm2.load(path)

    assert pm2._total_classified == 15
    assert pm2._counts["adverse_selection"] == 10
    assert pm2._counts["profitable"] == 5


def test_get_summary():
    """get_summary should return structured data."""
    pm = TradePostMortem()
    pm.classify_fill(pnl=-0.10, spread_capture=0.02, adverse_selection_5s=-0.08)
    pm.classify_fill(pnl=0.05, spread_capture=0.02, adverse_selection_5s=-0.01)

    summary = pm.get_summary()
    assert summary["total_classified"] == 2
    assert "counts" in summary
    assert "amounts" in summary


def test_load_nonexistent_is_noop():
    """Loading from a nonexistent file should not raise."""
    pm = TradePostMortem()
    pm.load("/tmp/nonexistent_post_mortem_file_12345.json")
    assert pm._total_classified == 0

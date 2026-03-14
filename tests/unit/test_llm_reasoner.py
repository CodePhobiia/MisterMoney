"""Tests for embedded LLM reasoner."""

import time

from pmm1.strategy.llm_reasoner import (
    LLMEstimate,
    LLMReasoner,
    ReasonerConfig,
)


def test_estimate_freshness():
    """Estimates expire after max_age."""
    est = LLMEstimate(
        condition_id="test",
        p_raw=0.60,
        p_calibrated=0.63,
        uncertainty=0.10,
        reasoning="test",
        model="test",
        generated_at=time.time(),
    )
    assert est.is_fresh

    # Simulate old estimate
    est.generated_at = time.time() - 600
    assert not est.is_fresh


def test_estimate_decay():
    """Signal decays toward market midpoint with age."""
    est = LLMEstimate(
        condition_id="test",
        p_raw=0.70,
        p_calibrated=0.73,
        uncertainty=0.10,
        reasoning="test",
        model="test",
        generated_at=time.time(),
    )
    # Fresh: should be close to p_calibrated
    decayed_fresh = est.decay_toward_market(0.50)
    assert abs(decayed_fresh - 0.73) < 0.01

    # Aged: should be closer to midpoint
    est.generated_at = time.time() - 1800  # 30 min old
    decayed_old = est.decay_toward_market(0.50)
    assert decayed_old < decayed_fresh
    assert decayed_old > 0.50  # Not fully decayed


def test_reasoner_disabled():
    """Disabled reasoner returns None for all queries."""
    config = ReasonerConfig(enabled=False)
    reasoner = LLMReasoner(config)
    assert reasoner.get_estimate("any_market") is None


def test_reasoner_cache():
    """Estimates are cached and retrievable."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    # Manually insert an estimate
    est = LLMEstimate(
        condition_id="market_1",
        p_raw=0.65,
        p_calibrated=0.68,
        uncertainty=0.12,
        reasoning="test reasoning",
        model="test",
    )
    reasoner._cache["market_1"] = est

    # Should retrieve it
    result = reasoner.get_estimate("market_1")
    assert result is not None
    assert result.p_calibrated == 0.68

    # Unknown market → None
    assert reasoner.get_estimate("unknown") is None


def test_reasoner_expired_estimate():
    """Expired estimates return None."""
    config = ReasonerConfig(
        enabled=True, auth_token="test",
        signal_max_age_s=60.0,
    )
    reasoner = LLMReasoner(config)

    est = LLMEstimate(
        condition_id="old_market",
        p_raw=0.65,
        p_calibrated=0.68,
        uncertainty=0.12,
        reasoning="test",
        model="test",
        generated_at=time.time() - 120,  # 2 min old, limit 1 min
    )
    reasoner._cache["old_market"] = est

    assert reasoner.get_estimate("old_market") is None


def test_blended_fair_value_no_signal():
    """No LLM signal → returns book midpoint."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    fv, meta = reasoner.get_blended_fair_value(
        "no_signal", book_midpoint=0.55,
    )
    assert fv == 0.55
    assert not meta["llm_used"]


def test_blended_fair_value_with_signal():
    """Paper 1: 67% market / 33% AI blend."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    est = LLMEstimate(
        condition_id="market_1",
        p_raw=0.70,
        p_calibrated=0.73,
        uncertainty=0.10,
        reasoning="strong signal",
        model="test",
    )
    reasoner._cache["market_1"] = est

    fv, meta = reasoner.get_blended_fair_value(
        "market_1", book_midpoint=0.55, blend_weight=0.33,
    )
    assert meta["llm_used"]
    # Blended: 0.67 * 0.55 + 0.33 * 0.73 ≈ 0.609
    assert 0.59 < fv < 0.62


def test_blended_fair_value_low_confidence():
    """Low confidence → falls back to midpoint."""
    config = ReasonerConfig(
        enabled=True, auth_token="test",
        min_confidence=0.70,
    )
    reasoner = LLMReasoner(config)

    est = LLMEstimate(
        condition_id="uncertain",
        p_raw=0.70,
        p_calibrated=0.73,
        uncertainty=0.40,  # confidence = 0.60 < 0.70 min
        reasoning="uncertain",
        model="test",
    )
    reasoner._cache["uncertain"] = est

    fv, meta = reasoner.get_blended_fair_value(
        "uncertain", book_midpoint=0.55,
    )
    assert fv == 0.55
    assert not meta["llm_used"]
    assert meta["miss_reason"] == "low_confidence"


def test_reasoner_status():
    """Status reports cache and call counts."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    status = reasoner.get_status()
    assert status["enabled"]
    assert status["cached_estimates"] == 0
    assert status["total_calls"] == 0


def test_config_from_env(monkeypatch: object) -> None:
    """Config loads from environment variables."""
    import os

    os.environ["PMM1_LLM_ENABLED"] = "true"
    os.environ["ANTHROPIC_OAUTH_TOKEN"] = "sk-ant-oat01-test"
    os.environ["PMM1_LLM_MODEL"] = "claude-opus-4-6-20250610"
    os.environ["PMM1_LLM_THINKING_BUDGET"] = "8000"
    os.environ["PMM1_LLM_CYCLE_INTERVAL"] = "180"

    try:
        config = ReasonerConfig.from_env()
        assert config.enabled
        assert config.auth_token == "sk-ant-oat01-test"
        assert config.thinking_budget == 8000
        assert config.cycle_interval_s == 180.0
    finally:
        for key in [
            "PMM1_LLM_ENABLED", "ANTHROPIC_OAUTH_TOKEN",
            "PMM1_LLM_MODEL", "PMM1_LLM_THINKING_BUDGET",
            "PMM1_LLM_CYCLE_INTERVAL",
        ]:
            os.environ.pop(key, None)


def test_parse_response():
    """Parses JSON from Opus response text."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    # Clean JSON
    result = reasoner._parse_response(
        '{"p_hat": 0.65, "uncertainty": 0.12, '
        '"reasoning_summary": "test"}',
    )
    assert result is not None
    assert result["p_hat"] == 0.65

    # JSON in code block
    result = reasoner._parse_response(
        'Here is my analysis:\n```json\n'
        '{"p_hat": 0.70, "uncertainty": 0.15}\n```',
    )
    assert result is not None
    assert result["p_hat"] == 0.70

    # Invalid JSON
    result = reasoner._parse_response("not json at all")
    assert result is None

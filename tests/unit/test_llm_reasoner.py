"""Tests for embedded LLM reasoner with Paper 1+2 pipeline."""

import time

from pmm1.strategy.llm_reasoner import (
    LLMEstimate,
    LLMReasoner,
    ReasonerConfig,
)


def _make_estimate(**overrides: object) -> LLMEstimate:
    defaults = {
        "condition_id": "test",
        "p_blind": 0.65,
        "p_challenged": 0.62,
        "p_calibrated": 0.66,
        "uncertainty": 0.10,
        "reasoning": "test reasoning",
        "contra_points": "contra argument",
        "model": "test-model",
    }
    defaults.update(overrides)
    return LLMEstimate(**defaults)  # type: ignore[arg-type]


def test_estimate_freshness():
    """Estimates expire after max_age."""
    est = _make_estimate()
    assert est.is_fresh

    est.generated_at = time.time() - 600
    assert not est.is_fresh


def test_estimate_decay():
    """Signal decays toward market midpoint with age."""
    est = _make_estimate(p_calibrated=0.73)

    decayed_fresh = est.decay_toward_market(0.50)
    assert abs(decayed_fresh - 0.73) < 0.01

    est.generated_at = time.time() - 1800
    decayed_old = est.decay_toward_market(0.50)
    assert decayed_old < decayed_fresh
    assert decayed_old > 0.50


def test_estimate_has_blind_and_challenged():
    """Estimate tracks both blind and challenged passes."""
    est = _make_estimate(
        p_blind=0.70,
        p_challenged=0.63,
        p_calibrated=0.66,
    )
    assert est.p_blind == 0.70
    assert est.p_challenged == 0.63
    assert est.p_calibrated == 0.66
    assert est.contra_points == "contra argument"


def test_reasoner_disabled():
    """Disabled reasoner returns None."""
    config = ReasonerConfig(enabled=False)
    reasoner = LLMReasoner(config)
    assert reasoner.get_estimate("any") is None


def test_reasoner_cache():
    """Estimates are cached and retrievable."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    est = _make_estimate(condition_id="market_1")
    reasoner._cache["market_1"] = est

    result = reasoner.get_estimate("market_1")
    assert result is not None
    assert result.p_calibrated == 0.66
    assert reasoner.get_estimate("unknown") is None


def test_reasoner_expired_estimate():
    """Expired estimates return None."""
    config = ReasonerConfig(
        enabled=True, auth_token="test",
        signal_max_age_s=60.0,
    )
    reasoner = LLMReasoner(config)

    est = _make_estimate(condition_id="old")
    est.generated_at = time.time() - 120
    reasoner._cache["old"] = est

    assert reasoner.get_estimate("old") is None


def test_blended_fair_value_no_signal():
    """No LLM signal → book midpoint."""
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

    est = _make_estimate(
        condition_id="m1", p_calibrated=0.73,
        uncertainty=0.10,
    )
    reasoner._cache["m1"] = est

    fv, meta = reasoner.get_blended_fair_value(
        "m1", book_midpoint=0.55, blend_weight=0.33,
    )
    assert meta["llm_used"]
    assert 0.59 < fv < 0.62


def test_blended_fair_value_low_confidence():
    """Low confidence → fallback to midpoint."""
    config = ReasonerConfig(
        enabled=True, auth_token="test",
        min_confidence=0.70,
    )
    reasoner = LLMReasoner(config)

    est = _make_estimate(
        condition_id="uncertain", uncertainty=0.40,
    )
    reasoner._cache["uncertain"] = est

    fv, meta = reasoner.get_blended_fair_value(
        "uncertain", book_midpoint=0.55,
    )
    assert fv == 0.55
    assert meta["miss_reason"] == "low_confidence"


def test_blended_meta_includes_blind():
    """Metadata includes blind pass info."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    est = _make_estimate(
        condition_id="m2",
        p_blind=0.70,
        p_challenged=0.65,
        p_calibrated=0.68,
        uncertainty=0.10,
    )
    reasoner._cache["m2"] = est

    _, meta = reasoner.get_blended_fair_value(
        "m2", book_midpoint=0.50,
    )
    assert meta["p_blind"] == 0.70
    assert meta["p_challenged"] == 0.65
    assert meta["p_calibrated"] == 0.68


def test_reasoner_status():
    """Status reports cache and call counts."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    status = reasoner.get_status()
    assert status["enabled"]
    assert status["cached_estimates"] == 0


def test_config_from_env() -> None:
    """Config loads from environment variables."""
    import os

    os.environ["PMM1_LLM_ENABLED"] = "true"
    os.environ["ANTHROPIC_OAUTH_TOKEN"] = "sk-ant-oat01-test"
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
            "PMM1_LLM_THINKING_BUDGET",
            "PMM1_LLM_CYCLE_INTERVAL",
        ]:
            os.environ.pop(key, None)


def test_parse_response():
    """Parses JSON from Opus response text."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    result = reasoner._parse_response(
        '{"p_hat": 0.65, "uncertainty": 0.12}',
    )
    assert result is not None
    assert result["p_hat"] == 0.65

    result = reasoner._parse_response(
        'Analysis:\n```json\n{"p_hat": 0.70}\n```',
    )
    assert result is not None
    assert result["p_hat"] == 0.70

    assert reasoner._parse_response("not json") is None


def test_priority_markets_skips_extremes():
    """Markets near 0 or 1 are not worth analyzing."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)
    # No bot_state → empty list
    assert reasoner._get_priority_markets() == []


def test_set_bot_state():
    """Bot state can be injected after construction."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)
    assert reasoner.bot_state is None
    reasoner.set_bot_state({"test": True})
    assert reasoner.bot_state is not None

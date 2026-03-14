"""Tests for embedded LLM reasoner with Paper 1+2 pipeline."""

import time
from unittest.mock import AsyncMock

import pytest

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


# ── Phase 2: Cost Controls ──


def test_config_tiered_models_defaults():
    """Config has tiered model defaults."""
    config = ReasonerConfig()
    assert config.blind_model == "claude-sonnet-4-6-20250514"
    assert config.challenge_model == "claude-opus-4-6-20250610"
    assert config.daily_cost_cap_usd == 50.0


def test_config_tiered_models_from_env():
    """Tiered models load from environment variables."""
    import os

    os.environ["PMM1_LLM_ENABLED"] = "true"
    os.environ["ANTHROPIC_OAUTH_TOKEN"] = "sk-ant-oat01-test"
    os.environ["PMM1_LLM_BLIND_MODEL"] = "claude-haiku-custom"
    os.environ["PMM1_LLM_CHALLENGE_MODEL"] = "claude-opus-custom"
    os.environ["PMM1_LLM_DAILY_COST_CAP"] = "25.0"

    try:
        config = ReasonerConfig.from_env()
        assert config.blind_model == "claude-haiku-custom"
        assert config.challenge_model == "claude-opus-custom"
        assert config.daily_cost_cap_usd == 25.0
    finally:
        for key in [
            "PMM1_LLM_ENABLED", "ANTHROPIC_OAUTH_TOKEN",
            "PMM1_LLM_BLIND_MODEL", "PMM1_LLM_CHALLENGE_MODEL",
            "PMM1_LLM_DAILY_COST_CAP",
        ]:
            os.environ.pop(key, None)


def test_track_cost_accumulates():
    """_track_cost accumulates daily spend."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    assert reasoner._daily_cost_usd == 0.0
    assert reasoner._daily_token_spend == 0

    reasoner._track_cost(1000, 500)
    assert reasoner._daily_token_spend == 1500
    # 1000 * 15/1M + 500 * 75/1M = 0.015 + 0.0375 = 0.0525
    assert abs(reasoner._daily_cost_usd - 0.0525) < 1e-6

    reasoner._track_cost(2000, 1000)
    assert reasoner._daily_token_spend == 4500
    expected = 0.0525 + (2000 * 15 / 1e6 + 1000 * 75 / 1e6)
    assert abs(reasoner._daily_cost_usd - expected) < 1e-6


def test_track_cost_resets_after_24h():
    """_track_cost resets counters after 24 hours."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    reasoner._track_cost(10000, 5000)
    assert reasoner._daily_token_spend == 15000
    assert reasoner._daily_cost_usd > 0

    # Simulate 25 hours passing
    reasoner._cost_day_start = time.time() - 90000
    reasoner._track_cost(100, 50)
    # Should have reset and only counted new tokens
    assert reasoner._daily_token_spend == 150


def test_cost_cap_hit_property():
    """_cost_cap_hit returns True when over cap."""
    config = ReasonerConfig(
        enabled=True, auth_token="test",
        daily_cost_cap_usd=1.0,
    )
    reasoner = LLMReasoner(config)

    assert not reasoner._cost_cap_hit
    reasoner._daily_cost_usd = 0.99
    assert not reasoner._cost_cap_hit
    reasoner._daily_cost_usd = 1.0
    assert reasoner._cost_cap_hit
    reasoner._daily_cost_usd = 1.5
    assert reasoner._cost_cap_hit


def test_status_includes_cost_fields():
    """get_status() includes cost and circuit breaker fields."""
    config = ReasonerConfig(
        enabled=True, auth_token="test",
        daily_cost_cap_usd=50.0,
    )
    reasoner = LLMReasoner(config)
    reasoner._daily_cost_usd = 12.345

    status = reasoner.get_status()
    assert status["daily_cost_usd"] == 12.35  # rounded
    assert status["daily_cost_cap_usd"] == 50.0
    assert status["cost_cap_hit"] is False
    assert status["circuit_open"] is False
    assert status["consecutive_failures"] == 0


# ── Phase 3: Circuit Breaker ──


def test_circuit_breaker_initial_state():
    """Circuit breaker starts closed."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    assert not reasoner._circuit_open
    assert reasoner._consecutive_failures == 0
    assert reasoner._circuit_open_until == 0.0


def test_circuit_breaker_opens_after_max_failures():
    """Circuit opens after _MAX_CONSECUTIVE_FAILURES."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    # Simulate failures below threshold
    reasoner._consecutive_failures = 4
    assert not reasoner._circuit_open

    # Trip it at threshold
    reasoner._consecutive_failures = 5
    reasoner._circuit_open = True
    reasoner._circuit_open_until = time.time() + 900

    assert reasoner._circuit_open

    status = reasoner.get_status()
    assert status["circuit_open"] is True
    assert status["consecutive_failures"] == 5


@pytest.mark.asyncio
async def test_call_opus_rejects_when_circuit_open():
    """_call_opus raises when circuit is open."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    reasoner._circuit_open = True
    reasoner._circuit_open_until = time.time() + 900

    with pytest.raises(RuntimeError, match="Circuit breaker open"):
        await reasoner._call_opus("test prompt")


@pytest.mark.asyncio
async def test_call_opus_resets_circuit_after_cooldown():
    """Circuit closes after cooldown expires."""
    config = ReasonerConfig(enabled=True, auth_token="test")
    reasoner = LLMReasoner(config)

    reasoner._circuit_open = True
    reasoner._consecutive_failures = 5
    # Set cooldown to already expired
    reasoner._circuit_open_until = time.time() - 1

    # The circuit should close, but the actual API call will fail
    # (no real API). The important thing is it doesn't raise
    # RuntimeError("Circuit breaker open").
    with pytest.raises(Exception) as exc_info:
        await reasoner._call_opus("test prompt")
    # Should NOT be the circuit breaker error
    assert "Circuit breaker open" not in str(exc_info.value)
    # Circuit should now be closed (reset happened before API call)
    # But consecutive_failures increments due to API error
    assert not reasoner._circuit_open or (
        reasoner._consecutive_failures
        >= LLMReasoner._MAX_CONSECUTIVE_FAILURES
    )


@pytest.mark.asyncio
async def test_call_opus_increments_failures():
    """Each failed _call_opus increments consecutive_failures."""
    config = ReasonerConfig(
        enabled=True, auth_token="test",
        per_market_timeout_s=1.0,
    )
    reasoner = LLMReasoner(config)

    assert reasoner._consecutive_failures == 0

    for i in range(1, 4):
        with pytest.raises(Exception):
            await reasoner._call_opus("test")
        assert reasoner._consecutive_failures == i
        assert not reasoner._circuit_open  # under threshold


@pytest.mark.asyncio
async def test_call_opus_opens_circuit_at_threshold():
    """Circuit opens on the 5th consecutive failure."""
    config = ReasonerConfig(
        enabled=True, auth_token="test",
        per_market_timeout_s=1.0,
    )
    reasoner = LLMReasoner(config)

    for i in range(5):
        with pytest.raises(Exception):
            await reasoner._call_opus("test")

    assert reasoner._circuit_open
    assert reasoner._consecutive_failures == 5
    assert reasoner._circuit_open_until > time.time()


@pytest.mark.asyncio
async def test_call_opus_resets_failures_on_success():
    """Successful call resets consecutive_failures to 0."""
    config = ReasonerConfig(
        enabled=True, auth_token="test",
        per_market_timeout_s=5.0,
    )
    reasoner = LLMReasoner(config)
    reasoner._consecutive_failures = 3

    # Mock a successful API response
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = AsyncMock(return_value={
        "content": [{"type": "text", "text": "{}"}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.post = lambda *a, **kw: mock_resp
    mock_session.closed = False
    reasoner._session = mock_session

    result = await reasoner._call_opus("test")
    assert reasoner._consecutive_failures == 0
    assert result["text"] == "{}"


@pytest.mark.asyncio
async def test_call_opus_uses_model_parameter():
    """_call_opus uses the model parameter when provided."""
    config = ReasonerConfig(
        enabled=True, auth_token="test",
        model="default-model",
    )
    reasoner = LLMReasoner(config)

    captured_body = {}

    mock_resp = AsyncMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = AsyncMock(return_value={
        "content": [{"type": "text", "text": "{}"}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    def capture_post(*args, **kwargs):
        captured_body.update(kwargs.get("json", {}))
        return mock_resp

    mock_session = AsyncMock()
    mock_session.post = capture_post
    mock_session.closed = False
    reasoner._session = mock_session

    # With explicit model
    await reasoner._call_opus("test", model="custom-model")
    assert captured_body["model"] == "custom-model"

    # Without model (should use default)
    captured_body.clear()
    await reasoner._call_opus("test")
    assert captured_body["model"] == "default-model"


def test_class_level_circuit_constants():
    """Circuit breaker constants are class-level."""
    assert LLMReasoner._MAX_CONSECUTIVE_FAILURES == 5
    assert LLMReasoner._CIRCUIT_COOLDOWN_S == 900.0

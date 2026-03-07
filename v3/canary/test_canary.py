"""
V3 Canary Integration Tests
Tests V3Integrator, CanaryRamp, and CanaryMetrics
"""

import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import yaml

from v3.canary.integrator import V3Integrator
from v3.canary.ramp import CanaryRamp
from v3.canary.metrics import CanaryMetrics
from v3.evidence.entities import FairValueSignal

# Use anyio for async test support
pytestmark = pytest.mark.anyio


class MockRedis:
    """Mock Redis for testing."""
    
    def __init__(self):
        self.data = {}
        self.closed = False
        
    async def hgetall(self, key: str) -> dict:
        """Mock hgetall."""
        return self.data.get(key, {})
        
    async def aclose(self):
        """Mock close."""
        self.closed = True
        
    @staticmethod
    async def from_url(url: str, decode_responses: bool = True):
        """Mock from_url."""
        return MockRedis()


@pytest.fixture
def mock_redis():
    """Provide mock Redis instance."""
    return MockRedis()


@pytest.fixture
def integrator(mock_redis, monkeypatch):
    """Provide V3Integrator with mock Redis."""
    async def mock_from_url(url, decode_responses=True):
        return mock_redis
        
    monkeypatch.setattr("redis.asyncio.from_url", mock_from_url)
    
    integrator = V3Integrator(
        redis_url="redis://localhost:6379",
        max_skew_cents=1.0,
        min_confidence=0.70,
        max_age_seconds=300.0,
        enabled=True,
    )
    
    return integrator


@pytest.fixture
def temp_config():
    """Provide temporary config file."""
    config = {
        "bot": {"name": "PMM-1"},
        "v3": {
            "enabled": False,
            "max_skew_cents": 0,
        }
    }
    
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config, f)
        temp_path = f.name
        
    yield temp_path
    
    # Cleanup
    Path(temp_path).unlink(missing_ok=True)


def create_signal_dict(
    condition_id: str = "0x123",
    p_calibrated: float = 0.60,
    uncertainty: float = 0.20,
    hurdle_met: bool = True,
    route: str = "numeric",
    age_seconds: float = 60.0,
) -> dict:
    """Create mock V3 signal dict."""
    generated_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    
    return {
        "condition_id": condition_id,
        "p_calibrated": str(p_calibrated),
        "p_low": str(p_calibrated - 0.05),
        "p_high": str(p_calibrated + 0.05),
        "uncertainty": str(uncertainty),
        "skew_cents": "2.5",
        "hurdle_cents": "1.0",
        "hurdle_met": "true" if hurdle_met else "false",
        "route": route,
        "evidence_ids": "ev1,ev2",
        "counterevidence_ids": "",
        "models_used": "gpt-4,claude",
        "generated_at": generated_at.isoformat(),
    }


async def test_integrator_v3_signal_available_clamped(integrator, mock_redis):
    """Test: V3 signal available → blended value clamped correctly."""
    await integrator.connect()
    
    condition_id = "0x123"
    book_mid = 0.50
    
    # V3 says 0.60, market at 0.50, max_skew=0.01 → blended=0.51
    signal_dict = create_signal_dict(
        condition_id=condition_id,
        p_calibrated=0.60,
        uncertainty=0.20,  # confidence = 0.80 > 0.70
        hurdle_met=True,
        age_seconds=60.0,  # < 300s
    )
    
    mock_redis.data[f"v3:signal:{condition_id}"] = signal_dict
    
    blended, metadata = await integrator.get_blended_fair_value(condition_id, book_mid)
    
    # Should clamp to book_mid + max_skew_cents/100 = 0.50 + 0.01 = 0.51
    assert blended == pytest.approx(0.51, abs=1e-6)
    assert metadata["v3_used"] is True
    assert metadata["v3_raw"] == 0.60
    assert metadata["v3_clamped"] == pytest.approx(0.51, abs=1e-6)
    assert metadata["skew_applied_cents"] == pytest.approx(1.0, abs=1e-6)
    assert metadata["v3_route"] == "numeric"
    assert metadata["v3_age_seconds"] is not None
    assert metadata["miss_reason"] is None
    
    await integrator.close()


async def test_integrator_v3_signal_within_clamp(integrator, mock_redis):
    """Test: V3 says 0.60, market at 0.50, max_skew=0.10 → blended=0.60 (within clamp)."""
    await integrator.connect()
    
    # Change max_skew to 10 cents
    integrator.max_skew_cents = 10.0
    
    condition_id = "0x456"
    book_mid = 0.50
    
    signal_dict = create_signal_dict(
        condition_id=condition_id,
        p_calibrated=0.60,
        uncertainty=0.20,
        hurdle_met=True,
        age_seconds=60.0,
    )
    
    mock_redis.data[f"v3:signal:{condition_id}"] = signal_dict
    
    blended, metadata = await integrator.get_blended_fair_value(condition_id, book_mid)
    
    # Should use V3 signal directly (0.60 is within [0.40, 0.60] = [0.50±0.10])
    assert blended == pytest.approx(0.60, abs=1e-6)
    assert metadata["v3_used"] is True
    assert metadata["v3_raw"] == 0.60
    assert metadata["v3_clamped"] == pytest.approx(0.60, abs=1e-6)
    assert metadata["skew_applied_cents"] == pytest.approx(10.0, abs=1e-6)
    
    await integrator.close()


async def test_integrator_v3_signal_unavailable(integrator, mock_redis):
    """Test: V3 signal unavailable → falls back to midpoint."""
    await integrator.connect()
    
    condition_id = "0x789"
    book_mid = 0.55
    
    # No signal in Redis
    blended, metadata = await integrator.get_blended_fair_value(condition_id, book_mid)
    
    assert blended == book_mid
    assert metadata["v3_used"] is False
    assert metadata["v3_raw"] is None
    assert metadata["miss_reason"] == "not_available"
    
    await integrator.close()


async def test_integrator_v3_signal_expired(integrator, mock_redis):
    """Test: V3 signal expired → falls back to midpoint."""
    await integrator.connect()
    
    condition_id = "0xabc"
    book_mid = 0.65
    
    # Signal age = 400 seconds (> 300 max_age_seconds)
    signal_dict = create_signal_dict(
        condition_id=condition_id,
        p_calibrated=0.70,
        uncertainty=0.20,
        hurdle_met=True,
        age_seconds=400.0,
    )
    
    mock_redis.data[f"v3:signal:{condition_id}"] = signal_dict
    
    blended, metadata = await integrator.get_blended_fair_value(condition_id, book_mid)
    
    assert blended == book_mid
    assert metadata["v3_used"] is False
    assert metadata["miss_reason"] == "expired"
    assert metadata["v3_age_seconds"] > 300
    
    await integrator.close()


async def test_integrator_v3_low_confidence(integrator, mock_redis):
    """Test: V3 low confidence → falls back to midpoint."""
    await integrator.connect()
    
    condition_id = "0xdef"
    book_mid = 0.45
    
    # Uncertainty = 0.40 → confidence = 0.60 < 0.70 min_confidence
    signal_dict = create_signal_dict(
        condition_id=condition_id,
        p_calibrated=0.50,
        uncertainty=0.40,
        hurdle_met=True,
        age_seconds=60.0,
    )
    
    mock_redis.data[f"v3:signal:{condition_id}"] = signal_dict
    
    blended, metadata = await integrator.get_blended_fair_value(condition_id, book_mid)
    
    assert blended == book_mid
    assert metadata["v3_used"] is False
    assert metadata["miss_reason"] == "low_confidence"
    
    await integrator.close()


async def test_integrator_v3_disabled(integrator, mock_redis):
    """Test: V3 disabled → falls back to midpoint."""
    await integrator.connect()
    
    # Disable V3
    integrator._enabled = False
    
    condition_id = "0xghi"
    book_mid = 0.70
    
    signal_dict = create_signal_dict(
        condition_id=condition_id,
        p_calibrated=0.75,
        uncertainty=0.20,
        hurdle_met=True,
        age_seconds=60.0,
    )
    
    mock_redis.data[f"v3:signal:{condition_id}"] = signal_dict
    
    blended, metadata = await integrator.get_blended_fair_value(condition_id, book_mid)
    
    assert blended == book_mid
    assert metadata["v3_used"] is False
    assert metadata["miss_reason"] == "disabled"
    
    await integrator.close()


async def test_integrator_hurdle_not_met(integrator, mock_redis):
    """Test: V3 hurdle not met → falls back to midpoint."""
    await integrator.connect()
    
    condition_id = "0xjkl"
    book_mid = 0.52
    
    signal_dict = create_signal_dict(
        condition_id=condition_id,
        p_calibrated=0.55,
        uncertainty=0.20,
        hurdle_met=False,  # Hurdle not met
        age_seconds=60.0,
    )
    
    mock_redis.data[f"v3:signal:{condition_id}"] = signal_dict
    
    blended, metadata = await integrator.get_blended_fair_value(condition_id, book_mid)
    
    assert blended == book_mid
    assert metadata["v3_used"] is False
    assert metadata["miss_reason"] == "hurdle_not_met"
    
    await integrator.close()


def test_canary_ramp_get_current_stage(temp_config):
    """Test: CanaryRamp get_current_stage."""
    ramp = CanaryRamp(config_path=temp_config)
    
    current = ramp.get_current_stage()
    
    assert current["name"] == "shadow"
    assert current["max_skew"] == 0
    assert current["enabled"] is False
    assert current["is_current"] is True


def test_canary_ramp_advance_stage(temp_config):
    """Test: CanaryRamp advance_stage progression."""
    ramp = CanaryRamp(config_path=temp_config)
    
    # Start at shadow
    current = ramp.get_current_stage()
    assert current["name"] == "shadow"
    
    # Advance to canary_1c
    next_stage = ramp.advance_stage()
    assert next_stage["name"] == "canary_1c"
    assert next_stage["max_skew"] == 1.0
    assert next_stage["enabled"] is True
    
    # Verify config was updated
    current = ramp.get_current_stage()
    assert current["name"] == "canary_1c"
    
    # Advance to canary_2c
    next_stage = ramp.advance_stage()
    assert next_stage["name"] == "canary_2c"
    assert next_stage["max_skew"] == 2.0
    
    # Advance to canary_5c
    next_stage = ramp.advance_stage()
    assert next_stage["name"] == "canary_5c"
    assert next_stage["max_skew"] == 5.0
    
    # Advance to production
    next_stage = ramp.advance_stage()
    assert next_stage["name"] == "production"
    assert next_stage["max_skew"] == 100.0
    
    # Try to advance beyond production (should raise)
    with pytest.raises(ValueError, match="Already at production"):
        ramp.advance_stage()


def test_canary_ramp_retreat_stage(temp_config):
    """Test: CanaryRamp retreat_stage."""
    ramp = CanaryRamp(config_path=temp_config)
    
    # Advance to canary_2c
    ramp.advance_stage()  # shadow -> canary_1c
    ramp.advance_stage()  # canary_1c -> canary_2c
    
    current = ramp.get_current_stage()
    assert current["name"] == "canary_2c"
    
    # Retreat to canary_1c
    prev_stage = ramp.retreat_stage()
    assert prev_stage["name"] == "canary_1c"
    assert prev_stage["max_skew"] == 1.0
    
    # Retreat to shadow
    prev_stage = ramp.retreat_stage()
    assert prev_stage["name"] == "shadow"
    assert prev_stage["enabled"] is False
    
    # Try to retreat beyond shadow (should raise)
    with pytest.raises(ValueError, match="Already at shadow"):
        ramp.retreat_stage()


def test_canary_ramp_emergency_kill(temp_config):
    """Test: CanaryRamp emergency_kill."""
    ramp = CanaryRamp(config_path=temp_config)
    
    # Advance to production
    for _ in range(4):
        ramp.advance_stage()
        
    current = ramp.get_current_stage()
    assert current["name"] == "production"
    assert current["enabled"] is True
    
    # Emergency kill
    ramp.emergency_kill()
    
    # Should be disabled but keep max_skew setting
    config = ramp._load_config()
    assert config["v3"]["enabled"] is False
    assert config["v3"]["max_skew_cents"] == 100.0  # Unchanged


def test_canary_metrics_recording():
    """Test: CanaryMetrics record_blend and get_summary."""
    metrics = CanaryMetrics()
    
    # Record some blends
    metrics.record_blend("0x1", 0.50, 0.60, 0.51, True, 1.0)
    metrics.record_blend("0x2", 0.45, 0.50, 0.46, True, 1.0)
    metrics.record_blend("0x3", 0.55, None, 0.55, False, 0.0, "not_available")
    metrics.record_blend("0x4", 0.60, None, 0.60, False, 0.0, "expired")
    metrics.record_blend("0x5", 0.52, 0.54, 0.53, True, 1.0)
    
    summary = metrics.get_summary()
    
    assert summary["total_quotes"] == 5
    assert summary["v3_used_count"] == 3
    assert summary["v3_used_pct"] == 60.0
    assert summary["avg_skew_cents"] == 1.0
    assert summary["max_skew_cents"] == 1.0
    assert summary["min_skew_cents"] == 1.0
    assert summary["v3_miss_reasons"]["not_available"] == 1
    assert summary["v3_miss_reasons"]["expired"] == 1


def test_canary_metrics_format_telegram():
    """Test: CanaryMetrics format_telegram_report."""
    metrics = CanaryMetrics()
    
    metrics.record_blend("0x1", 0.50, 0.55, 0.51, True, 1.0)
    metrics.record_blend("0x2", 0.48, None, 0.48, False, 0.0, "disabled")
    metrics.record_blend("0x3", 0.52, 0.58, 0.53, True, 1.0)
    
    report = metrics.format_telegram_report()
    
    assert "V3 Canary Metrics" in report
    assert "**Total Quotes:** 3" in report
    assert "**V3 Used:** 2 (66.67%)" in report
    assert "Avg: +1.00¢" in report
    assert "disabled: 1" in report


def test_canary_metrics_reset():
    """Test: CanaryMetrics reset."""
    metrics = CanaryMetrics()
    
    metrics.record_blend("0x1", 0.50, 0.55, 0.51, True, 1.0)
    metrics.record_blend("0x2", 0.48, None, 0.48, False, 0.0, "disabled")
    
    assert len(metrics.records) == 2
    assert len(metrics.miss_reasons) == 1
    
    metrics.reset()
    
    assert len(metrics.records) == 0
    assert len(metrics.miss_reasons) == 0
    
    summary = metrics.get_summary()
    assert summary["total_quotes"] == 0


def test_clamp_math_edge_cases():
    """Test: Clamp math edge cases."""
    # Test negative skew (V3 lower than market)
    # V3 = 0.40, market = 0.50, max_skew = 0.01 → clamped = 0.49
    max_skew_decimal = 0.01
    book_mid = 0.50
    v3_signal = 0.40
    
    lower = book_mid - max_skew_decimal
    upper = book_mid + max_skew_decimal
    clamped = max(lower, min(upper, v3_signal))
    
    assert clamped == 0.49
    
    # Test V3 exactly at lower bound
    v3_signal = 0.49
    clamped = max(lower, min(upper, v3_signal))
    assert clamped == 0.49
    
    # Test V3 exactly at upper bound
    v3_signal = 0.51
    clamped = max(lower, min(upper, v3_signal))
    assert clamped == 0.51
    
    # Test V3 within bounds
    v3_signal = 0.505
    clamped = max(lower, min(upper, v3_signal))
    assert clamped == 0.505


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

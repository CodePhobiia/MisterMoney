"""
Integration Tests for V3 Calibration and Signal Serving
"""

import asyncio
import math
from datetime import datetime, timedelta, timezone
import pytest

from v3.calibration.route_models import RouteCalibrator, CalibrationManager, _logit, _sigmoid
from v3.calibration.decay import decay_signal, is_signal_expired, HALF_LIVES
from v3.evidence.entities import FairValueSignal
from v3.evidence.db import Database
from v3.serving.publisher import SignalPublisher
from v3.serving.consumer import V3Consumer


# Test 1: RouteCalibrator calibrate() with cold start
def test_route_calibrator_cold_start():
    """Test RouteCalibrator calibration in cold start mode"""
    calibrator = RouteCalibrator("numeric")
    
    # Cold start beta = [0, 1, 0, 0, 0, 0, 0] should pass through market prior
    features = {
        'market_mid': 0.65,
        'uncertainty': 0.3,
        'evidence_count': 5,
        'source_reliability_avg': 0.8,
        'hours_to_resolution': 24,
        'volume_24h': 1000,
    }
    
    raw_p = 0.75
    p_calibrated = calibrator.calibrate(raw_p, features)
    
    # With beta = [0, 1, 0, 0, 0, 0, 0], should calibrate toward market_mid
    # p_calibrated = sigmoid(0 * logit(raw_p) + 1 * logit(market_mid) + 0 * ...)
    #              = sigmoid(logit(0.65))
    #              = 0.65
    assert abs(p_calibrated - 0.65) < 0.01, f"Expected ~0.65, got {p_calibrated}"
    
    print(f"✓ Cold start calibration: raw_p={raw_p}, p_calibrated={p_calibrated:.4f}")


# Test 2: Conformal intervals (cold start = wide)
def test_conformal_intervals_cold_start():
    """Test conformal prediction intervals in cold start"""
    calibrator = RouteCalibrator("simple")
    
    features = {
        'market_mid': 0.5,
        'uncertainty': 0.25,
        'evidence_count': 3,
        'source_reliability_avg': 0.7,
        'hours_to_resolution': 48,
        'volume_24h': 500,
    }
    
    raw_p = 0.6
    p_low, p_high = calibrator.conformal_interval(raw_p, features)
    
    # Cold start intervals should be ±0.20
    # p_calibrated should be ~0.5 (market_mid in cold start)
    # So p_low ~= 0.3, p_high ~= 0.7
    assert p_low >= 0.0 and p_low <= 0.5, f"p_low out of range: {p_low}"
    assert p_high >= 0.5 and p_high <= 1.0, f"p_high out of range: {p_high}"
    assert (p_high - p_low) >= 0.35, f"Interval too narrow: {p_high - p_low}"  # Should be ~0.4 in cold start
    
    print(f"✓ Cold start intervals: [{p_low:.4f}, {p_high:.4f}], width={p_high-p_low:.4f}")


# Test 3: Signal decay (exponential decay math)
def test_signal_decay():
    """Test signal decay toward market consensus"""
    route = "numeric"
    half_life = HALF_LIVES[route]  # 60s
    
    p_raw = 0.7
    market_mid = 0.5
    
    # At t=0, should return p_raw
    p_t0 = decay_signal(p_raw, market_mid, age_seconds=0, source_staleness_seconds=0, route=route)
    assert abs(p_t0 - p_raw) < 0.01, f"Expected {p_raw}, got {p_t0}"
    
    # At t=half_life, lambda = exp(-1) ≈ 0.368
    # p_live = 0.368 * 0.7 + 0.632 * 0.5 ≈ 0.574
    p_t_half = decay_signal(p_raw, market_mid, age_seconds=half_life, source_staleness_seconds=0, route=route)
    expected_half = 0.368 * p_raw + 0.632 * market_mid
    assert abs(p_t_half - expected_half) < 0.01, f"Expected {expected_half:.4f}, got {p_t_half:.4f}"
    
    # At t=5*half_life, should be very close to market_mid
    p_t_old = decay_signal(p_raw, market_mid, age_seconds=5*half_life, source_staleness_seconds=0, route=route)
    assert abs(p_t_old - market_mid) < 0.05, f"Expected ~{market_mid}, got {p_t_old}"
    
    # With source staleness, decay should be faster
    p_stale = decay_signal(p_raw, market_mid, age_seconds=half_life, source_staleness_seconds=half_life, route=route)
    assert p_stale < p_t_half, f"Stale signal should decay more: {p_stale} vs {p_t_half}"
    
    print(f"✓ Signal decay: t=0: {p_t0:.4f}, t=half_life: {p_t_half:.4f}, t=5*half_life: {p_t_old:.4f}")


# Test 4: is_signal_expired for each route
def test_signal_expiration():
    """Test signal expiration thresholds"""
    for route, half_life in HALF_LIVES.items():
        # Fresh signal should not be expired
        assert not is_signal_expired(0, route), f"{route}: Fresh signal should not expire"
        assert not is_signal_expired(half_life, route), f"{route}: Signal at half_life should not expire"
        
        # Old signal should be expired (lambda < 0.05 means age > -ln(0.05) * half_life ≈ 3 * half_life)
        expiry_age = -math.log(0.05) * half_life * 1.1  # 10% beyond boundary to avoid floating point issues
        assert is_signal_expired(expiry_age, route), f"{route}: Signal should expire at {expiry_age}s"
        
        print(f"✓ {route}: expires at {expiry_age:.0f}s ({expiry_age/60:.1f} min)")


# Test 5: SignalPublisher write/read cycle
@pytest.mark.asyncio
async def test_signal_publisher():
    """Test SignalPublisher Postgres + Redis write/read"""
    # Create test database connection
    db = Database("postgresql://mmbot:mmbot_v3_2026@localhost/mistermoney_v3")
    await db.connect()
    
    # Create publisher
    publisher = SignalPublisher(db, redis_url="redis://localhost:6379")
    await publisher.connect()
    
    # Create test signal
    test_signal = FairValueSignal(
        condition_id="test_condition_123",
        generated_at=datetime.now(timezone.utc),
        p_calibrated=0.68,
        p_low=0.55,
        p_high=0.80,
        uncertainty=0.15,
        skew_cents=2.5,
        hurdle_cents=3.0,
        hurdle_met=True,
        route="numeric",
        evidence_ids=["ev1", "ev2"],
        counterevidence_ids=["ev3"],
        models_used=["sonnet", "opus"],
    )
    
    # Publish signal
    await publisher.publish(test_signal)
    print("✓ Signal published to Postgres + Redis")
    
    # Read from Redis (should be fast)
    signal_redis = await publisher.get_latest("test_condition_123")
    assert signal_redis is not None, "Signal not found in Redis"
    assert signal_redis.condition_id == "test_condition_123"
    assert abs(signal_redis.p_calibrated - 0.68) < 0.01
    print("✓ Signal retrieved from Redis")
    
    # Test get_cached_or_neutral with missing condition
    neutral = await publisher.get_cached_or_neutral("missing_condition_999")
    assert neutral.condition_id == "missing_condition_999"
    assert neutral.p_calibrated == 0.5
    assert neutral.hurdle_met == False
    print("✓ Neutral signal returned for missing condition")
    
    # Cleanup
    await publisher.close()
    await db.close()


# Test 6: V3Consumer get_fair_value scenarios
@pytest.mark.asyncio
async def test_v3_consumer():
    """Test V3Consumer with various scenarios"""
    db = Database("postgresql://mmbot:mmbot_v3_2026@localhost/mistermoney_v3")
    await db.connect()
    
    publisher = SignalPublisher(db, redis_url="redis://localhost:6379")
    await publisher.connect()
    
    consumer = V3Consumer(publisher)
    
    # Scenario 1: Valid signal (hurdle_met=True, low uncertainty, not expired)
    valid_signal = FairValueSignal(
        condition_id="test_valid_signal",
        generated_at=datetime.now(timezone.utc),
        p_calibrated=0.72,
        uncertainty=0.20,
        hurdle_met=True,
        route="simple",
        evidence_ids=["ev1"],
        counterevidence_ids=[],
        models_used=["sonnet"],
    )
    await publisher.publish(valid_signal)
    
    fv = await consumer.get_fair_value("test_valid_signal")
    assert fv is not None, "Should return fair value for valid signal"
    assert abs(fv - 0.72) < 0.01
    print(f"✓ Valid signal: fair_value={fv:.4f}")
    
    # Scenario 2: Hurdle not met (should return None)
    no_hurdle_signal = FairValueSignal(
        condition_id="test_no_hurdle",
        generated_at=datetime.now(timezone.utc),
        p_calibrated=0.52,
        uncertainty=0.15,
        hurdle_met=False,
        route="simple",
        evidence_ids=["ev1"],
        counterevidence_ids=[],
        models_used=["sonnet"],
    )
    await publisher.publish(no_hurdle_signal)
    
    fv_no_hurdle = await consumer.get_fair_value("test_no_hurdle")
    assert fv_no_hurdle is None, "Should return None when hurdle not met"
    print("✓ No hurdle: returns None")
    
    # Scenario 3: High uncertainty (should return None)
    high_unc_signal = FairValueSignal(
        condition_id="test_high_uncertainty",
        generated_at=datetime.now(timezone.utc),
        p_calibrated=0.70,
        uncertainty=0.45,  # > 0.30 threshold
        hurdle_met=True,
        route="simple",
        evidence_ids=["ev1"],
        counterevidence_ids=[],
        models_used=["sonnet"],
    )
    await publisher.publish(high_unc_signal)
    
    fv_high_unc = await consumer.get_fair_value("test_high_uncertainty")
    assert fv_high_unc is None, "Should return None when uncertainty too high"
    print("✓ High uncertainty: returns None")
    
    # Scenario 4: Expired signal (should return None)
    expired_signal = FairValueSignal(
        condition_id="test_expired",
        generated_at=datetime.now(timezone.utc) - timedelta(hours=5),  # Very old for numeric route
        p_calibrated=0.75,
        uncertainty=0.18,
        hurdle_met=True,
        route="numeric",  # 60s half-life
        evidence_ids=["ev1"],
        counterevidence_ids=[],
        models_used=["sonnet"],
    )
    await publisher.publish(expired_signal)
    
    fv_expired = await consumer.get_fair_value("test_expired")
    assert fv_expired is None, "Should return None when signal expired"
    print("✓ Expired signal: returns None")
    
    # Test get_signal_detail
    detail = await consumer.get_signal_detail("test_valid_signal")
    assert detail is not None
    assert detail['condition_id'] == "test_valid_signal"
    assert 'age_seconds' in detail
    assert 'expired' in detail
    print(f"✓ Signal detail: {detail['route']}, age={detail['age_seconds']:.1f}s")
    
    # Cleanup
    await publisher.close()
    await db.close()


# Test 7: CalibrationManager retrain (with mock data)
def test_calibration_manager():
    """Test CalibrationManager with mock resolved markets"""
    manager = CalibrationManager(data_dir="data/v3/calibration_test")
    
    # Get a calibrator
    calibrator = manager.get_calibrator("simple")
    assert calibrator.route == "simple"
    print("✓ CalibrationManager created and calibrator retrieved")
    
    # Create mock resolved markets (< 50, so should stay in cold start)
    mock_markets = [
        {
            'raw_p': 0.6,
            'features': {
                'market_mid': 0.55,
                'uncertainty': 0.25,
                'evidence_count': 5,
                'source_reliability_avg': 0.75,
                'hours_to_resolution': 24,
                'volume_24h': 1000,
            },
            'outcome': 1  # YES
        }
        for _ in range(30)  # Only 30 markets (< 50 threshold)
    ]
    
    # Update weights (should stay in cold start)
    calibrator.update_weights(mock_markets)
    assert calibrator.resolved_market_count == 30
    assert calibrator.beta == [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0], "Should stay in cold start mode"
    print(f"✓ Cold start maintained with {calibrator.resolved_market_count} markets")
    
    # Now add more markets to trigger training
    mock_markets_large = [
        {
            'raw_p': 0.6 + (i % 5) * 0.05,  # Vary raw_p
            'features': {
                'market_mid': 0.55,
                'uncertainty': 0.25,
                'evidence_count': 5,
                'source_reliability_avg': 0.75,
                'hours_to_resolution': 24,
                'volume_24h': 1000,
            },
            'outcome': 1 if i % 2 == 0 else 0  # Mix of YES/NO
        }
        for i in range(100)
    ]
    
    calibrator.update_weights(mock_markets_large)
    assert calibrator.resolved_market_count == 100
    # Beta should have changed from cold start
    print(f"✓ Trained with {calibrator.resolved_market_count} markets, beta={[round(b, 4) for b in calibrator.beta]}")
    print(f"  Stats: {calibrator.calibration_stats}")
    
    # Test save/load
    save_path = "data/v3/calibration_test/test_save.json"
    calibrator.save(save_path)
    
    # Load into new calibrator
    calibrator_loaded = RouteCalibrator("simple")
    calibrator_loaded.load(save_path)
    assert calibrator_loaded.resolved_market_count == 100
    assert calibrator_loaded.beta == calibrator.beta
    print("✓ Calibrator save/load successful")


# Main test runner
if __name__ == "__main__":
    print("\n=== V3 Calibration & Signal Serving Integration Tests ===\n")
    
    print("Test 1: RouteCalibrator cold start calibration")
    test_route_calibrator_cold_start()
    
    print("\nTest 2: Conformal intervals (cold start)")
    test_conformal_intervals_cold_start()
    
    print("\nTest 3: Signal decay")
    test_signal_decay()
    
    print("\nTest 4: Signal expiration")
    test_signal_expiration()
    
    print("\nTest 5: SignalPublisher write/read")
    asyncio.run(test_signal_publisher())
    
    print("\nTest 6: V3Consumer scenarios")
    asyncio.run(test_v3_consumer())
    
    print("\nTest 7: CalibrationManager retrain")
    test_calibration_manager()
    
    print("\n=== All Tests Passed ✓ ===")

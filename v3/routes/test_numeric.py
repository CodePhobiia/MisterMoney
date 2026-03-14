"""
Integration tests for Numeric Route
Tests source registry, checkers, barrier/hazard math, and end-to-end flow
"""

import asyncio
import math
from datetime import UTC, datetime, timedelta

import pytest

from v3.evidence.entities import RuleGraph
from v3.intake.source_checkers.base import SourceCheckResult
from v3.intake.source_checkers.coingecko import CoinGeckoChecker
from v3.intake.source_registry import SourceRegistry
from v3.routes.numeric import NumericRoute

# ========== Market Classification Tests ==========

def test_classify_numeric_coingecko():
    """Test numeric classification for crypto price markets"""
    registry = SourceRegistry()

    result = registry.classify_market(
        question="Will Bitcoin reach $100,000 by end of 2024?",
        rules=(
            "Resolves YES if BTC price on CoinGecko"
            " >= $100,000 at any point before"
            " Dec 31, 2024 23:59 UTC"
        ),
        source="https://coingecko.com/en/coins/bitcoin"
    )

    assert result == "numeric", f"Expected 'numeric', got '{result}'"
    print("✓ CoinGecko market classified as numeric")


def test_classify_simple():
    """Test simple classification for basic YES/NO markets"""
    registry = SourceRegistry()

    result = registry.classify_market(
        question="Will it rain tomorrow?",
        rules="Resolves YES if weather.com reports rain in NYC on Jan 15, 2024",
        source="weather.com"
    )

    assert result == "simple", f"Expected 'simple', got '{result}'"
    print("✓ Simple weather market classified correctly")


def test_classify_rule():
    """Test rule classification for complex legal markets"""
    registry = SourceRegistry()

    long_rules = """
    Resolves YES if the Federal Reserve raises interest rates by at least 25 basis points
    at their next FOMC meeting, notwithstanding any emergency meetings called due to
    market disruptions. Unless the meeting is postponed beyond March 31, 2024, in which
    case this market resolves N/A. Clarification: "raise" means an increase from the
    current effective rate, not the target range. Edge case: if multiple decisions are
    announced simultaneously, the primary decision applies.
    """

    result = registry.classify_market(
        question="Will the Fed raise rates?",
        rules=long_rules,
        source="https://www.federalreserve.gov"
    )

    assert result == "rule", f"Expected 'rule', got '{result}'"
    print("✓ Complex rule-based market classified correctly")


def test_classify_dossier():
    """Test dossier classification for investigation-needed markets"""
    registry = SourceRegistry()

    result = registry.classify_market(
        question="Will credible reports confirm alien contact by 2025?",
        rules=(
            "Resolves YES if multiple credible sources"
            " (NASA, ESA, or peer-reviewed journals)"
            " confirm detection of extraterrestrial"
            " intelligence. Requires verification"
            " from independent panels."
        ),
        source="multiple sources, to be determined"
    )

    assert result == "dossier", f"Expected 'dossier', got '{result}'"
    print("✓ Investigation-heavy market classified as dossier")


def test_classify_numeric_sports():
    """Test numeric classification for sports scores"""
    registry = SourceRegistry()

    result = registry.classify_market(
        question="Will the Lakers score more than 110 points?",
        rules="Resolves YES if Lakers final score > 110 in tonight's game per ESPN",
        source="https://espn.com/nba/scoreboard"
    )

    assert result == "numeric", f"Expected 'numeric', got '{result}'"
    print("✓ Sports score market classified as numeric")


# ========== Barrier Probability Tests ==========

def test_barrier_probability_above_threshold():
    """Test barrier probability when current price is above threshold"""
    route = NumericRoute(
        registry=None,
        evidence_graph=None,
        source_registry=None
    )

    # Current price already above threshold
    prob = route._barrier_probability(
        current=110000,
        threshold=100000,
        vol=0.5,
        time_to_expiry_hours=24 * 365,  # 1 year
        operator='>'
    )

    # Should be high probability since already above
    assert prob > 0.7, f"Expected prob > 0.7 for price already above threshold, got {prob}"
    print(f"✓ Barrier probability (above threshold): {prob:.4f}")


def test_barrier_probability_below_threshold():
    """Test barrier probability when current price is below threshold"""
    route = NumericRoute(
        registry=None,
        evidence_graph=None,
        source_registry=None
    )

    # Current price below threshold (need higher vol or longer time for decent probability)
    prob = route._barrier_probability(
        current=50000,
        threshold=100000,
        vol=1.2,  # High volatility (120% annualized - typical for crypto)
        time_to_expiry_hours=24 * 365,  # 1 year
        operator='>'
    )

    # Should be moderate probability with high volatility
    assert 0.15 < prob < 0.9, f"Expected moderate probability with high vol, got {prob}"
    print(f"✓ Barrier probability (below threshold, high vol): {prob:.4f}")


def test_barrier_probability_high_volatility():
    """Test barrier probability with high volatility"""
    route = NumericRoute(
        registry=None,
        evidence_graph=None,
        source_registry=None
    )

    prob_low_vol = route._barrier_probability(
        current=80000,
        threshold=100000,
        vol=0.1,  # Low volatility
        time_to_expiry_hours=24 * 30,  # 1 month
        operator='>'
    )

    prob_high_vol = route._barrier_probability(
        current=80000,
        threshold=100000,
        vol=1.5,  # High volatility
        time_to_expiry_hours=24 * 30,  # 1 month
        operator='>'
    )

    # Higher volatility should give higher probability of crossing
    assert prob_high_vol > prob_low_vol, \
        f"Expected higher prob with higher vol: {prob_high_vol} vs {prob_low_vol}"
    print(
        f"✓ Barrier probability volatility effect:"
        f" low={prob_low_vol:.4f}, high={prob_high_vol:.4f}"
    )


def test_barrier_probability_expired():
    """Test barrier probability when time has expired"""
    route = NumericRoute(
        registry=None,
        evidence_graph=None,
        source_registry=None
    )

    # Expired, above threshold
    prob_above = route._barrier_probability(
        current=110000,
        threshold=100000,
        vol=0.5,
        time_to_expiry_hours=0,  # Already expired
        operator='>'
    )

    # Expired, below threshold
    prob_below = route._barrier_probability(
        current=90000,
        threshold=100000,
        vol=0.5,
        time_to_expiry_hours=0,  # Already expired
        operator='>'
    )

    assert prob_above == 1.0, f"Expected 1.0 for expired above threshold, got {prob_above}"
    assert prob_below == 0.0, f"Expected 0.0 for expired below threshold, got {prob_below}"
    print("✓ Barrier probability handles expiry correctly")


# ========== Hazard Probability Tests ==========

def test_hazard_probability_basic():
    """Test hazard probability with basic parameters"""
    route = NumericRoute(
        registry=None,
        evidence_graph=None,
        source_registry=None
    )

    # 1% chance per hour, 100 hours
    prob = route._hazard_probability(
        rate=0.01,
        time_to_expiry_hours=100
    )

    # P = 1 - exp(-0.01 * 100) = 1 - exp(-1) ≈ 0.632
    expected = 1 - math.exp(-1)

    assert abs(prob - expected) < 0.01, \
        f"Expected {expected:.4f}, got {prob:.4f}"
    print(f"✓ Hazard probability (basic): {prob:.4f}")


def test_hazard_probability_high_rate():
    """Test hazard probability with high event rate"""
    route = NumericRoute(
        registry=None,
        evidence_graph=None,
        source_registry=None
    )

    # Very high rate should give high probability
    prob = route._hazard_probability(
        rate=0.1,  # 10% per hour
        time_to_expiry_hours=50
    )

    assert prob > 0.99, f"Expected very high probability, got {prob}"
    print(f"✓ Hazard probability (high rate): {prob:.4f}")


def test_hazard_probability_zero_time():
    """Test hazard probability with zero time"""
    route = NumericRoute(
        registry=None,
        evidence_graph=None,
        source_registry=None
    )

    prob = route._hazard_probability(
        rate=0.01,
        time_to_expiry_hours=0
    )

    assert prob == 0.0, f"Expected 0.0 for zero time, got {prob}"
    print("✓ Hazard probability handles zero time")


# ========== CoinGecko Live API Test ==========

@pytest.mark.asyncio
async def test_coingecko_live_bitcoin():
    """Test CoinGecko checker against live API for Bitcoin"""
    checker = CoinGeckoChecker()

    # Create a simple rule for Bitcoin price check
    rule = RuleGraph(
        condition_id="test_btc_001",
        source_name="bitcoin",
        operator=">",
        threshold_num=50000.0,
        window_start=datetime.now(UTC),
        window_end=datetime.now(UTC) + timedelta(days=30)
    )

    result = await checker.check("test_btc_001", rule)

    print("\n✓ CoinGecko live check:")
    print(f"  Source: {result.source}")
    print(f"  Current BTC price: ${result.current_value}")
    print(f"  Threshold: ${result.threshold}")
    print(f"  Probability: {result.probability:.4f}")
    print(f"  Confidence: {result.confidence:.4f}")

    # Basic validation
    assert result.condition_id == "test_btc_001"
    assert isinstance(result.current_value, (int, float))
    assert 0.0 <= result.probability <= 1.0
    assert 0.0 <= result.confidence <= 1.0

    # If API succeeded, current_value should be reasonable
    if result.confidence > 0.5:
        assert result.current_value > 1000, \
            f"BTC price seems unrealistic: {result.current_value}"
        print("  ✓ Live API data looks valid")


# ========== End-to-End Test ==========

@pytest.mark.asyncio
async def test_numeric_route_end_to_end():
    """Test complete NumericRoute.solve() flow with mock data"""

    # Create mock source check result
    source_check = SourceCheckResult(
        condition_id="test_e2e_001",
        source="bitcoin",
        current_value=95000.0,
        threshold=100000.0,
        probability=0.7,
        confidence=0.85,
        raw_data={'volatility': 0.6},
        ttl_seconds=60
    )

    # Create rule
    rule = RuleGraph(
        condition_id="test_e2e_001",
        source_name="bitcoin",
        operator=">",
        threshold_num=100000.0,
        window_start=datetime.now(UTC),
        window_end=datetime.now(UTC) + timedelta(days=365)
    )

    # Create route (with None dependencies for unit test)
    route = NumericRoute(
        registry=None,
        evidence_graph=None,
        source_registry=None
    )

    # Solve
    signal = await route.solve(
        condition_id="test_e2e_001",
        rule=rule,
        source_check=source_check,
        evidence=[]
    )

    print("\n✓ End-to-end NumericRoute.solve():")
    print(f"  Condition: {signal.condition_id}")
    print(f"  Route: {signal.route}")
    print(f"  P(calibrated): {signal.p_calibrated:.4f}")
    print(f"  Uncertainty: {signal.uncertainty:.4f}")
    print(f"  Bounds: [{signal.p_low:.4f}, {signal.p_high:.4f}]")
    print(f"  Models used: {signal.models_used}")

    # Validation
    assert signal.condition_id == "test_e2e_001"
    assert signal.route == "numeric"
    assert 0.0 <= signal.p_calibrated <= 1.0
    assert 0.0 <= signal.uncertainty <= 1.0
    assert signal.p_low <= signal.p_calibrated <= signal.p_high
    assert len(signal.models_used) > 0


# ========== Main Test Runner ==========

def run_sync_tests():
    """Run all synchronous tests"""
    print("\n" + "=" * 60)
    print("NUMERIC ROUTE INTEGRATION TESTS")
    print("=" * 60)

    print("\n--- Market Classification Tests ---")
    test_classify_numeric_coingecko()
    test_classify_simple()
    test_classify_rule()
    test_classify_dossier()
    test_classify_numeric_sports()

    print("\n--- Barrier Probability Tests ---")
    test_barrier_probability_above_threshold()
    test_barrier_probability_below_threshold()
    test_barrier_probability_high_volatility()
    test_barrier_probability_expired()

    print("\n--- Hazard Probability Tests ---")
    test_hazard_probability_basic()
    test_hazard_probability_high_rate()
    test_hazard_probability_zero_time()


async def run_async_tests():
    """Run all async tests"""
    print("\n--- Live API Tests ---")

    try:
        await test_coingecko_live_bitcoin()
    except Exception as e:
        print(f"⚠ CoinGecko live test failed (may be rate-limited): {e}")

    print("\n--- End-to-End Tests ---")
    await test_numeric_route_end_to_end()


if __name__ == "__main__":
    # Run sync tests
    run_sync_tests()

    # Run async tests
    asyncio.run(run_async_tests())

    print("\n" + "=" * 60)
    print("ALL TESTS COMPLETE")
    print("=" * 60 + "\n")

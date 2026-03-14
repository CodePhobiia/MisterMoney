"""Tests for Kelly criterion integration in QuoteEngine."""

from pmm1.settings import PricingConfig
from pmm1.strategy.quote_engine import QuoteEngine


def _make_engine(
    kelly_enabled: bool = False,
    max_dollar_size: float = 15.0,
    **overrides: object,
) -> QuoteEngine:
    """Create a QuoteEngine with configurable Kelly settings."""
    defaults = {
        "target_dollar_size": 8.0,
        "max_dollar_size": max_dollar_size,
        "kelly_enabled": kelly_enabled,
        "kelly_fraction": 0.25,
        "kelly_min_edge": 0.03,
        "kelly_max_position_nav": 0.05,
        "kelly_adverse_selection_lambda": 0.4,
    }
    defaults.update(overrides)
    config = PricingConfig(**defaults)  # type: ignore[arg-type]
    return QuoteEngine(
        config, max_dollar_size=max_dollar_size,
    )


def test_kelly_disabled_uses_flat_sizing():
    """When kelly_enabled=False, uses flat dollar sizing."""
    engine = _make_engine(kelly_enabled=False)
    size = engine.compute_size(
        confidence=1.0,
        market_inventory=0.0,
        fair_value=0.50,
        market_price=0.45,
        nav=1000.0,
    )
    # Flat: $8 / $0.50 = 16 shares, capped at $15 / $0.50 = 30
    assert size == 16.0


def test_kelly_enabled_sizes_by_edge():
    """When kelly_enabled=True, sizes proportionally to edge."""
    engine = _make_engine(kelly_enabled=True)

    # Small edge: 50% vs 55% → adj_edge = 5% * 0.4 = 2% < 3% min → 0
    size_small = engine.compute_size(
        confidence=1.0,
        market_inventory=0.0,
        fair_value=0.55,
        market_price=0.50,
        nav=1000.0,
    )
    assert size_small == 0.0  # Below min_edge

    # Large edge: 50% vs 70% → adj_edge = 20% * 0.4 = 8% → trades
    size_large = engine.compute_size(
        confidence=1.0,
        market_inventory=0.0,
        fair_value=0.70,
        market_price=0.50,
        nav=1000.0,
    )
    assert size_large > 5.0  # Above minimum


def test_kelly_scales_with_nav():
    """Kelly sizes scale proportionally with NAV."""
    engine = _make_engine(
        kelly_enabled=True, max_dollar_size=500.0,
    )

    size_1k = engine.compute_size(
        confidence=1.0,
        market_inventory=0.0,
        fair_value=0.70,
        market_price=0.50,
        nav=1000.0,
    )
    size_2k = engine.compute_size(
        confidence=1.0,
        market_inventory=0.0,
        fair_value=0.70,
        market_price=0.50,
        nav=2000.0,
    )
    # Doubling NAV should roughly double the position
    assert abs(size_2k / size_1k - 2.0) < 0.5


def test_kelly_respects_max_position():
    """Kelly sizing respects max_position_nav cap."""
    engine = _make_engine(
        kelly_enabled=True,
        kelly_max_position_nav=0.05,
    )
    size = engine.compute_size(
        confidence=1.0,
        market_inventory=0.0,
        fair_value=0.95,  # Huge edge
        market_price=0.50,
        nav=1000.0,
    )
    # Max $50 (5% of $1000) / $0.95 per share ≈ 52 shares
    # But max_dollar_size ($15) also caps: $15 / $0.95 ≈ 15.8
    assert size <= 16.0  # max_dollar_size / price


def test_kelly_edge_confidence_modulation():
    """Edge confidence modulates the Kelly fraction."""
    engine = _make_engine(
        kelly_enabled=True, kelly_fraction=0.50,
        max_dollar_size=500.0,
    )

    # Full confidence
    size_full = engine.compute_size(
        confidence=1.0,
        market_inventory=0.0,
        fair_value=0.70,
        market_price=0.50,
        nav=1000.0,
        edge_confidence=1.0,
    )

    # Low confidence (SPRT undecided, few trades)
    size_low = engine.compute_size(
        confidence=1.0,
        market_inventory=0.0,
        fair_value=0.70,
        market_price=0.50,
        nav=1000.0,
        edge_confidence=0.3,
    )

    assert size_low < size_full


def test_kelly_backward_compatible():
    """When kelly_enabled=False, all existing behavior preserved.

    Specifically: market_price, nav, edge_confidence are ignored.
    """
    engine_old = _make_engine(kelly_enabled=False)

    size_no_extras = engine_old.compute_size(
        confidence=0.8,
        market_inventory=5.0,
        fair_value=0.50,
    )
    size_with_extras = engine_old.compute_size(
        confidence=0.8,
        market_inventory=5.0,
        fair_value=0.50,
        market_price=0.45,
        nav=1000.0,
        edge_confidence=0.5,
    )
    assert size_no_extras == size_with_extras


def test_kelly_inventory_adjustment_still_applies():
    """Kelly sizing still applies inventory decay."""
    engine = _make_engine(kelly_enabled=True)

    size_no_inv = engine.compute_size(
        confidence=1.0,
        market_inventory=0.0,
        fair_value=0.70,
        market_price=0.50,
        nav=1000.0,
    )
    size_with_inv = engine.compute_size(
        confidence=1.0,
        market_inventory=50.0,
        fair_value=0.70,
        market_price=0.50,
        nav=1000.0,
    )
    assert size_with_inv < size_no_inv

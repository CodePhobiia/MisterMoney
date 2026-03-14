"""Integration tests: FV -> Kelly -> Quote -> RiskLimits pipeline.

Validates end-to-end quote computation with Kelly sizing, risk limit
enforcement, and min-edge filtering.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pmm1.risk.limits import RiskConfig, RiskLimits
from pmm1.settings import PricingConfig
from pmm1.state.positions import PositionTracker
from pmm1.strategy.features import FeatureVector
from pmm1.strategy.quote_engine import QuoteEngine


def _default_features(**overrides: object) -> FeatureVector:
    """Create a minimal FeatureVector for testing."""
    defaults = {
        "midpoint": 0.50,
        "microprice": 0.50,
        "vol_regime": "normal",
        "time_to_resolution_hours": 48.0,
        "sweep_intensity": 0.0,
        "is_stale": False,
    }
    defaults.update(overrides)
    return FeatureVector(**defaults)


class TestKellyQuoteStraddlesFairValue:
    """With Kelly enabled, bid < fair_value < ask must hold."""

    def test_kelly_quote_straddles_fair_value(self) -> None:
        cfg = PricingConfig(kelly_enabled=True, kelly_fraction=0.25, kelly_min_edge=0.03)
        engine = QuoteEngine(config=cfg)
        features = _default_features()

        quote = engine.compute_quote(
            token_id="tok1",
            features=features,
            fair_value=0.60,
            haircut=0.01,
            confidence=0.9,
            market_inventory=0.0,
            tick_size=0.01,
            market_price=0.50,
            nav=1000.0,
            condition_id="cond1",
        )

        assert quote.bid_price is not None
        assert quote.ask_price is not None
        assert quote.bid_price < 0.60, (
            f"bid_price {quote.bid_price} must be below fair_value 0.60"
        )
        assert quote.ask_price > 0.60, (
            f"ask_price {quote.ask_price} must be above fair_value 0.60"
        )


class TestKellySizeProportionalToEdge:
    """Bigger edge should produce larger Kelly size."""

    def test_kelly_size_proportional_to_edge(self) -> None:
        cfg = PricingConfig(
            kelly_enabled=True,
            kelly_fraction=0.25,
            kelly_min_edge=0.03,
            kelly_max_position_nav=0.50,  # High cap so it doesn't bind
        )
        engine = QuoteEngine(config=cfg, max_dollar_size=500.0)

        # Use the same fair_value for both so shares are comparable
        # (compute_size divides dollar_size by fair_value to get shares).
        # Small edge: fair_value=0.55, market=0.50 -> edge=0.05
        small_size = engine.compute_size(
            confidence=0.9,
            market_inventory=0.0,
            fair_value=0.50,
            market_price=0.45,
            nav=1000.0,
            edge_confidence=1.0,
        )

        # Big edge: fair_value=0.70, market=0.50 -> edge=0.20
        big_size = engine.compute_size(
            confidence=0.9,
            market_inventory=0.0,
            fair_value=0.50,
            market_price=0.30,
            nav=1000.0,
            edge_confidence=1.0,
        )

        assert big_size > small_size, (
            f"Big-edge size ({big_size}) must exceed small-edge size ({small_size})"
        )


class TestRiskLimitsConstrainQuote:
    """Per-market gross limit should reject quotes that exceed it."""

    def test_risk_limits_constrain_quote(self) -> None:
        risk_cfg = RiskConfig(per_market_gross_nav=0.001)  # Tiny: 0.1% NAV
        tracker = PositionTracker()
        inventory_mgr = MagicMock()  # InventoryManager not used by check_per_market_gross
        limits = RiskLimits(
            config=risk_cfg,
            position_tracker=tracker,
            inventory_manager=inventory_mgr,
        )
        limits.update_nav(100.0)  # NAV = $100 -> limit = $0.10

        # Propose adding $5 to a market (way above $0.10 limit)
        result = limits.check_per_market_gross(
            condition_id="cond1",
            proposed_additional_dollars=5.0,
        )

        assert result.passed is False, (
            "Per-market gross check must fail when proposed exposure exceeds limit"
        )
        assert len(result.breaches) > 0


class TestNoTradeWhenBelowMinEdge:
    """Kelly sizing must return 0 when edge < min_edge."""

    def test_no_trade_when_below_min_edge(self) -> None:
        cfg = PricingConfig(
            kelly_enabled=True,
            kelly_fraction=0.25,
            kelly_min_edge=0.03,
        )
        engine = QuoteEngine(config=cfg)

        # Edge = |0.51 - 0.50| = 0.01 < min_edge=0.03
        size = engine.compute_size(
            confidence=0.9,
            market_inventory=0.0,
            fair_value=0.51,
            market_price=0.50,
            nav=1000.0,
            edge_confidence=1.0,
        )

        assert size == 0.0, (
            f"Size must be 0.0 when edge (0.01) < min_edge (0.03), got {size}"
        )

"""Tests for risk limits — per-market, per-event, directional, apply_to_quote preserves asks."""

import pytest

from pmm1.risk.correlation import ThematicCorrelation
from pmm1.risk.limits import RiskLimits
from pmm1.settings import RiskConfig
from pmm1.state.inventory import InventoryManager
from pmm1.state.orders import OrderTracker
from pmm1.state.positions import PositionTracker
from pmm1.strategy.quote_engine import QuoteIntent


def _make_limits(
    nav: float = 100.0,
    per_market_gross_nav: float = 0.02,
    per_event_cluster_nav: float = 0.05,
    total_directional_nav: float = 0.10,
    correlation: ThematicCorrelation | None = None,
) -> RiskLimits:
    config = RiskConfig(
        per_market_gross_nav=per_market_gross_nav,
        per_event_cluster_nav=per_event_cluster_nav,
        total_directional_nav=total_directional_nav,
    )
    pos_tracker = PositionTracker()
    order_tracker = OrderTracker()
    inv_manager = InventoryManager(pos_tracker, order_tracker)
    limits = RiskLimits(config, pos_tracker, inv_manager, correlation=correlation)
    limits.update_nav(nav)
    return limits


class TestPerMarketGross:
    def test_within_limit(self):
        limits = _make_limits(nav=100.0, per_market_gross_nav=0.08)
        result = limits.check_per_market_gross("cond-1", proposed_additional_dollars=5.0)
        assert result.passed is True

    def test_exceeds_limit(self):
        limits = _make_limits(nav=100.0, per_market_gross_nav=0.02)
        result = limits.check_per_market_gross("cond-1", proposed_additional_dollars=3.0)
        assert result.passed is False

    def test_zero_nav_passes(self):
        limits = _make_limits(nav=0.0)
        result = limits.check_per_market_gross("cond-1", proposed_additional_dollars=999.0)
        assert result.passed is True


class TestPerEventCluster:
    def test_within_limit(self):
        limits = _make_limits(nav=100.0, per_event_cluster_nav=0.10)
        result = limits.check_per_event_cluster("event-1", proposed_additional=5.0)
        assert result.passed is True

    def test_exceeds_limit(self):
        limits = _make_limits(nav=100.0, per_event_cluster_nav=0.05)
        result = limits.check_per_event_cluster("event-1", proposed_additional=6.0)
        assert result.passed is False

    def test_uses_mark_to_market_exposure_not_share_counts(self):
        limits = _make_limits(nav=100.0, per_event_cluster_nav=0.02)
        pos = limits.positions.register_market(
            "cond-1",
            "yes-1",
            "no-1",
            event_id="event-1",
        )
        pos.yes_size = 10.0
        pos.yes_avg_price = 0.90
        pos.yes_cost_basis = 9.0
        limits.set_price_oracle_provider(lambda: {"yes-1": 0.10, "no-1": 0.90})

        result = limits.check_per_event_cluster("event-1", proposed_additional=0.5)

        assert result.passed is True


class TestTotalDirectional:
    def test_within_limit(self):
        limits = _make_limits(nav=100.0, total_directional_nav=0.10)
        result = limits.check_total_directional(proposed_additional_net=5.0)
        assert result.passed is True

    def test_exceeds_limit(self):
        limits = _make_limits(nav=100.0, total_directional_nav=0.10)
        result = limits.check_total_directional(proposed_additional_net=15.0)
        assert result.passed is False

    def test_uses_mark_to_market_directional_exposure(self):
        limits = _make_limits(nav=100.0, total_directional_nav=0.02)
        pos = limits.positions.register_market("cond-1", "yes-1", "no-1")
        pos.yes_size = 10.0
        pos.yes_avg_price = 0.90
        pos.yes_cost_basis = 9.0
        limits.set_price_oracle_provider(lambda: {"yes-1": 0.10, "no-1": 0.90})

        result = limits.check_total_directional(proposed_additional_net=0.5)

        assert result.passed is True


class TestApplyToQuote:
    def _make_intent(self, bid_price=0.50, bid_size=10.0, ask_price=0.51, ask_size=10.0):
        return QuoteIntent(
            condition_id="cond-1",
            token_id="tok-yes",
            bid_price=bid_price,
            bid_size=bid_size,
            ask_price=ask_price,
            ask_size=ask_size,
        )

    def test_preserves_asks_on_per_market_breach(self):
        """Per-market risk limits should never zero asks — asks REDUCE exposure."""
        limits = _make_limits(nav=100.0, per_market_gross_nav=0.01)  # Very tight
        intent = self._make_intent(bid_size=100.0, ask_size=100.0)
        result = limits.apply_to_quote(intent)
        # Ask must be preserved even if bid is zeroed by per-market check
        # Note: directional check may scale both sides, but ask should still be > 0
        assert result.ask_size > 0
        assert result.ask_price == 0.51

    def test_bid_zeroed_on_breach(self):
        limits = _make_limits(nav=100.0, per_market_gross_nav=0.01)
        intent = self._make_intent(bid_size=100.0, bid_price=0.50)
        result = limits.apply_to_quote(intent)
        # Bid should be zeroed (100 * 0.5 = $50 >> 1% of $100 = $1)
        assert result.bid_size == 0 or (result.bid_size * (result.bid_price or 0) <= 1.0)

    def test_passes_clean(self):
        limits = _make_limits(nav=1000.0, per_market_gross_nav=0.10)
        intent = self._make_intent(bid_size=10.0, ask_size=10.0)
        result = limits.apply_to_quote(intent)
        assert result.bid_size == 10.0
        assert result.ask_size == 10.0

    def test_diagnostics_report_bid_reduction_reason(self):
        limits = _make_limits(nav=100.0, per_market_gross_nav=0.01)
        intent = self._make_intent(bid_size=100.0, ask_size=10.0)

        _, diagnostics = limits.apply_to_quote_with_diagnostics(intent)

        assert "per_market_gross" in diagnostics.bid_reasons


class TestThematicCorrelation:
    def test_classify_election(self):
        tc = ThematicCorrelation()
        theme = tc.classify("c1", "Will Trump win the 2024 election?")
        assert theme == "US_ELECTION"

    def test_classify_crypto(self):
        tc = ThematicCorrelation()
        theme = tc.classify("c2", "Bitcoin price above $100k?")
        assert theme == "CRYPTO_BTC"

    def test_classify_uncorrelated(self):
        tc = ThematicCorrelation()
        theme = tc.classify("c3", "Will it rain tomorrow?")
        assert theme == "uncorrelated"

    def test_theme_limit_check_passes(self):
        tc = ThematicCorrelation(per_theme_nav=0.15)
        tc.classify("c1", "Trump wins")
        pos_tracker = PositionTracker()
        passed, _ = tc.check_theme_limit("c1", 5.0, 100.0, pos_tracker)
        assert passed is True

    def test_uncorrelated_always_passes(self):
        tc = ThematicCorrelation(per_theme_nav=0.01)
        tc.classify("c1", "Random market question")
        pos_tracker = PositionTracker()
        passed, _ = tc.check_theme_limit("c1", 999.0, 100.0, pos_tracker)
        assert passed is True

    def test_theme_limit_uses_mark_to_market_prices(self):
        tc = ThematicCorrelation(per_theme_nav=0.02)
        tc.classify("c1", "Will Trump win?")
        pos_tracker = PositionTracker()
        pos = pos_tracker.register_market("c1", "yes-1", "no-1", event_id="event-1")
        pos.yes_size = 10.0
        pos.yes_avg_price = 0.90
        pos.yes_cost_basis = 9.0

        passed, remaining = tc.check_theme_limit(
            "c1",
            0.5,
            100.0,
            pos_tracker,
            price_oracle={"yes-1": 0.10, "no-1": 0.90},
        )

        assert passed is True
        assert remaining == pytest.approx(1.0)

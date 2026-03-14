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

    def test_zero_nav_blocks(self):
        limits = _make_limits(nav=0.0)
        result = limits.check_per_market_gross("cond-1", proposed_additional_dollars=999.0)
        assert result.passed is False
        assert any("nav_unavailable" in b for b in result.breaches)


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

    def test_buy_increasing_exposure_past_limit_is_blocked(self):
        """A buy that pushes net exposure past the limit should be blocked."""
        limits = _make_limits(nav=1000.0, total_directional_nav=0.10)  # limit = 100
        # Simulate existing directional exposure of 90 (near limit of 100)
        pos = limits.positions.register_market("cond-1", "yes-1", "no-1")
        pos.yes_size = 100.0
        pos.yes_avg_price = 0.90
        pos.yes_cost_basis = 90.0
        # Mark-to-market: 100 shares * 0.90 = 90 net exposure
        limits.set_price_oracle_provider(lambda: {"yes-1": 0.90, "no-1": 0.10})

        # Proposed buy adds 20 more → abs(90 + 20) = 110 > 100 → blocked
        result = limits.check_total_directional(proposed_additional_net=20.0)
        assert result.passed is False
        assert any("total_directional" in b for b in result.breaches)

    def test_signed_input_hedging_reduces_exposure(self):
        """A negative proposed_additional_net (hedge/sell) should reduce net exposure."""
        limits = _make_limits(nav=1000.0, total_directional_nav=0.10)  # limit = 100
        # Simulate existing directional exposure of 80
        pos = limits.positions.register_market("cond-1", "yes-1", "no-1")
        pos.yes_size = 100.0
        pos.yes_avg_price = 0.80
        pos.yes_cost_basis = 80.0
        limits.set_price_oracle_provider(lambda: {"yes-1": 0.80, "no-1": 0.20})

        # Proposed hedge (sell) reduces exposure: abs(80 + (-30)) = 50 < 100 → pass
        result = limits.check_total_directional(proposed_additional_net=-30.0)
        assert result.passed is True

    def test_exactly_at_limit_passes(self):
        """Exposure exactly at the limit should pass (not strictly greater)."""
        limits = _make_limits(nav=1000.0, total_directional_nav=0.10)  # limit = 100
        # No existing positions, so current_net = 0
        # Proposed = 100 → abs(0 + 100) = 100, limit = 100 → 100 > 100 is False → pass
        result = limits.check_total_directional(proposed_additional_net=100.0)
        assert result.passed is True

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


class TestAskSizeLimit:
    """KP-06: Asymmetric risk limits for asks."""

    def test_ask_size_capped(self):
        """SELL order for 100 shares when position is 100 → capped at 50."""
        limits = _make_limits(nav=1000.0)
        pos = limits.positions.register_market("cond-1", "yes-1", "no-1")
        pos.yes_size = 100.0
        pos.yes_avg_price = 0.50
        pos.yes_cost_basis = 50.0

        result = limits.check_ask_size("cond-1", proposed_sell_size=100.0)
        assert result.passed is False
        assert any("ask_size_exceeds_position_pct" in b for b in result.breaches)
        assert result.adjustments["max_sell_size"] == pytest.approx(50.0)

    def test_ask_within_limit_passes(self):
        """Sell 40 out of 100 shares (40% < 50%) → passes."""
        limits = _make_limits(nav=1000.0)
        pos = limits.positions.register_market("cond-1", "yes-1", "no-1")
        pos.yes_size = 100.0
        pos.yes_avg_price = 0.50
        pos.yes_cost_basis = 50.0

        result = limits.check_ask_size("cond-1", proposed_sell_size=40.0)
        assert result.passed is True

    def test_ask_no_position_passes(self):
        """No position → sell check passes (nothing to cap against)."""
        limits = _make_limits(nav=1000.0)
        result = limits.check_ask_size("cond-unknown", proposed_sell_size=100.0)
        assert result.passed is True

    def test_apply_to_quote_caps_ask_size(self):
        """apply_to_quote should cap ask size based on position."""
        limits = _make_limits(nav=1000.0, per_market_gross_nav=0.10)
        pos = limits.positions.register_market("cond-1", "yes-1", "no-1")
        pos.yes_size = 100.0
        pos.yes_avg_price = 0.50
        pos.yes_cost_basis = 50.0

        intent = QuoteIntent(
            condition_id="cond-1",
            token_id="yes-1",
            bid_price=0.50,
            bid_size=5.0,
            ask_price=0.51,
            ask_size=100.0,  # Trying to sell entire position
        )
        result, diagnostics = limits.apply_to_quote_with_diagnostics(intent)
        # Ask should be capped at 50% of 100 = 50
        assert result.ask_size == pytest.approx(50.0)
        assert "ask_size_exceeds_position_pct" in diagnostics.ask_reasons


class TestNavZeroBlocks:
    def test_nav_zero_blocks_all_checks(self):
        """R-C1: All 4 risk checks must fail when NAV is zero."""
        limits = _make_limits(nav=0.0)

        r1 = limits.check_per_market_gross("cond-1", proposed_additional_dollars=1.0)
        assert r1.passed is False
        assert any("nav_unavailable" in b for b in r1.breaches)

        r2 = limits.check_per_event_cluster("event-1", proposed_additional=1.0)
        assert r2.passed is False
        assert any("nav_unavailable" in b for b in r2.breaches)

        r3 = limits.check_total_directional(proposed_additional_net=1.0)
        assert r3.passed is False
        assert any("nav_unavailable" in b for b in r3.breaches)

        r4 = limits.check_total_arb_gross(proposed_additional=1.0)
        assert r4.passed is False
        assert any("nav_unavailable" in b for b in r4.breaches)


# ── KP-03: Dynamic Theme Correlation ──


class TestDynamicThemeCorrelation:
    def test_theme_rho_defaults(self):
        """KP-03: Default rho values for each theme."""
        tc = ThematicCorrelation()
        tc.classify("c1", "Will Trump win the election?")
        tc.classify("c2", "Bitcoin above $100k?")
        tc.classify("c3", "Ethereum merge success?")
        tc.classify("c4", "Solana TPS record?")
        tc.classify("c5", "Crypto market cap?")
        tc.classify("c6", "Russia Ukraine war?")
        tc.classify("c7", "Fed rate cut?")
        tc.classify("c8", "OpenAI AGI?")

        assert tc.get_theme_rho("c1") == 0.30  # US_ELECTION
        assert tc.get_theme_rho("c2") == 0.25  # CRYPTO_BTC
        assert tc.get_theme_rho("c3") == 0.25  # CRYPTO_ETH
        assert tc.get_theme_rho("c4") == 0.25  # CRYPTO_SOL
        assert tc.get_theme_rho("c5") == 0.20  # CRYPTO_GENERAL
        assert tc.get_theme_rho("c6") == 0.20  # GEOPOLITICS
        assert tc.get_theme_rho("c7") == 0.15  # FED_RATES
        assert tc.get_theme_rho("c8") == 0.10  # AI_TECH

    def test_record_outcome_updates_rho(self):
        """KP-03: After 20+ outcomes, rho changes from its prior."""
        tc = ThematicCorrelation()
        tc.classify("c1", "Will Trump win?")

        prior_rho = tc.get_theme_rho("c1")
        assert prior_rho == 0.30

        # Record 25 correlated outcomes (alternating high/low to
        # create positive lag-1 autocorrelation)
        for i in range(25):
            # Pattern: 0.8, 0.8, 0.2, 0.2, ... creates lag-1 correlation
            if (i // 2) % 2 == 0:
                tc.record_outcome("c1", 0.8)
            else:
                tc.record_outcome("c1", 0.2)

        updated_rho = tc.get_theme_rho("c1")
        # Rho should have moved from the prior (blended 70/30)
        assert updated_rho != prior_rho

    def test_get_theme_rho_uncorrelated(self):
        """KP-03: Uncorrelated theme returns default_rho."""
        tc = ThematicCorrelation()
        tc.classify("c_random", "Will it rain tomorrow?")

        rho = tc.get_theme_rho("c_random")
        assert rho == tc._default_rho
        assert rho == 0.03

    def test_record_outcome_uncorrelated_ignored(self):
        """KP-03: Recording outcome for uncorrelated market is a no-op."""
        tc = ThematicCorrelation()
        tc.classify("c_random", "Will it rain?")

        tc.record_outcome("c_random", 1.0)
        assert "uncorrelated" not in tc._theme_outcomes

    def test_outcome_buffer_capped_at_200(self):
        """KP-03: Outcome buffer is capped at 200 entries."""
        tc = ThematicCorrelation()
        tc.classify("c1", "Bitcoin price above $50k?")

        for i in range(250):
            tc.record_outcome("c1", float(i % 2))

        assert len(tc._theme_outcomes["CRYPTO_BTC"]) == 200

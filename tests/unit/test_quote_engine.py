"""Comprehensive unit tests for QuoteEngine — reservation price, spread, size, quote, crossing."""

from __future__ import annotations

import pytest

from pmm1.settings import PricingConfig
from pmm1.strategy.features import FeatureVector
from pmm1.strategy.quote_engine import QuoteEngine, QuoteIntent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_features(**overrides) -> FeatureVector:
    """Return a FeatureVector with sensible defaults; any field can be overridden."""
    defaults = dict(
        midpoint=0.50,
        microprice=0.50,
        imbalance=0.0,
        spread=0.02,
        spread_cents=2.0,
        best_bid=0.49,
        best_ask=0.51,
        bid_depth_2c=100.0,
        ask_depth_2c=100.0,
        signed_trade_flow=0.0,
        trade_intensity=0.0,
        sweep_intensity=0.0,
        realized_vol=0.005,
        vol_regime="normal",
        time_to_resolution_hours=48.0,
        time_to_resolution_fraction=0.5,
        related_market_residual=0.0,
        external_signal=0.0,
        token_id="tok_test",
        condition_id="cond_test",
        timestamp=1_700_000_000.0,
        is_stale=False,
    )
    defaults.update(overrides)
    return FeatureVector(**defaults)


def _default_config(**overrides) -> PricingConfig:
    """Return a PricingConfig with production defaults; any field can be overridden."""
    return PricingConfig(**overrides)


def _engine(config: PricingConfig | None = None, **kwargs) -> QuoteEngine:
    """Build a QuoteEngine with default config."""
    cfg = config or _default_config()
    return QuoteEngine(config=cfg, **kwargs)


# ===================================================================
# compute_reservation_price
# ===================================================================


class TestComputeReservationPrice:
    """Tests 1-6: reservation price with inventory skew and clipping."""

    def test_zero_inventory_equals_fair_value(self):
        """#1 — With zero inventory the reservation price equals fair_value."""
        eng = _engine()
        r = eng.compute_reservation_price(fair_value=0.60, market_inventory=0.0)
        assert r == pytest.approx(0.60)

    def test_positive_inventory_pushes_price_down(self):
        """#2 — Long inventory pushes reservation price below fair_value."""
        eng = _engine()
        fv = 0.50
        r = eng.compute_reservation_price(fair_value=fv, market_inventory=5.0)
        assert r < fv, "Reservation price should be pushed down when long"

    def test_negative_inventory_pushes_price_up(self):
        """#3 — Short inventory pushes reservation price above fair_value."""
        eng = _engine()
        fv = 0.50
        r = eng.compute_reservation_price(fair_value=fv, market_inventory=-5.0)
        assert r > fv, "Reservation price should be pushed up when short"

    def test_dynamic_gamma_ramp_zero_hours(self):
        """#4a — At 0 position age hours, gamma equals base gamma."""
        eng = _engine()
        # With gamma_base only, skew = gamma_base * inventory
        gamma_base = eng.config.inventory_skew_gamma  # 0.015
        inv = 3.0
        r = eng.compute_reservation_price(
            fair_value=0.50, market_inventory=inv, position_age_hours=0.0,
        )
        expected = 0.50 - gamma_base * inv
        assert r == pytest.approx(expected)

    def test_dynamic_gamma_ramp_large_hours(self):
        """#4b — At very large position age, gamma approaches gamma_max."""
        cfg = _default_config(inventory_skew_gamma=0.015, gamma_max=0.05, age_halflife_hours=4.0)
        eng = _engine(config=cfg)
        inv = 3.0
        fv = 0.50
        # After many half-lives (e.g. 200 hours), gamma ~ gamma_max
        r = eng.compute_reservation_price(
            fair_value=fv, market_inventory=inv, position_age_hours=200.0,
        )
        expected_approx = fv - cfg.gamma_max * inv
        assert r == pytest.approx(expected_approx, abs=0.001)

    def test_cluster_inventory_skew_with_eta(self):
        """#5 — Cluster inventory applies eta coefficient."""
        cfg = _default_config(cluster_skew_eta=0.02)
        eng = _engine(config=cfg)
        fv = 0.50
        cluster_inv = 10.0
        r = eng.compute_reservation_price(
            fair_value=fv, market_inventory=0.0, cluster_inventory=cluster_inv,
        )
        expected = fv - cfg.cluster_skew_eta * cluster_inv
        assert r == pytest.approx(expected)

    def test_price_clipping_lower_bound(self):
        """#6a — Reservation price cannot go below epsilon = 0.005."""
        eng = _engine()
        # Push price extremely low: fair_value=0.01, huge positive inventory
        r = eng.compute_reservation_price(fair_value=0.01, market_inventory=100.0)
        assert r == pytest.approx(0.005)

    def test_price_clipping_upper_bound(self):
        """#6b — Reservation price cannot go above 1 - epsilon = 0.995."""
        eng = _engine()
        # Push price extremely high: fair_value=0.99, huge negative inventory
        r = eng.compute_reservation_price(fair_value=0.99, market_inventory=-100.0)
        assert r == pytest.approx(0.995)


# ===================================================================
# compute_half_spread
# ===================================================================


class TestComputeHalfSpread:
    """Tests 7-11: half-spread widening and tightening components."""

    def test_toxicity_widening_high_sweep(self):
        """#7 — sweep_intensity > 0.5 adds +0.5c (0.005)."""
        eng = _engine()
        feat_calm = _default_features(sweep_intensity=0.0)
        feat_toxic = _default_features(sweep_intensity=0.6)
        delta_calm = eng.compute_half_spread(feat_calm)
        delta_toxic = eng.compute_half_spread(feat_toxic)
        assert delta_toxic - delta_calm == pytest.approx(0.005)

    def test_volatility_regime_extreme_widening(self):
        """#8 — 'extreme' vol regime adds +1c (0.01) vs normal's +0.1c."""
        eng = _engine()
        feat_normal = _default_features(vol_regime="normal")
        feat_extreme = _default_features(vol_regime="extreme")
        delta_normal = eng.compute_half_spread(feat_normal)
        delta_extreme = eng.compute_half_spread(feat_extreme)
        # Extreme adds 0.01, normal adds 0.001 → difference = 0.009
        assert delta_extreme - delta_normal == pytest.approx(0.009)

    def test_stale_data_latency_widening(self):
        """#9 — is_stale=True adds +0.3c (0.003)."""
        eng = _engine()
        feat_fresh = _default_features(is_stale=False)
        feat_stale = _default_features(is_stale=True)
        delta_fresh = eng.compute_half_spread(feat_fresh)
        delta_stale = eng.compute_half_spread(feat_stale)
        assert delta_stale - delta_fresh == pytest.approx(0.003)

    def test_reward_discount_tightens_spread(self):
        """#10 — Positive reward_ev tightens the half-spread."""
        eng = _engine()
        feat = _default_features()
        delta_no_reward = eng.compute_half_spread(feat, reward_ev=0.0)
        delta_with_reward = eng.compute_half_spread(feat, reward_ev=0.01)
        assert delta_with_reward < delta_no_reward

    def test_minimum_half_spread_is_half_tick(self):
        """#11 — Half-spread never falls below tick_size / 2."""
        # Use very aggressive reward_ev to try to push spread below floor
        cfg = _default_config(base_half_spread_cents=0.1, reward_capture_weight=100.0)
        eng = _engine(config=cfg)
        feat = _default_features()
        tick = 0.01
        delta = eng.compute_half_spread(feat, tick_size=tick, reward_ev=1.0)
        assert delta >= tick / 2.0


# ===================================================================
# compute_size
# ===================================================================


class TestComputeSize:
    """Tests 12-16: size model with Kelly and dollar-flat modes."""

    def test_kelly_mode_proportional_to_edge(self):
        """#12 — Kelly sizing: larger edge yields larger size."""
        cfg = _default_config(kelly_enabled=True, kelly_min_edge=0.01)
        eng = _engine(config=cfg)
        size_small_edge = eng.compute_size(
            confidence=1.0, market_inventory=0.0,
            fair_value=0.55, market_price=0.50, nav=100.0,
        )
        size_large_edge = eng.compute_size(
            confidence=1.0, market_inventory=0.0,
            fair_value=0.65, market_price=0.50, nav=100.0,
        )
        assert size_large_edge > size_small_edge

    def test_dollar_flat_mode(self):
        """#13 — Dollar-flat sizing: target_dollar_size / price."""
        cfg = _default_config(kelly_enabled=False)
        eng = _engine(config=cfg, target_dollar_size=8.0)
        # With confidence=1, inventory=0, normal vol, no catalyst discount
        size = eng.compute_size(
            confidence=1.0, market_inventory=0.0, fair_value=0.50,
        )
        # target_shares = 8.0 / 0.50 = 16.0
        # max_shares = 15.0 / 0.50 = 30.0
        # vol_multiplier normal = 1.0; no catalyst discount
        # size = 16.0 * 1.0 * 1.0 / (1 + 0.1*0) = 16.0
        assert size == pytest.approx(16.0)

    def test_volatility_discount_extreme(self):
        """#14a — 'extreme' volatility regime → 0.3x multiplier."""
        cfg = _default_config(kelly_enabled=False)
        eng = _engine(config=cfg, target_dollar_size=8.0, max_dollar_size=100.0)
        size_normal = eng.compute_size(
            confidence=1.0, market_inventory=0.0,
            fair_value=0.50, volatility_regime="normal",
        )
        size_extreme = eng.compute_size(
            confidence=1.0, market_inventory=0.0,
            fair_value=0.50, volatility_regime="extreme",
        )
        # normal multiplier = 1.0, extreme = 0.3
        # Both start at 16 shares; extreme = 16*0.3 = 4.8, but min is 5.0
        assert size_extreme < size_normal
        # extreme should be 0.3x before the floor
        raw_extreme = 16.0 * 0.3  # 4.8
        assert size_extreme == pytest.approx(max(5.0, raw_extreme))

    def test_volatility_discount_high(self):
        """#14b — 'high' volatility regime → 0.6x multiplier."""
        cfg = _default_config(kelly_enabled=False)
        eng = _engine(config=cfg, target_dollar_size=8.0, max_dollar_size=100.0)
        size_normal = eng.compute_size(
            confidence=1.0, market_inventory=0.0,
            fair_value=0.50, volatility_regime="normal",
        )
        size_high = eng.compute_size(
            confidence=1.0, market_inventory=0.0,
            fair_value=0.50, volatility_regime="high",
        )
        # normal 16, high 16*0.6=9.6
        assert size_high == pytest.approx(size_normal * 0.6)

    def test_multi_bet_kelly_adjustment(self):
        """#15 — More active positions → smaller Kelly size."""
        cfg = _default_config(kelly_enabled=True, kelly_min_edge=0.01)
        eng = _engine(config=cfg)
        size_1 = eng.compute_size(
            confidence=1.0, market_inventory=0.0,
            fair_value=0.60, market_price=0.50, nav=100.0,
            n_active_positions=1,
        )
        size_5 = eng.compute_size(
            confidence=1.0, market_inventory=0.0,
            fair_value=0.60, market_price=0.50, nav=100.0,
            n_active_positions=5,
        )
        assert size_5 < size_1

    def test_kelly_zero_edge_returns_zero(self):
        """#16 — Edge below min_edge → size is 0.0 (before floor)."""
        cfg = _default_config(kelly_enabled=True, kelly_min_edge=0.03)
        eng = _engine(config=cfg)
        # Edge = |0.51 - 0.50| = 0.01 < 0.03 min_edge → kelly returns 0
        size = eng.compute_size(
            confidence=1.0, market_inventory=0.0,
            fair_value=0.51, market_price=0.50, nav=100.0,
        )
        assert size == pytest.approx(0.0)


# ===================================================================
# compute_quote
# ===================================================================


class TestComputeQuote:
    """Tests 17-21: full pipeline, invariants, Polymarket minimums, asymmetric sizing."""

    def _make_quote(self, *, inventory=0.0, fair_value=0.50, **kw) -> QuoteIntent:
        """Helper to build a quote with sensible defaults."""
        eng = _engine()
        feat = _default_features(**{k: v for k, v in kw.items() if k in FeatureVector.model_fields})
        quote_kw = {k: v for k, v in kw.items() if k not in FeatureVector.model_fields}
        return eng.compute_quote(
            token_id="tok_test",
            features=feat,
            fair_value=fair_value,
            haircut=0.01,
            confidence=0.9,
            market_inventory=inventory,
            tick_size=0.01,
            **quote_kw,
        )

    def test_full_pipeline_bid_below_reservation_below_ask(self):
        """#17 — bid < reservation < ask for a standard quote."""
        q = self._make_quote()
        assert q.bid_price is not None
        assert q.ask_price is not None
        assert q.bid_price < q.reservation_price < q.ask_price

    def test_bid_less_than_ask_invariant_extreme_inventory(self):
        """#18 — Bid < ask even at extreme inventory levels."""
        for inv in [-50, -10, 0, 10, 50]:
            q = self._make_quote(inventory=float(inv))
            assert q.bid_price is not None
            assert q.ask_price is not None
            assert q.bid_price < q.ask_price, (
                f"bid >= ask at inventory={inv}: "
                f"bid={q.bid_price}, ask={q.ask_price}"
            )

    def test_polymarket_minimum_shares(self):
        """#19 — Bid and ask sizes are at least max(5 shares, $1.50/price)."""
        q = self._make_quote(fair_value=0.50)
        assert q.bid_price is not None and q.bid_price > 0
        assert q.ask_price is not None and q.ask_price > 0
        min_bid = max(5.0, 1.5 / q.bid_price)
        min_ask = max(5.0, 1.5 / q.ask_price)
        assert q.bid_size >= min_bid - 1e-9
        assert q.ask_size >= min_ask - 1e-9

    def test_asymmetric_sizing_long_inventory(self):
        """#20 — Long inventory → bid_size < ask_size (discourage buying)."""
        q = self._make_quote(inventory=10.0)
        assert q.bid_size is not None
        assert q.ask_size is not None
        assert q.bid_size < q.ask_size, (
            f"With long inventory, bid_size ({q.bid_size}) should be "
            f"< ask_size ({q.ask_size})"
        )

    def test_asymmetric_sizing_short_inventory(self):
        """#21 — Short inventory → ask_size < bid_size (discourage selling)."""
        q = self._make_quote(inventory=-10.0)
        assert q.bid_size is not None
        assert q.ask_size is not None
        assert q.ask_size < q.bid_size, (
            f"With short inventory, ask_size ({q.ask_size}) should be "
            f"< bid_size ({q.bid_size})"
        )


# ===================================================================
# check_crossing_rule
# ===================================================================


class TestCheckCrossingRule:
    """Tests 22-24: crossing rule for aggressive fills."""

    def test_buy_crossing_positive_edge_passes(self):
        """#22 — BUY with sufficient edge passes the crossing rule."""
        eng = _engine()
        should_cross, take_ev = eng.check_crossing_rule(
            fair_value=0.60,
            execution_price=0.50,
            side="BUY",
            haircut=0.01,
        )
        assert should_cross is True
        assert take_ev > 0

    def test_sell_crossing_positive_edge_passes(self):
        """#23 — SELL with sufficient edge passes the crossing rule."""
        eng = _engine()
        should_cross, take_ev = eng.check_crossing_rule(
            fair_value=0.40,
            execution_price=0.50,
            side="SELL",
            haircut=0.01,
        )
        assert should_cross is True
        assert take_ev > 0

    def test_crossing_below_threshold_rejected(self):
        """#24 — Tiny edge below take_threshold is rejected."""
        cfg = _default_config(take_threshold_cents=0.8)
        eng = _engine(config=cfg)
        # Edge = |0.505 - 0.50| = 0.005 in price, minus haircut 0.005 = 0.0
        # 0.0 < threshold 0.008 → rejected
        should_cross, take_ev = eng.check_crossing_rule(
            fair_value=0.505,
            execution_price=0.50,
            side="BUY",
            haircut=0.005,
        )
        assert should_cross is False


# ===================================================================
# Q-H1: Kelly no share floor
# ===================================================================


class TestKellyNoShareFloor:
    """Q-H1: Kelly sizing should not inflate to 5-share minimum."""

    def test_kelly_no_share_floor(self):
        """Kelly with tiny edge returns 0, not 5 shares."""
        cfg = _default_config(kelly_enabled=True, kelly_min_edge=0.03)
        eng = _engine(config=cfg)
        # Edge = |0.51 - 0.50| = 0.01 < 0.03 min_edge → kelly returns 0
        size = eng.compute_size(
            confidence=1.0,
            market_inventory=0.0,
            fair_value=0.51,
            market_price=0.50,
            nav=100.0,
        )
        assert size == 0.0, (
            f"Kelly with sub-threshold edge should return 0.0, got {size}"
        )

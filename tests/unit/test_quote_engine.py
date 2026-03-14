"""Comprehensive unit tests for QuoteEngine — reservation price, spread, size, quote, crossing."""

from __future__ import annotations

import math

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
        """#4a — At 0 position age hours and zero inventory, gamma equals base gamma."""
        eng = _engine()
        # With gamma_base only and zero inventory (no MM-11 urgency),
        # reservation price equals fair value.
        _ = eng.config.inventory_skew_gamma  # 0.015 (gamma_base)
        r = eng.compute_reservation_price(
            fair_value=0.50, market_inventory=0.0, position_age_hours=0.0,
        )
        assert r == pytest.approx(0.50)

    def test_dynamic_gamma_at_zero_age_small_inventory(self):
        """#4a-bis — At 0 age, small inventory activates MM-11 inv_urgency."""
        cfg = _default_config(
            inventory_skew_gamma=0.015, gamma_max=0.05,
            max_position_shares=200.0,
        )
        eng = _engine(config=cfg)
        inv = 3.0
        # MM-11: inv_urgency = sqrt(3/200) ~ 0.1225
        inv_urgency = (inv / 200.0) ** 0.5
        expected_gamma = 0.015 + (0.05 - 0.015) * inv_urgency
        r = eng.compute_reservation_price(
            fair_value=0.50, market_inventory=inv, position_age_hours=0.0,
        )
        expected = 0.50 - expected_gamma * inv
        assert r == pytest.approx(expected, abs=1e-6)

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
    """Tests 7-11: Avellaneda-Stoikov half-spread formula."""

    def test_as_spread_positive(self):
        """#7 — A-S formula produces positive half-spread."""
        eng = _engine()
        feat = _default_features(sigma_eff=0.25, kappa_estimate=0.1)
        delta = eng.compute_half_spread(feat)
        assert delta > 0

    def test_as_spread_widens_with_vol(self):
        """#8 — Higher sigma_eff produces wider spread (A-S vol sensitivity)."""
        eng = _engine()
        feat_low = _default_features(sigma_eff=0.10, kappa_estimate=0.1)
        feat_high = _default_features(sigma_eff=0.45, kappa_estimate=0.1)
        delta_low = eng.compute_half_spread(feat_low)
        delta_high = eng.compute_half_spread(feat_high)
        assert delta_high > delta_low, (
            f"Higher sigma_eff should widen spread: {delta_high} vs {delta_low}"
        )

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
        cfg = _default_config(reward_capture_weight=100.0)
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
        """Helper to build a quote with sensible defaults.

        Uses kappa_estimate=10.0 to produce a tight A-S spread
        suitable for integration-level pipeline tests.
        """
        feat_overrides = {k: v for k, v in kw.items() if k in FeatureVector.model_fields}
        feat_overrides.setdefault("kappa_estimate", 10.0)
        eng = _engine()
        feat = _default_features(**feat_overrides)
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


# ===================================================================
# KP-01: Kelly adaptive lambda ramp
# ===================================================================


class TestKellyAdaptiveLambdaRamp:
    """KP-01: Adaptive Kelly lambda scales with edge_confidence."""

    def test_kelly_ramp_low_confidence(self):
        """Low edge_confidence=0.1 → effective_lambda near 0.125."""
        cfg = _default_config(
            kelly_enabled=True,
            kelly_base_lambda=0.10,
            kelly_max_lambda=0.35,
            kelly_min_edge=0.01,
        )
        # effective_lambda = 0.10 + (0.35 - 0.10) * 0.1 = 0.10 + 0.025 = 0.125
        expected_lambda = 0.10 + (0.35 - 0.10) * 0.1
        assert expected_lambda == pytest.approx(0.125)

        # Verify sizing is proportional: low confidence → smaller size
        eng = _engine(config=cfg)
        size_low = eng.compute_size(
            confidence=1.0, market_inventory=0.0,
            fair_value=0.60, market_price=0.50, nav=100.0,
            edge_confidence=0.1,
        )
        size_high = eng.compute_size(
            confidence=1.0, market_inventory=0.0,
            fair_value=0.60, market_price=0.50, nav=100.0,
            edge_confidence=1.0,
        )
        assert size_low < size_high, (
            f"Low edge_confidence should produce smaller size: {size_low} vs {size_high}"
        )

    def test_kelly_ramp_full_confidence(self):
        """Full edge_confidence=1.0 → effective_lambda = 0.35."""
        cfg = _default_config(
            kelly_enabled=True,
            kelly_base_lambda=0.10,
            kelly_max_lambda=0.35,
            kelly_min_edge=0.01,
        )
        expected_lambda = 0.10 + (0.35 - 0.10) * 1.0
        assert expected_lambda == pytest.approx(0.35)

        # With full confidence, kelly_bet_dollars receives lambda_frac=0.35
        # Verify this produces a reasonable non-zero size
        eng = _engine(config=cfg)
        size = eng.compute_size(
            confidence=1.0, market_inventory=0.0,
            fair_value=0.60, market_price=0.50, nav=100.0,
            edge_confidence=1.0,
        )
        assert size > 0, "Full confidence Kelly should produce positive size"


# ===================================================================
# MM-01: Avellaneda-Stoikov spread formula
# ===================================================================


class TestAvellanedaStoikovSpread:
    """MM-01: A-S closed-form spread formula verification."""

    def test_as_spread_formula(self):
        """With known sigma_eff, kappa, T_eff → verify spread matches A-S formula."""
        gamma = 0.015
        sigma_eff = 0.25
        kappa = 0.10
        time_to_res = 12.0  # hours

        cfg = _default_config(inventory_skew_gamma=gamma)
        eng = _engine(config=cfg)
        feat = _default_features(
            sigma_eff=sigma_eff,
            kappa_estimate=kappa,
            time_to_resolution_hours=time_to_res,
            is_stale=False,
        )
        delta = eng.compute_half_spread(feat, reward_ev=0.0)

        # Expected A-S half-spread
        t_eff = time_to_res / 24.0  # 0.5
        expected_as = (
            (gamma * sigma_eff ** 2 * t_eff) / 2
            + (1 / gamma) * math.log(1 + gamma / kappa)
        )
        assert delta == pytest.approx(expected_as, rel=1e-6)

    def test_as_spread_widens_with_vol(self):
        """Higher sigma_eff → wider spread (sigma^2 term dominates)."""
        cfg = _default_config(inventory_skew_gamma=0.015)
        eng = _engine(config=cfg)

        feat_low_vol = _default_features(sigma_eff=0.10, kappa_estimate=0.1)
        feat_high_vol = _default_features(sigma_eff=0.45, kappa_estimate=0.1)

        delta_low = eng.compute_half_spread(feat_low_vol)
        delta_high = eng.compute_half_spread(feat_high_vol)

        assert delta_high > delta_low, (
            f"Higher sigma_eff should widen spread: {delta_high:.6f} vs {delta_low:.6f}"
        )


# ===================================================================
# MM-11: Inventory-proportional gamma
# ===================================================================


class TestInventoryProportionalGamma:
    """MM-11: Large inventory → higher gamma even at age=0."""

    def test_inventory_proportional_gamma(self):
        """Large inventory at age=0 should increase gamma beyond base."""
        cfg = _default_config(
            inventory_skew_gamma=0.015,
            gamma_max=0.05,
            max_position_shares=200.0,
        )
        eng = _engine(config=cfg)

        # At zero inventory, reservation price = fair value
        r_zero = eng.compute_reservation_price(
            fair_value=0.50, market_inventory=0.0, position_age_hours=0.0,
        )
        assert r_zero == pytest.approx(0.50)

        # Large inventory (100 shares out of 200 max) at age=0
        inv = 100.0
        eng.compute_reservation_price(
            fair_value=0.50, market_inventory=inv, position_age_hours=0.0,
        )

        # With pure base gamma: r = 0.50 - 0.015 * 100 = -1.0 (clipped to 0.005)
        # With MM-11: inv_urgency = sqrt(100/200) = 0.707
        # effective_gamma = 0.015 + (0.05-0.015)*0.707 = 0.015 + 0.0247 = 0.0397
        # r = 0.50 - 0.0397*100 = 0.50 - 3.97 → clipped to 0.005
        # Both hit the floor at this extreme, so test with moderate inventory
        inv_mod = 5.0
        r_mod = eng.compute_reservation_price(
            fair_value=0.50, market_inventory=inv_mod, position_age_hours=0.0,
        )
        # inv_urgency = sqrt(5/200) = 0.158
        inv_urgency = (inv_mod / 200.0) ** 0.5
        effective_gamma = 0.015 + (0.05 - 0.015) * inv_urgency
        expected = 0.50 - effective_gamma * inv_mod
        assert r_mod == pytest.approx(expected, abs=1e-6)

        # Confirm gamma is above base
        assert effective_gamma > 0.015, (
            f"MM-11: effective gamma {effective_gamma} should exceed base 0.015"
        )

    def test_inventory_urgency_dominates_over_age_when_larger(self):
        """When inventory urgency > age factor, gamma uses inventory urgency."""
        cfg = _default_config(
            inventory_skew_gamma=0.015,
            gamma_max=0.05,
            age_halflife_hours=4.0,
            max_position_shares=100.0,
        )
        eng = _engine(config=cfg)

        # Large inventory (50/100 = 0.5 fraction → urgency = sqrt(0.5) = 0.707)
        # Small age (0.5 hours → age_factor = 1-exp(-0.693*0.5/4) = 0.083)
        inv = 50.0
        r = eng.compute_reservation_price(
            fair_value=0.50, market_inventory=inv, position_age_hours=0.5,
        )

        age_factor = 1.0 - math.exp(-0.693 * 0.5 / 4.0)
        inv_urgency = min(1.0, (50.0 / 100.0) ** 0.5)
        # inv_urgency (0.707) > age_factor (0.083), so inv_urgency wins
        assert inv_urgency > age_factor
        effective_gamma = 0.015 + (0.05 - 0.015) * max(age_factor, inv_urgency)
        expected = 0.50 - effective_gamma * inv
        # Result may be clipped to 0.005
        expected = max(0.005, min(0.995, expected))
        assert r == pytest.approx(expected, abs=1e-6)


# ===================================================================
# KP-09: Adaptive taker threshold
# ===================================================================


class TestAdaptiveTakerThreshold:
    """KP-09: Larger quantity → harder to cross (higher effective threshold)."""

    def test_adaptive_taker_threshold(self):
        """Larger quantity makes crossing harder due to slippage_estimate."""
        cfg = _default_config(take_threshold_cents=0.8)
        eng = _engine(config=cfg)

        # Same edge, but different quantities
        # raw_edge = (0.60 - 0.50) * Q = 0.10 * Q
        # effective_threshold = 0.008 + fee + slippage + 0.001*Q
        # should_cross = raw_edge > effective_threshold * Q
        # = 0.10*Q > (0.008 + 0.001*Q) * Q

        # Small quantity (Q=1): raw_edge=0.10, eff_thresh=0.009, eff_thresh*Q=0.009 → cross
        cross_small, _ = eng.check_crossing_rule(
            fair_value=0.60, execution_price=0.50,
            side="BUY", haircut=0.01, quantity=1.0,
        )

        # Large quantity (Q=100): raw_edge=10.0, eff_thresh=0.108, eff_thresh*Q=10.8 → no cross
        cross_large, _ = eng.check_crossing_rule(
            fair_value=0.60, execution_price=0.50,
            side="BUY", haircut=0.01, quantity=100.0,
        )

        assert cross_small is True, "Small quantity with large edge should cross"
        assert cross_large is False, "Large quantity should be harder to cross"

    def test_adaptive_threshold_includes_costs(self):
        """Effective threshold adds fee, slippage, and quantity-proportional slippage."""
        cfg = _default_config(take_threshold_cents=1.0)
        eng = _engine(config=cfg)

        # With fee=0.01, slippage=0.005, quantity=10:
        # base_threshold = 0.01
        # slippage_estimate = 0.001 * 10 = 0.01
        # effective_threshold = 0.01 + 0.01 + 0.005 + 0.01 = 0.035
        # raw_edge = (0.55 - 0.50) * 10 = 0.50
        # should_cross = 0.50 > 0.035 * 10 = 0.35 → True
        cross, _ = eng.check_crossing_rule(
            fair_value=0.55, execution_price=0.50,
            side="BUY", haircut=0.01,
            fee=0.01, slippage=0.005, quantity=10.0,
        )
        assert cross is True

        # Smaller edge won't cross: fair_value=0.51
        # raw_edge = (0.51 - 0.50) * 10 = 0.10
        # 0.10 > 0.35? No → rejected
        cross2, _ = eng.check_crossing_rule(
            fair_value=0.51, execution_price=0.50,
            side="BUY", haircut=0.01,
            fee=0.01, slippage=0.005, quantity=10.0,
        )
        assert cross2 is False

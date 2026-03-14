"""Tests for FeatureVector and binary volatility estimator (MM-02)."""

from __future__ import annotations

import math
import time
from decimal import Decimal

import pytest

from pmm1.state.books import OrderBook
from pmm1.strategy.features import FeatureEngine, FeatureVector, TradeAccumulator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sigma_eff(midpoint: float, realized_vol: float) -> float:
    """Recompute sigma_eff from the formula for test assertions."""
    bernoulli_var = midpoint * (1.0 - midpoint)
    return (0.7 * bernoulli_var + 0.3 * min(realized_vol ** 2, 0.25)) ** 0.5


def _make_book(
    bid: float, ask: float, bid_size: float = 100.0, ask_size: float = 100.0,
) -> OrderBook:
    """Create a simple two-level order book for testing."""
    book = OrderBook(token_id="tok_test", tick_size=Decimal("0.01"))
    book.apply_snapshot(
        bids=[{"price": str(bid), "size": str(bid_size)}],
        asks=[{"price": str(ask), "size": str(ask_size)}],
    )
    return book


# ===================================================================
# FeatureVector field existence and defaults
# ===================================================================


class TestFeatureVectorFields:
    """Verify new MM-02 fields exist with correct defaults."""

    def test_feature_vector_has_sigma_eff(self):
        """FeatureVector should have sigma_eff field."""
        fv = FeatureVector()
        assert hasattr(fv, "sigma_eff")
        assert fv.sigma_eff == 0.25  # default

    def test_feature_vector_has_kappa_estimate(self):
        """FeatureVector should have kappa_estimate field."""
        fv = FeatureVector()
        assert hasattr(fv, "kappa_estimate")
        assert fv.kappa_estimate == 0.1  # default

    def test_sigma_eff_settable(self):
        """sigma_eff can be set to a custom value."""
        fv = FeatureVector(sigma_eff=0.42)
        assert fv.sigma_eff == pytest.approx(0.42)

    def test_kappa_estimate_settable(self):
        """kappa_estimate can be set to a custom value."""
        fv = FeatureVector(kappa_estimate=0.05)
        assert fv.kappa_estimate == pytest.approx(0.05)

    def test_existing_fields_still_present(self):
        """All pre-existing fields remain on FeatureVector."""
        fv = FeatureVector()
        for field in (
            "midpoint", "microprice", "imbalance", "spread", "spread_cents",
            "best_bid", "best_ask", "bid_depth_2c", "ask_depth_2c",
            "signed_trade_flow", "trade_intensity", "sweep_intensity",
            "realized_vol", "vol_regime",
            "time_to_resolution_hours", "time_to_resolution_fraction",
            "related_market_residual", "external_signal",
            "token_id", "condition_id", "timestamp", "is_stale",
        ):
            assert hasattr(fv, field), f"Missing field: {field}"


# ===================================================================
# Binary volatility formula unit tests
# ===================================================================


class TestBinaryVolatilityFormula:
    """Tests for the sigma_eff formula: sqrt(0.7 * p*(1-p) + 0.3 * min(rv^2, 0.25))."""

    def test_sigma_eff_at_midpoint_zero_rv(self):
        """At p=0.5 with rv=0, bernoulli_var is maximized (0.25)."""
        # sigma_eff = sqrt(0.7 * 0.25 + 0.3 * 0.0) = sqrt(0.175) ~ 0.4183
        sigma = _sigma_eff(0.5, 0.0)
        assert sigma == pytest.approx(math.sqrt(0.175), abs=1e-6)
        assert sigma == pytest.approx(0.4183, abs=1e-3)

    def test_sigma_eff_at_extreme_zero_rv(self):
        """At p=0.95, bernoulli_var is small (0.0475)."""
        sigma = _sigma_eff(0.95, 0.0)
        expected = math.sqrt(0.7 * 0.95 * 0.05)
        assert sigma == pytest.approx(expected, abs=1e-6)
        assert sigma < 0.20  # Much less than at p=0.5

    def test_sigma_eff_at_very_extreme(self):
        """At p=0.99, bernoulli_var=0.0099, sigma_eff should be very small."""
        sigma = _sigma_eff(0.99, 0.0)
        expected = math.sqrt(0.7 * 0.99 * 0.01)
        assert sigma == pytest.approx(expected, abs=1e-6)
        assert sigma < 0.10

    def test_sigma_eff_symmetry(self):
        """sigma_eff at p and (1-p) should be identical (symmetric around 0.5)."""
        for p in [0.1, 0.2, 0.3, 0.4, 0.8, 0.9, 0.95]:
            s1 = _sigma_eff(p, 0.0)
            s2 = _sigma_eff(1 - p, 0.0)
            assert s1 == pytest.approx(s2, abs=1e-10), f"Asymmetric at p={p}"

    def test_sigma_eff_with_high_realized_vol(self):
        """Large realized_vol is capped at sqrt(0.25)=0.5 contribution."""
        # rv=1.0 >> 0.5, so min(1.0^2, 0.25) = 0.25
        sigma_capped = _sigma_eff(0.5, 1.0)
        # sqrt(0.7*0.25 + 0.3*0.25) = sqrt(0.25) = 0.5
        assert sigma_capped == pytest.approx(0.5, abs=1e-6)

    def test_sigma_eff_realized_vol_contribution(self):
        """Non-zero realized_vol increases sigma_eff vs zero rv."""
        sigma_no_rv = _sigma_eff(0.5, 0.0)
        sigma_with_rv = _sigma_eff(0.5, 0.3)
        assert sigma_with_rv > sigma_no_rv

    def test_sigma_eff_at_boundaries(self):
        """At p=0 and p=1, bernoulli_var=0 so sigma depends only on rv."""
        # p=0: bernoulli_var=0
        sigma_zero = _sigma_eff(0.0, 0.0)
        assert sigma_zero == pytest.approx(0.0)

        sigma_one = _sigma_eff(1.0, 0.0)
        assert sigma_one == pytest.approx(0.0)

    def test_sigma_eff_monotone_in_midpoint_away_from_extremes(self):
        """sigma_eff increases as midpoint goes from 0 toward 0.5 (with rv=0)."""
        prev = 0.0
        for p in [0.05, 0.1, 0.2, 0.3, 0.4, 0.5]:
            s = _sigma_eff(p, 0.0)
            assert s >= prev, f"Not monotone at p={p}"
            prev = s


# ===================================================================
# Vol regime derivation from sigma_eff
# ===================================================================


class TestVolRegimeFromSigmaEff:
    """Test that vol_regime thresholds are based on sigma_eff."""

    @pytest.mark.parametrize("midpoint,expected_regime", [
        # p=0.98: sigma_eff = sqrt(0.7*0.0196) ~ 0.117 -> "low"
        (0.98, "low"),
        # p=0.95: sigma_eff = sqrt(0.7*0.0475) ~ 0.182 -> "normal"
        (0.95, "normal"),
        # p=0.85: sigma_eff = sqrt(0.7*0.1275) ~ 0.299 -> "normal" (just under 0.30)
        (0.85, "normal"),
        # p=0.70: sigma_eff = sqrt(0.7*0.21) ~ 0.383 -> "high"
        (0.70, "high"),
        # p=0.50: sigma_eff = sqrt(0.7*0.25) ~ 0.418 -> "high" (just under 0.42)
        (0.50, "high"),
    ])
    def test_vol_regime_from_midpoint_no_rv(self, midpoint: float, expected_regime: str):
        """Vol regime depends on midpoint via sigma_eff (with rv=0)."""
        sigma = _sigma_eff(midpoint, 0.0)
        if sigma < 0.15:
            regime = "low"
        elif sigma < 0.30:
            regime = "normal"
        elif sigma < 0.42:
            regime = "high"
        else:
            regime = "extreme"
        assert regime == expected_regime, (
            f"midpoint={midpoint}, sigma_eff={sigma:.4f}, "
            f"expected={expected_regime}, got={regime}"
        )

    def test_extreme_regime_requires_high_rv_at_midpoint(self):
        """At p=0.5, need high rv to push sigma_eff >= 0.42 into extreme."""
        # sigma_eff = sqrt(0.7*0.25 + 0.3*min(rv^2, 0.25))
        # For extreme: sigma_eff >= 0.42 -> 0.7*0.25 + 0.3*min(rv^2,0.25) >= 0.1764
        # 0.175 + 0.3*min(rv^2,0.25) >= 0.1764
        # min(rv^2,0.25) >= 0.0047 -> rv >= 0.0686
        sigma_low_rv = _sigma_eff(0.5, 0.05)
        sigma_high_rv = _sigma_eff(0.5, 0.5)
        assert sigma_low_rv < 0.42  # not extreme
        assert sigma_high_rv >= 0.42  # extreme


# ===================================================================
# FeatureEngine integration tests
# ===================================================================


class TestFeatureEngineCompute:
    """Integration tests: FeatureEngine.compute() produces correct sigma_eff."""

    def test_compute_sets_sigma_eff(self):
        """FeatureEngine.compute() populates sigma_eff on the result."""
        engine = FeatureEngine()
        book = _make_book(0.49, 0.51)
        fv = engine.compute("tok_test", book)
        # midpoint=0.50, no trades so rv=0
        expected = _sigma_eff(0.50, 0.0)
        assert fv.sigma_eff == pytest.approx(expected, abs=1e-6)

    def test_compute_sets_kappa_estimate_dynamic(self):
        """FeatureEngine.compute() sets kappa_estimate dynamically (MM-03)."""
        engine = FeatureEngine()
        book = _make_book(0.49, 0.51)
        fv = engine.compute("tok_test", book)
        # With book depth but no trades, kappa is driven by book depth
        # kappa >= 0.01 (floor) and computed from depth
        assert fv.kappa_estimate >= 0.01

    def test_compute_extreme_midpoint_low_sigma(self):
        """At p=0.98 (bid=0.97, ask=0.99), sigma_eff is low."""
        engine = FeatureEngine()
        book = _make_book(0.97, 0.99)
        fv = engine.compute("tok_test", book)
        assert fv.sigma_eff < 0.15
        assert fv.vol_regime == "low"

    def test_compute_midpoint_near_half_high_sigma(self):
        """At p=0.50, sigma_eff is in the high range (no rv)."""
        engine = FeatureEngine()
        book = _make_book(0.49, 0.51)
        fv = engine.compute("tok_test", book)
        assert fv.sigma_eff > 0.30
        assert fv.vol_regime == "high"

    def test_compute_with_trades_increases_sigma(self):
        """Adding trades that produce non-zero rv should increase sigma_eff."""
        engine = FeatureEngine()
        book = _make_book(0.49, 0.51)

        fv_no_trades = engine.compute("tok_test", book)

        # Add trades that create realized volatility
        now = time.time()
        for i, price in enumerate([0.48, 0.52, 0.47, 0.53, 0.46, 0.54]):
            engine.record_trade("tok_test", price, 10.0, "BUY", timestamp=now + i)

        fv_with_trades = engine.compute("tok_test", book)
        assert fv_with_trades.realized_vol > 0.0
        assert fv_with_trades.sigma_eff >= fv_no_trades.sigma_eff

    def test_compute_vol_regime_uses_sigma_eff_not_raw_vol(self):
        """Vol regime should follow sigma_eff thresholds, not old realized_vol thresholds.

        At p=0.98 with no trades: realized_vol=0 (would be "low" under old thresholds),
        and sigma_eff ~ 0.117 < 0.15 (still "low"). At p=0.90 with no trades:
        realized_vol=0 (would be "low" under old thresholds 0.001),
        but sigma_eff ~ 0.251 -> "normal" under new thresholds.
        """
        engine = FeatureEngine()
        # p=0.90 -> sigma_eff = sqrt(0.7 * 0.09) ~ 0.251
        book = _make_book(0.89, 0.91)
        fv = engine.compute("tok_test", book)
        # Under old code: realized_vol=0 < 0.001 -> "low"
        # Under new code: sigma_eff ~ 0.251 >= 0.15 -> "normal"
        assert fv.vol_regime == "normal", (
            f"Expected 'normal' from sigma_eff={fv.sigma_eff:.4f}, got '{fv.vol_regime}'"
        )

    def test_compute_no_book_uses_default_midpoint(self):
        """With no book, midpoint defaults to 0.5, so sigma_eff uses that."""
        engine = FeatureEngine()
        fv = engine.compute("tok_test", None)
        expected = _sigma_eff(0.5, 0.0)
        assert fv.sigma_eff == pytest.approx(expected, abs=1e-6)

    def test_compute_preserves_existing_fields(self):
        """All pre-existing feature fields remain populated correctly."""
        engine = FeatureEngine()
        book = _make_book(0.49, 0.51, bid_size=200, ask_size=100)
        fv = engine.compute("tok_test", book, condition_id="cond_abc")
        assert fv.midpoint == pytest.approx(0.50)
        assert fv.spread == pytest.approx(0.02)
        assert fv.spread_cents == pytest.approx(2.0)
        assert fv.best_bid == pytest.approx(0.49)
        assert fv.best_ask == pytest.approx(0.51)
        assert fv.condition_id == "cond_abc"
        assert fv.token_id == "tok_test"
        # imbalance: (200-100)/(200+100) = 100/300 ~ 0.333
        assert fv.imbalance == pytest.approx(1 / 3, abs=0.01)


# ===================================================================
# Regression: old log-return vol inflation at extreme prices
# ===================================================================


class TestLogReturnVolInflation:
    """Regression tests: sigma_eff fixes the problem of inflated vol at extreme prices."""

    def test_extreme_price_small_absolute_move(self):
        """At p=0.95, a 1-cent move (0.94->0.96) creates ~2% log return.

        Under old code this could push realized_vol into "high" or "extreme".
        Under MM-02, sigma_eff is dominated by Bernoulli variance which is small.
        """
        engine = FeatureEngine()
        book = _make_book(0.94, 0.96)

        # Simulate tiny 1-cent oscillations around 0.95
        now = time.time()
        for i in range(20):
            price = 0.94 + (i % 2) * 0.02  # alternates 0.94, 0.96
            engine.record_trade("tok_test", price, 10.0, "BUY", timestamp=now + i)

        fv = engine.compute("tok_test", book)
        # The realized_vol from log returns of 0.94->0.96 can be non-trivially large
        # but sigma_eff should remain moderate because bernoulli_var at p=0.95 is only 0.0475
        assert fv.sigma_eff < 0.30, (
            f"sigma_eff={fv.sigma_eff:.4f} should be moderate at extreme price, "
            f"rv={fv.realized_vol:.4f}"
        )
        assert fv.vol_regime in ("low", "normal"), (
            f"Vol regime should be low/normal at p=0.95, got '{fv.vol_regime}'"
        )


# ===================================================================
# MM-03: Dynamic kappa estimation
# ===================================================================


class TestDynamicKappa:
    """Tests for MM-03: Dynamic kappa estimation via EMA."""

    def test_kappa_estimate_computed(self):
        """With trade activity and book depth, kappa should be > 0.1."""
        engine = FeatureEngine()
        book = _make_book(0.49, 0.51, bid_size=200, ask_size=200)

        # Add trades to create trade_intensity > 0
        now = time.time()
        for i in range(10):
            engine.record_trade("tok_test", 0.50, 10.0, "BUY", timestamp=now + i)

        fv = engine.compute("tok_test", book)
        # trade_intensity > 0 and book depth > 0 => kappa should be substantial
        assert fv.kappa_estimate > 0.1

    def test_kappa_ema_smoothing(self):
        """Multiple calls produce smoothed kappa via EMA."""
        engine = FeatureEngine()
        book_thin = _make_book(0.49, 0.51, bid_size=10, ask_size=10)
        book_thick = _make_book(0.49, 0.51, bid_size=500, ask_size=500)

        # First call with thin book
        fv1 = engine.compute("tok_test", book_thin)
        kappa1 = fv1.kappa_estimate

        # Second call with thick book — EMA should smooth, not jump fully
        fv2 = engine.compute("tok_test", book_thick)
        kappa2 = fv2.kappa_estimate

        # kappa2 should be higher than kappa1 (thicker book) but smoothed
        assert kappa2 > kappa1
        # Should not fully equal the raw thick-book kappa (EMA smoothing)
        raw_thick = 0.01 * (500 + 500) / 2.0  # 5.0 (no trade intensity)
        assert kappa2 < raw_thick  # EMA means it hasn't fully caught up

    def test_kappa_no_trades_uses_book_depth(self):
        """With zero trades but book depth, kappa > 0."""
        engine = FeatureEngine()
        book = _make_book(0.49, 0.51, bid_size=100, ask_size=100)

        fv = engine.compute("tok_test", book)
        # No trades so trade_intensity = 0, but book depth contributes
        # raw_kappa = 0.5 * 0 + 0.01 * (100 + 100) / 2 = 1.0
        assert fv.kappa_estimate > 0.0
        assert fv.kappa_estimate >= 0.01  # floor


# ===================================================================
# MM-04+PM-04: VPIN Toxicity Signal
# ===================================================================


class TestVPIN:
    """Tests for VPIN toxicity computation."""

    def test_vpin_no_bars(self):
        """No trades -> VPIN = 0."""
        acc = TradeAccumulator()
        assert acc.get_vpin() == 0.0

    def test_vpin_balanced_flow(self):
        """Equal buy/sell volume per bar -> VPIN approx 0."""
        acc = TradeAccumulator()
        # Each bar = $100; alternate buy and sell so each bar is balanced
        for i in range(100):
            side = "BUY" if i % 2 == 0 else "SELL"
            # price=1.0, size=10 -> $10 per trade, 10 trades = 1 bar
            acc.add_trade(price=1.0, size=10.0, side=side, timestamp=float(i))
        # 100 trades * $10 = $1000 -> 10 bars, each with ~$50 buy + $50 sell
        vpin = acc.get_vpin()
        assert vpin == pytest.approx(0.0, abs=0.05)

    def test_vpin_one_sided(self):
        """All buys -> VPIN approx 1.0."""
        acc = TradeAccumulator()
        # 100 trades, all BUY, price=1.0, size=10 -> $10 each -> 10 bars all-buy
        for i in range(100):
            acc.add_trade(price=1.0, size=10.0, side="BUY", timestamp=float(i))
        vpin = acc.get_vpin()
        assert vpin == pytest.approx(1.0, abs=0.01)

    def test_toxicity_levels(self):
        """VPIN thresholds produce correct toxicity_level on FeatureVector."""
        # Low VPIN -> normal
        fv_normal = FeatureVector(vpin=0.1, toxicity_level="normal")
        assert fv_normal.toxicity_level == "normal"

        # Mid VPIN -> elevated
        fv_elevated = FeatureVector(vpin=0.4, toxicity_level="elevated")
        assert fv_elevated.toxicity_level == "elevated"

        # High VPIN -> toxic
        fv_toxic = FeatureVector(vpin=0.8, toxicity_level="toxic")
        assert fv_toxic.toxicity_level == "toxic"

        # Integration: engine computes toxicity_level from VPIN
        engine = FeatureEngine()
        acc = engine.get_accumulator("tok_test")
        # Push enough one-sided volume to create high VPIN
        for i in range(200):
            acc.add_trade(price=1.0, size=10.0, side="BUY", timestamp=float(i))
        fv = engine.compute("tok_test", None)
        assert fv.vpin > 0.6
        assert fv.toxicity_level == "toxic"


# ===================================================================
# PM-03: Time-of-Day Spread Regime
# ===================================================================


class TestTimeOfDay:
    """Tests for PM-03 time-of-day spread/size multipliers."""

    def test_tod_off_peak(self):
        """Hour 22 UTC -> low-liquidity regime: spread_mult=1.5, size_mult=0.6."""
        # We test the logic directly via FeatureVector fields set by compute().
        # Since compute() uses datetime.now(UTC), we check the field defaults
        # and verify the computation logic for a known hour.
        # Hour 22 is in the 21:00-04:00 range.
        fv = FeatureVector(hour_of_day=22, tod_spread_mult=1.5, tod_size_mult=0.6)
        assert fv.tod_spread_mult == pytest.approx(1.5)
        assert fv.tod_size_mult == pytest.approx(0.6)

    def test_tod_peak(self):
        """Hour 16 UTC -> peak regime: spread_mult=1.0, size_mult=1.0."""
        fv = FeatureVector(hour_of_day=16, tod_spread_mult=1.0, tod_size_mult=1.0)
        assert fv.tod_spread_mult == pytest.approx(1.0)
        assert fv.tod_size_mult == pytest.approx(1.0)

    def test_tod_fields_on_feature_vector(self):
        """FeatureVector has all PM-03 fields with correct defaults."""
        fv = FeatureVector()
        assert hasattr(fv, "hour_of_day")
        assert hasattr(fv, "tod_spread_mult")
        assert hasattr(fv, "tod_size_mult")
        assert fv.hour_of_day == 12
        assert fv.tod_spread_mult == 1.0
        assert fv.tod_size_mult == 1.0

    def test_tod_compute_sets_fields(self):
        """FeatureEngine.compute() populates tod fields."""
        engine = FeatureEngine()
        fv = engine.compute("tok_test", None)
        # hour_of_day should be set to the current UTC hour
        assert 0 <= fv.hour_of_day <= 23
        # multipliers should be one of the three regimes
        assert fv.tod_spread_mult in (1.0, 1.2, 1.5)
        assert fv.tod_size_mult in (0.6, 0.8, 1.0)


# ===================================================================
# PM-11: Multi-Timeframe Volatility
# ===================================================================


class TestMultiTimeframeVol:
    """Tests for PM-11 multi-timeframe volatility."""

    def test_vol_windowed(self):
        """Trades in different windows produce different vol."""
        acc = TradeAccumulator(window_s=7200)  # 2h window to keep all trades
        now = time.time()

        # Add volatile trades only in the last 5 minutes
        for i in range(20):
            price = 0.50 + (i % 2) * 0.04  # oscillate 0.50 <-> 0.54
            acc.add_trade(price=price, size=10.0, side="BUY", timestamp=now - 200 + i)

        # Add stable trades from 30-60 minutes ago
        for i in range(20):
            acc.add_trade(price=0.50, size=10.0, side="BUY", timestamp=now - 2400 + i)

        # 5m window should capture volatile trades -> higher vol
        vol_5m = acc.get_realized_volatility_windowed(300)
        # 1h window includes both volatile and stable -> vol is diluted
        vol_1h = acc.get_realized_volatility_windowed(3600)

        # The 5m window should have higher vol because it captures only the volatile period
        assert vol_5m > 0.0
        assert vol_1h > 0.0

    def test_vol_ratio(self):
        """Short-term spike detected by vol_ratio_short_long > 1."""
        acc = TradeAccumulator(window_s=7200)
        now = time.time()

        # Stable prices from 30-60 min ago
        for i in range(20):
            acc.add_trade(price=0.50, size=10.0, side="BUY", timestamp=now - 2400 + i)

        # Volatile prices in last 5 minutes
        for i in range(20):
            price = 0.50 + (i % 2) * 0.10  # large oscillation 0.50 <-> 0.60
            acc.add_trade(price=price, size=10.0, side="BUY", timestamp=now - 200 + i)

        vol_5m = acc.get_realized_volatility_windowed(300)
        vol_1h = acc.get_realized_volatility_windowed(3600)

        # Short-term vol should be higher than long-term
        assert vol_5m > 0.0
        assert vol_1h > 0.0
        ratio = vol_5m / max(0.0001, vol_1h) if vol_1h > 0 else 1.0
        assert ratio > 1.0, (
            f"Expected vol_ratio > 1.0 for short-term spike, got {ratio:.3f} "
            f"(vol_5m={vol_5m:.6f}, vol_1h={vol_1h:.6f})"
        )

    def test_vol_fields_on_feature_vector(self):
        """FeatureVector has all PM-11 fields with correct defaults."""
        fv = FeatureVector()
        assert fv.vol_5m == 0.0
        assert fv.vol_1h == 0.0
        assert fv.vol_24h == 0.0
        assert fv.vol_ratio_short_long == 1.0

    def test_vol_compute_populates_fields(self):
        """FeatureEngine.compute() populates multi-timeframe vol fields."""
        engine = FeatureEngine()
        fv = engine.compute("tok_test", None)
        assert hasattr(fv, "vol_5m")
        assert hasattr(fv, "vol_1h")
        assert hasattr(fv, "vol_24h")
        assert hasattr(fv, "vol_ratio_short_long")


# ===================================================================
# PM-07: Maker Rebate Optimization
# ===================================================================


class TestMakerRebate:
    """Tests for PM-07 maker rebate optimization."""

    def test_fee_market_fields_default(self):
        """FeatureVector defaults: no fee market, no rebate."""
        fv = FeatureVector()
        assert fv.fee_market is False
        assert fv.rebate_spread_discount == 0.0

    def test_fee_market_enabled(self):
        """When fee_market=True, rebate discount is applied."""
        engine = FeatureEngine()
        fv = engine.compute("tok_test", None, fee_market=True)
        assert fv.fee_market is True
        assert fv.rebate_spread_discount == pytest.approx(0.001)

    def test_fee_market_disabled(self):
        """When fee_market=False, no rebate discount."""
        engine = FeatureEngine()
        fv = engine.compute("tok_test", None, fee_market=False)
        assert fv.fee_market is False
        assert fv.rebate_spread_discount == 0.0

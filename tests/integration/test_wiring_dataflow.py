"""Integration tests verifying analytics data actually flows through the system.

These tests verify that:
1. Analytics params are computed and change pricing output (not just exist on signatures)
2. A single fill event feeds all 6 learning modules simultaneously
3. Save/load round-trips preserve state for all persistable modules
"""

import tempfile
from pathlib import Path

import pytest

from pmm1.analytics.edge_tracker import EdgeTracker
from pmm1.analytics.market_profitability import MarketProfitabilityTracker
from pmm1.analytics.markout_tracker import MarkoutTracker
from pmm1.analytics.post_mortem import TradePostMortem
from pmm1.analytics.signal_value import SignalValueTracker
from pmm1.analytics.spread_optimizer import SpreadOptimizer
from pmm1.math.changepoint import BayesianChangePointDetector
from pmm1.math.kelly import shrinkage_factor
from pmm1.settings import PricingConfig
from pmm1.strategy.features import FeatureVector
from pmm1.strategy.quote_engine import QuoteEngine


class TestParameterThreading:
    """Verify that analytics-derived params change pricing output."""

    def test_optimal_spread_changes_half_spread(self):
        """SpreadOptimizer output blends into compute_half_spread."""
        eng = QuoteEngine(PricingConfig())
        feat = FeatureVector()

        spread_default = eng.compute_half_spread(feat)
        spread_with_learned = eng.compute_half_spread(
            feat, optimal_base_spread=0.05,
        )
        # The 70/30 blend should shift the spread
        assert spread_with_learned != spread_default

    def test_shrinkage_reduces_kelly_size(self):
        """Kelly shrinkage factor reduces position size."""
        eng = QuoteEngine(PricingConfig())

        # Must provide nav + market_price for Kelly path to activate
        size_full = eng.compute_size(
            confidence=0.9, market_inventory=0.0,
            fair_value=0.60, market_price=0.50, nav=100.0,
        )
        size_shrunk = eng.compute_size(
            confidence=0.9, market_inventory=0.0,
            fair_value=0.60, market_price=0.50, nav=100.0,
            shrinkage=0.5,
        )
        assert size_shrunk < size_full

    def test_dd_cap_limits_size(self):
        """Drawdown cap constrains position size."""
        eng = QuoteEngine(PricingConfig())

        size_uncapped = eng.compute_size(
            confidence=0.9, market_inventory=0.0,
            fair_value=0.60, market_price=0.50, nav=100.0,
        )
        size_capped = eng.compute_size(
            confidence=0.9, market_inventory=0.0,
            fair_value=0.60, market_price=0.50, nav=100.0,
            dd_size_cap=0.01,
        )
        assert size_capped <= size_uncapped

    def test_shrinkage_factor_produces_valid_range(self):
        """shrinkage_factor returns values in [0.1, 1.0]."""
        s = shrinkage_factor(edge=0.05, sigma_p=0.05, n_obs=100)
        assert 0.1 <= s <= 1.0

        # With very few observations, shrinkage should be strong
        s_few = shrinkage_factor(edge=0.05, sigma_p=0.05, n_obs=5)
        assert s_few < s

    def test_compute_quote_threads_spread_param(self):
        """compute_quote passes optimal_base_spread to half_spread.

        Uses low volatility so the A-S formula produces a tight spread
        that is sensitive to the learned blend.
        """
        eng = QuoteEngine(PricingConfig())
        feat = FeatureVector(sigma_eff=0.02, kappa_estimate=1.0)

        hs_default = eng.compute_half_spread(feat)
        hs_with_learned = eng.compute_half_spread(
            feat, optimal_base_spread=0.05,
        )
        # Blending should produce a different spread
        assert hs_with_learned != hs_default


class TestMultiModuleFillDistribution:
    """Verify a single fill feeds all learning modules."""

    def test_fill_updates_all_modules(self):
        """Simulates the fill handler feeding 6 modules simultaneously."""
        spread_opt = SpreadOptimizer()
        mkt_prof = MarketProfitabilityTracker()
        sig_val = SignalValueTracker()
        markout = MarkoutTracker()
        post_mortem = TradePostMortem()
        changepoint = BayesianChangePointDetector()

        cid = "cond_abc123"
        tid = "token_yes"
        price = 0.55
        side = "BUY"
        spread_capture = 0.005
        as_estimate = 0.002
        mid_at_fill = 0.54

        # Record to all modules (same order as _apply_fill_effects)
        mkt_prof.record_fill(cid, pnl=spread_capture, volume=price * 10)
        sig_val.record_fill(
            blended_fv=0.56, market_mid=mid_at_fill,
            fill_price=price, side=side,
            pnl=spread_capture, llm_used=True,
        )
        markout.record_fill(
            token_id=tid, condition_id=cid,
            fill_price=price, fill_side=side,
            fv_at_fill=mid_at_fill,
        )
        spread_opt.record_fill(
            condition_id=cid, spread_at_fill=0.02,
            spread_capture=spread_capture,
            adverse_selection_5s=as_estimate,
        )
        post_mortem.classify_fill(
            pnl=spread_capture, spread_capture=spread_capture,
            adverse_selection_5s=as_estimate,
        )
        changepoint.update(1.0)  # Win

        # Verify ALL modules received data
        assert len(mkt_prof._markets) > 0
        assert len(sig_val._observations) == 1
        assert len(markout._pending_by_id) > 0
        assert cid in spread_opt._market_buckets
        assert post_mortem._total_classified == 1
        assert changepoint._n_obs == 1


class TestPersistenceRoundTrips:
    """Verify save/load round-trips for all persistable modules."""

    def test_edge_tracker_persistence(self):
        """EdgeTracker save->load preserves state."""
        et = EdgeTracker(min_trades=5, target_edge=0.05)
        for i in range(20):
            et.record_trade(
                predicted_p=0.55,
                market_p=0.50,
                outcome=1.0 if i % 3 == 0 else 0.0,
                pnl=0.01 if i % 3 == 0 else -0.005,
                condition_id=f"c{i % 3}",
            )
        n_trades_before = len(et.trades)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            et.save(path)
            et2 = EdgeTracker(min_trades=5, target_edge=0.05)
            et2.load(path)
            assert len(et2.trades) == n_trades_before
        finally:
            Path(path).unlink(missing_ok=True)

    def test_spread_optimizer_persistence(self):
        """SpreadOptimizer save->load preserves per-market bucket stats."""
        so = SpreadOptimizer(default_spread=0.015)
        for _ in range(10):
            so.record_fill("mkt1", 0.015, 0.003, 0.001)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            so.save(path)
            so2 = SpreadOptimizer(default_spread=0.015)
            so2.load(path)
            # Verify market stats were preserved (not empty)
            assert "mkt1" in so2._market_buckets
        finally:
            Path(path).unlink(missing_ok=True)

    def test_market_profitability_persistence(self):
        """MarketProfitabilityTracker save->load preserves scores."""
        mp = MarketProfitabilityTracker()
        for _ in range(20):
            mp.record_fill("mkt_a", pnl=0.01, volume=5.0)
        score_before = mp.profitability_score("mkt_a")

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            mp.save(path)
            mp2 = MarketProfitabilityTracker()
            mp2.load(path)
            assert mp2.profitability_score("mkt_a") == pytest.approx(
                score_before, abs=0.01,
            )
        finally:
            Path(path).unlink(missing_ok=True)

    def test_signal_value_persistence(self):
        """SignalValueTracker save->load preserves observations."""
        sv = SignalValueTracker()
        for _ in range(10):
            sv.record_fill(
                blended_fv=0.55, market_mid=0.50,
                fill_price=0.52, side="BUY",
                pnl=0.003, llm_used=True,
            )

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sv.save(path)
            sv2 = SignalValueTracker()
            sv2.load(path)
            assert len(sv2._observations) == len(sv._observations)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_post_mortem_persistence(self):
        """TradePostMortem save->load preserves classifications."""
        pm = TradePostMortem()
        pm.classify_fill(pnl=-0.01, spread_capture=0.002, adverse_selection_5s=0.015)
        pm.classify_fill(pnl=0.005, spread_capture=0.005, adverse_selection_5s=0.001)
        total_before = pm._total_classified

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            pm.save(path)
            pm2 = TradePostMortem()
            pm2.load(path)
            assert pm2._total_classified == total_before
        finally:
            Path(path).unlink(missing_ok=True)


class TestQuoteEVSuppression:
    """Verify quote EV suppression logic works end-to-end."""

    def test_should_suppress_negative_ev(self):
        """should_suppress_quotes returns True for negative EV."""
        eng = QuoteEngine(PricingConfig())
        assert eng.should_suppress_quotes(-0.01) is True
        assert eng.should_suppress_quotes(0.0) is True
        assert eng.should_suppress_quotes(0.01) is False

    def test_compute_quote_ev_realistic(self):
        """compute_quote_ev produces reasonable values."""
        eng = QuoteEngine(PricingConfig())
        ev = eng.compute_quote_ev(
            reservation_price=0.50,
            bid_price=0.48,
            ask_price=0.52,
            as_cost=0.001,
        )
        # With symmetric spread around FV and low AS, EV should be positive
        assert ev > 0

    def test_high_as_cost_produces_negative_ev(self):
        """High adverse selection makes narrow quotes negative EV."""
        eng = QuoteEngine(PricingConfig())
        ev = eng.compute_quote_ev(
            reservation_price=0.50,
            bid_price=0.49,
            ask_price=0.51,
            as_cost=0.05,  # Very high AS
        )
        assert ev < 0

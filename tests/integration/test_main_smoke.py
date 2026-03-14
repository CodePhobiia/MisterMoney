"""Smoke tests for main-loop initialisation.

Verify that core config objects, analytics modules, and the
QuoteEngine can be instantiated with defaults — no external
services required.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from pmm1.settings import PricingConfig, RiskConfig

# ── 1. Config defaults ─────────────────────────────────────


def test_pricing_config_defaults():
    cfg = PricingConfig()
    assert cfg.base_half_spread_cents == 1.0
    assert cfg.inventory_skew_gamma == 0.015
    assert cfg.kelly_enabled is True
    assert cfg.target_dollar_size == 8.0
    assert cfg.max_dollar_size == 15.0


def test_risk_config_defaults():
    cfg = RiskConfig()
    assert cfg.per_market_gross_nav == 0.02
    assert cfg.max_orders_per_market_side == 3
    assert cfg.absolute_max_drawdown_nav == 0.15


# ── 2. Analytics module imports + instantiation ─────────────


def test_spread_optimizer_instantiation():
    from pmm1.analytics.spread_optimizer import (
        SpreadOptimizer,
    )

    opt = SpreadOptimizer(default_spread=0.01)
    assert opt.default_spread == 0.01


def test_market_profitability_tracker_instantiation():
    from pmm1.analytics.market_profitability import (
        MarketProfitabilityTracker,
    )

    tracker = MarketProfitabilityTracker()
    assert tracker.decay == 0.95


def test_signal_value_tracker_instantiation():
    from pmm1.analytics.signal_value import (
        SignalValueTracker,
    )

    tracker = SignalValueTracker()
    assert tracker.window == 200


def test_trade_post_mortem_instantiation():
    from pmm1.analytics.post_mortem import TradePostMortem

    pm = TradePostMortem()
    assert pm._total_classified == 0


def test_markout_tracker_instantiation():
    from pmm1.analytics.markout_tracker import MarkoutTracker

    tracker = MarkoutTracker()
    assert isinstance(tracker._pending, list)


def test_inventory_carry_tracker_instantiation():
    from pmm1.analytics.carry_tracker import (
        InventoryCarryTracker,
    )

    tracker = InventoryCarryTracker()
    assert tracker._total_carry == 0.0


def test_var_reporter_instantiation():
    from pmm1.analytics.var_calculator import VaRReporter

    reporter = VaRReporter()
    result = reporter.compute_report([])
    assert result["total_var_95"] == 0.0


def test_bayesian_changepoint_detector_instantiation():
    from pmm1.math.changepoint import (
        BayesianChangePointDetector,
    )

    detector = BayesianChangePointDetector()
    assert detector.hazard == 1 / 200
    assert detector._n_obs == 0


# ── 3. QuoteEngine ──────────────────────────────────────────


def test_quote_engine_instantiation():
    from pmm1.strategy.quote_engine import QuoteEngine

    cfg = PricingConfig()
    engine = QuoteEngine(
        cfg,
        target_dollar_size=cfg.target_dollar_size,
        max_dollar_size=cfg.max_dollar_size,
    )
    assert engine.config is cfg
    assert engine.target_dollar_size == 8.0
    assert engine.max_dollar_size == 15.0


# ── 4. os.makedirs("data", exist_ok=True) ──────────────────


def test_makedirs_creates_data_directory():
    tmp = tempfile.mkdtemp()
    target = os.path.join(tmp, "data")
    assert not os.path.exists(target)
    os.makedirs(target, exist_ok=True)
    assert os.path.isdir(target)
    # Idempotent — no error on second call
    os.makedirs(target, exist_ok=True)
    assert os.path.isdir(target)
    shutil.rmtree(tmp)

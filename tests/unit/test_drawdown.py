"""Tests for drawdown governor — HWM, tier transitions, daily reset."""

from unittest.mock import patch

import pytest

from pmm1.risk.drawdown import DrawdownGovernor, DrawdownTier
from pmm1.settings import RiskConfig


def _make_governor(
    pause=0.015, wider=0.025, flatten=0.04
) -> DrawdownGovernor:
    config = RiskConfig(
        daily_pause_drawdown_nav=pause,
        daily_wider_drawdown_nav=wider,
        daily_flatten_drawdown_nav=flatten,
    )
    gov = DrawdownGovernor(config)
    gov.initialize(100.0)
    return gov


class TestDrawdownHWM:
    """T0-02: Drawdown uses high-water mark, not day-start."""

    def test_hwm_updates_on_new_high(self):
        gov = _make_governor()
        gov.update(110.0)
        assert gov.state.daily_high_watermark == 110.0

    def test_hwm_doesnt_decrease(self):
        gov = _make_governor()
        gov.update(110.0)
        gov.update(105.0)
        assert gov.state.daily_high_watermark == 110.0

    def test_drawdown_from_hwm_not_day_start(self):
        """NAV 100 → 110 → 101: drawdown should be ~8.2%, not 1%."""
        gov = _make_governor()
        gov.update(110.0)
        state = gov.update(101.0)
        # Drawdown = (110 - 101) / 110 ≈ 8.18%
        assert state.drawdown_pct == pytest.approx(9.0 / 110.0, rel=0.01)
        assert state.drawdown_pct > 0.08  # Much more than 1%


class TestDrawdownTiers:
    def test_normal_no_drawdown(self):
        gov = _make_governor()
        state = gov.update(100.0)
        assert state.tier == DrawdownTier.NORMAL

    def test_tier1_pause_taker(self):
        gov = _make_governor(pause=0.015)
        gov.update(100.0)
        # 1.5% drawdown from 100
        state = gov.update(98.5)
        assert state.tier == DrawdownTier.TIER1_PAUSE_TAKER

    def test_tier2_wider_smaller(self):
        gov = _make_governor(wider=0.025)
        gov.update(100.0)
        # 2.5% drawdown
        state = gov.update(97.5)
        assert state.tier == DrawdownTier.TIER2_WIDER_SMALLER

    def test_tier3_flatten_only(self):
        gov = _make_governor(flatten=0.04)
        gov.update(100.0)
        # 4% drawdown
        state = gov.update(96.0)
        assert state.tier == DrawdownTier.TIER3_FLATTEN_ONLY

    def test_recovery_clears_tier_after_dwell(self):
        gov = _make_governor(pause=0.015)
        t0 = 1000.0
        with patch("pmm1.risk.drawdown.time") as mock_time:
            mock_time.time.return_value = t0
            gov.update(100.0)
            gov.update(98.5)  # Tier 1
            assert gov.state.tier == DrawdownTier.TIER1_PAUSE_TAKER
            # Recovery before dwell: tier stays
            mock_time.time.return_value = t0 + 60
            gov.update(100.0)
            assert gov.state.tier == DrawdownTier.TIER1_PAUSE_TAKER
            # Recovery after 5-min dwell: tier clears
            mock_time.time.return_value = t0 + 301
            gov.update(100.0)
            assert gov.state.tier == DrawdownTier.NORMAL


class TestDrawdownStateProperties:
    def test_should_pause_taker(self):
        gov = _make_governor(pause=0.01)
        gov.update(100.0)
        gov.update(99.0)
        assert gov.state.should_pause_taker is True

    def test_size_multiplier_tier2(self):
        gov = _make_governor(wider=0.01)
        gov.update(100.0)
        gov.update(99.0)
        assert gov.state.size_multiplier == 0.5

    def test_size_multiplier_tier3(self):
        gov = _make_governor(flatten=0.01)
        gov.update(100.0)
        gov.update(99.0)
        assert gov.state.size_multiplier == 0.0

    def test_spread_multiplier_tier2(self):
        gov = _make_governor(wider=0.01)
        gov.update(100.0)
        gov.update(99.0)
        assert gov.state.spread_multiplier == 1.5


class TestDrawdownReset:
    def test_daily_reset(self):
        gov = _make_governor(pause=0.01)
        gov.update(100.0)
        gov.update(99.0)  # Trigger tier 1
        assert gov.state.tier != DrawdownTier.NORMAL
        gov.reset_daily(99.0)
        assert gov.state.tier == DrawdownTier.NORMAL
        assert gov.state.daily_high_watermark == 99.0


class TestDrawdownDwellTime:
    def test_tier_recovery_requires_dwell_time(self):
        """R-M1: Tier should not recover before 5-minute dwell."""
        gov = _make_governor(pause=0.015, wider=0.025, flatten=0.04)
        t0 = 1000.0
        with patch("pmm1.risk.drawdown.time") as mock_time:
            mock_time.time.return_value = t0
            gov.update(100.0)
            # Escalate to TIER2
            gov.update(97.5)
            assert gov.state.tier == DrawdownTier.TIER2_WIDER_SMALLER

            # Try to recover immediately — should stay at TIER2
            mock_time.time.return_value = t0 + 10
            gov.update(100.0)
            assert gov.state.tier == DrawdownTier.TIER2_WIDER_SMALLER

            # Still within 5 minutes — should stay at TIER2
            mock_time.time.return_value = t0 + 299
            gov.update(100.0)
            assert gov.state.tier == DrawdownTier.TIER2_WIDER_SMALLER

            # After 5 minutes — should recover
            mock_time.time.return_value = t0 + 301
            gov.update(100.0)
            assert gov.state.tier == DrawdownTier.NORMAL


class TestDrawdownCallbacks:
    def test_set_on_tier_change(self):
        gov = _make_governor()
        called = []

        async def cb(old, new, dd_pct):
            called.append((old, new, dd_pct))

        gov.set_on_tier_change(cb)
        assert gov._on_tier_change is cb

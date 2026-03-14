"""Tests for universe selection — scoring, eligibility, and rotation."""

from datetime import UTC, datetime, timedelta

import pytest

from pmm1.settings import UniverseWeights
from pmm1.strategy.universe import (
    MarketMetadata,
    compute_universe_score,
    should_rotate_market,
)


def _default_weights() -> UniverseWeights:
    return UniverseWeights()


def _make_market(**overrides) -> MarketMetadata:
    defaults = {
        "condition_id": "cond-1",
        "token_id_yes": "yes-1",
        "token_id_no": "no-1",
        "question": "Will X happen?",
        "active": True,
        "closed": False,
        "accepting_orders": True,
        "enable_order_book": True,
        "volume_24h": 1000.0,
        "liquidity": 500.0,
        "spread": 0.02,
        "best_bid": 0.49,
        "best_ask": 0.51,
    }
    defaults.update(overrides)
    return MarketMetadata(**defaults)


class TestNewMarketBonus:
    def test_new_market_bonus(self):
        """PM-05: Market < 24h old with rewards gets score bonus."""
        weights = _default_weights()

        # Market created 6 hours ago with rewards
        new_market = _make_market(
            created_at=datetime.now(UTC) - timedelta(hours=6),
            reward_eligible=True,
        )
        score_new = compute_universe_score(new_market, weights)

        # Same market but without created_at (no bonus)
        old_market = _make_market(
            created_at=None,
            reward_eligible=True,
        )
        score_old = compute_universe_score(old_market, weights)

        # New market should get +2.0 bonus
        assert score_new == pytest.approx(score_old + 2.0)

    def test_no_bonus_if_not_reward_eligible(self):
        """PM-05: No bonus if market is not reward-eligible."""
        weights = _default_weights()

        market = _make_market(
            created_at=datetime.now(UTC) - timedelta(hours=6),
            reward_eligible=False,
        )
        score_no_reward = compute_universe_score(market, weights)

        market_no_date = _make_market(
            created_at=None,
            reward_eligible=False,
        )
        score_baseline = compute_universe_score(market_no_date, weights)

        assert score_no_reward == pytest.approx(score_baseline)

    def test_no_bonus_if_older_than_24h(self):
        """PM-05: No bonus if market is older than 24 hours."""
        weights = _default_weights()

        market = _make_market(
            created_at=datetime.now(UTC) - timedelta(hours=30),
            reward_eligible=True,
        )
        score_old = compute_universe_score(market, weights)

        market_no_date = _make_market(
            created_at=None,
            reward_eligible=True,
        )
        score_baseline = compute_universe_score(market_no_date, weights)

        assert score_old == pytest.approx(score_baseline)


class TestShouldRotateMarket:
    def test_should_rotate_market(self):
        """PM-12: Market below 50% median for 3 checks -> rotate."""
        market = _make_market(universe_score=2.0)
        median_score = 10.0

        # 3 consecutive below + score < 50% median → rotate
        assert should_rotate_market(
            market,
            profitability_score=1.0,
            median_score=median_score,
            consecutive_below=3,
        ) is True

    def test_should_not_rotate_if_consecutive_below_threshold(self):
        """PM-12: Market below 50% median for only 2 checks -> keep."""
        market = _make_market(universe_score=2.0)
        median_score = 10.0

        assert should_rotate_market(
            market,
            profitability_score=1.0,
            median_score=median_score,
            consecutive_below=2,
        ) is False

    def test_should_not_rotate_if_score_above_threshold(self):
        """PM-12: Market above 50% median -> keep even with 3 consecutive."""
        market = _make_market(universe_score=6.0)
        median_score = 10.0

        assert should_rotate_market(
            market,
            profitability_score=5.0,
            median_score=median_score,
            consecutive_below=3,
        ) is False

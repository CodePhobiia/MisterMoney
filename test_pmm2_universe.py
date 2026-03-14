#!/usr/bin/env python3
"""Test script for pmm2 universe layer.

Tests all major components:
- EnrichedMarket model
- compute_ambiguity_score
- RewardSurface
- FeeSurface
- UniverseScorer
"""

from pmm2.universe import (
    EnrichedMarket,
    FeeSurface,
    RewardSurface,
    UniverseScorer,
    compute_ambiguity_score,
)


def test_enriched_market():
    """Test EnrichedMarket model."""
    market = EnrichedMarket(
        condition_id="test123",
        question="Will BTC hit $100k?",
        token_id_yes="token_yes",
        token_id_no="token_no",
        best_bid=0.45,
        best_ask=0.47,
        mid=0.46,
        spread_cents=2.0,
        volume_24h=50000.0,
        liquidity=10000.0,
        reward_eligible=True,
        reward_daily_rate=0.15,
        hours_to_resolution=48.0,
    )
    assert market.condition_id == "test123"
    assert market.reward_eligible is True
    print("✓ EnrichedMarket model works")


def test_ambiguity_scoring():
    """Test ambiguity score computation."""
    # Vague question
    score1 = compute_ambiguity_score("Will it be approximately 70 degrees around noon?")
    assert score1 > 0.3, f"Expected high ambiguity, got {score1}"

    # Specific question
    score2 = compute_ambiguity_score("Will BTC close above $100,000 on 2026-12-31?")
    assert score2 < 0.2, f"Expected low ambiguity, got {score2}"

    print(f"✓ Ambiguity scoring: vague={score1:.2f}, specific={score2:.2f}")


def test_reward_surface():
    """Test RewardSurface dual indexing."""
    from pmm1.api.rewards import RewardInfo

    surface = RewardSurface()

    # Manually populate for testing
    surface.by_condition = {
        "cond1": RewardInfo(
            condition_id="cond1",
            daily_rate=0.15,
            min_size=10.0,
            max_spread=0.05,
            token_ids=["token_yes_1", "token_no_1"],
        )
    }
    surface.by_token = {
        "token_yes_1": surface.by_condition["cond1"],
        "token_no_1": surface.by_condition["cond1"],
    }

    # Test condition_id lookup
    info = surface.is_eligible(condition_id="cond1")
    assert info is not None
    assert info.daily_rate == 0.15

    # Test token_id fallback
    info2 = surface.is_eligible(token_id_yes="token_yes_1")
    assert info2 is not None
    assert info2.condition_id == "cond1"

    # Test miss
    info3 = surface.is_eligible(condition_id="nonexistent")
    assert info3 is None

    print("✓ RewardSurface dual indexing works")


def test_fee_surface():
    """Test FeeSurface."""
    surface = FeeSurface()

    # Mock market data
    markets = [
        {
            "condition_id": "fee_market_1",
            "fees_enabled": True,
            "fee_rate": 0.01,
        },
        {
            "condition_id": "no_fee_market",
            "fees_enabled": False,
        },
    ]

    surface.update_from_markets(markets)

    assert surface.is_fee_enabled("fee_market_1") is True
    assert surface.get_fee_rate("fee_market_1") == 0.01
    assert surface.is_fee_enabled("no_fee_market") is False
    assert surface.get_fee_rate("no_fee_market") == 0.0

    print("✓ FeeSurface works")


def test_universe_scorer():
    """Test UniverseScorer."""
    scorer = UniverseScorer()

    # High-quality market
    good_market = EnrichedMarket(
        condition_id="good",
        question="Clear question",
        best_bid=0.48,
        best_ask=0.52,
        mid=0.50,
        spread_cents=4.0,
        volume_24h=100000.0,
        liquidity=20000.0,
        reward_eligible=True,
        reward_daily_rate=0.20,
        fees_enabled=True,
        hours_to_resolution=72.0,
        ambiguity_score=0.1,
    )

    # Low-quality market
    bad_market = EnrichedMarket(
        condition_id="bad",
        question="Vague question",
        best_bid=0.01,
        best_ask=0.03,
        mid=0.02,
        spread_cents=2.0,
        volume_24h=100.0,
        liquidity=50.0,
        reward_eligible=False,
        hours_to_resolution=2.0,
        ambiguity_score=0.8,
    )

    score_good = scorer.score_market(good_market)
    score_bad = scorer.score_market(bad_market)

    assert score_good > score_bad, f"Good market should score higher: {score_good} vs {score_bad}"

    # Test selection
    markets = [bad_market, good_market]
    selected = scorer.select_top(markets, max_markets=1)

    assert len(selected) == 1
    assert selected[0].condition_id == "good"

    print(f"✓ UniverseScorer works: good={score_good:.2f}, bad={score_bad:.2f}")


def main():
    """Run all tests."""
    print("Testing pmm2 universe layer...\n")

    test_enriched_market()
    test_ambiguity_scoring()
    test_reward_surface()
    test_fee_surface()
    test_universe_scorer()

    print("\n✅ All tests passed!")


if __name__ == "__main__":
    main()

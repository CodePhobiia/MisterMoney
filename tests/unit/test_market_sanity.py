from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pmm1.risk.correlation import ThematicCorrelation
from pmm1.settings import MarketFiltersConfig
from pmm1.state.books import OrderBook
from pmm1.strategy.features import FeatureVector
from pmm1.strategy.market_sanity import (
    MarketTelemetry,
    MarketTelemetryEvent,
    assess_live_market,
    audit_active_markets,
    inventory_context_for_token,
)
from pmm1.strategy.universe import MarketMetadata, ScoredMarket


def _scored_market(
    condition_id: str,
    *,
    score: float,
    question: str,
    event_id: str,
    spread: float = 0.02,
    liquidity: float = 5000.0,
) -> ScoredMarket:
    return ScoredMarket(
        metadata=MarketMetadata(
            condition_id=condition_id,
            token_id_yes=f"{condition_id}-yes",
            token_id_no=f"{condition_id}-no",
            event_id=event_id,
            question=question,
            end_date=datetime.now(timezone.utc) + timedelta(days=7),
            spread=spread,
            liquidity=liquidity,
        ),
        score=score,
        rank=0,
        eligible=True,
    )


def test_audit_active_markets_caps_event_and_theme_concentration() -> None:
    filters = MarketFiltersConfig(
        max_markets_per_event=1,
        max_markets_per_theme=1,
    )
    correlation = ThematicCorrelation()
    ranked = [
        _scored_market(
            "cond-election-1",
            score=12.0,
            question="Will Trump win the election?",
            event_id="event-a",
        ),
        _scored_market(
            "cond-election-2",
            score=11.0,
            question="Will Trump win the popular vote?",
            event_id="event-b",
        ),
        _scored_market(
            "cond-crypto",
            score=10.0,
            question="Will Bitcoin trade above $100k?",
            event_id="event-c",
        ),
        _scored_market(
            "cond-event-dup",
            score=9.0,
            question="Will turnout exceed 2020 levels?",
            event_id="event-a",
        ),
    ]

    result = audit_active_markets(
        ranked,
        filters,
        max_markets=4,
        classify_theme=correlation.classify,
    )

    assert [item.metadata.condition_id for item in result.selected] == [
        "cond-election-1",
        "cond-crypto",
    ]
    rejected = {entry.condition_id: entry.reasons for entry in result.entries if not entry.selected}
    assert rejected["cond-election-2"] == ["theme_diversification_cap"]
    assert rejected["cond-event-dup"] == ["event_diversification_cap"]


def test_assess_live_market_rejects_stale_reward_spread_and_thin_depth() -> None:
    market = MarketMetadata(
        condition_id="cond-1",
        token_id_yes="tok-yes",
        token_id_no="tok-no",
        question="Reward market",
        reward_eligible=True,
        reward_max_spread=2.0,
        reward_min_size=10.0,
    )
    book = OrderBook("tok-yes")
    book.apply_snapshot(
        [{"price": "0.48", "size": "5"}],
        [{"price": "0.52", "size": "6"}],
    )
    features = FeatureVector(
        token_id="tok-yes",
        condition_id="cond-1",
        spread=0.04,
        spread_cents=4.0,
        bid_depth_2c=5.0,
        ask_depth_2c=6.0,
        is_stale=True,
    )
    filters = MarketFiltersConfig(
        max_top_spread_cents=10.0,
        min_live_depth_within_2c_shares=25.0,
    )

    result = assess_live_market(market, book, features, filters)

    assert result.tradable is False
    assert "book_stale" in result.reasons
    assert "reward_spread_outside_scoring" in result.reasons
    assert "live_depth_too_thin" in result.reasons


def test_market_telemetry_rate_limits_duplicate_events() -> None:
    telemetry = MarketTelemetry(log_ttl_s=60.0)
    event = MarketTelemetryEvent(
        kind="quote_market_suppressed",
        stage="live_market_sanity",
        reason="book_stale",
        condition_id="cond-1",
        token_id="tok-1",
    )

    assert telemetry.record(event, now=100.0) is True
    assert telemetry.record(event, now=110.0) is False
    assert telemetry.consume_reason_counts() == {"book_stale": 2}


def test_inventory_context_for_no_token_inverts_cluster_inventory() -> None:
    market_inventory, cluster_inventory = inventory_context_for_token(
        is_no_token=True,
        market_inventory=4.0,
        cluster_inventory=9.0,
    )

    assert market_inventory == -4.0
    assert cluster_inventory == -9.0

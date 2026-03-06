"""Universe selection — market eligibility filter, scoring, top-K selection from §5."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import structlog
from pydantic import BaseModel, Field

from pmm1.settings import MarketFiltersConfig, UniverseWeights

logger = structlog.get_logger(__name__)


class MarketMetadata(BaseModel):
    """Aggregated metadata for a single market used in universe scoring."""

    condition_id: str
    token_id_yes: str = ""
    token_id_no: str = ""
    event_id: str = ""
    question: str = ""
    slug: str = ""
    active: bool = True
    closed: bool = False
    enable_order_book: bool = True
    neg_risk: bool = False
    neg_risk_market_id: str = ""
    end_date: datetime | None = None
    game_start_time: datetime | None = None
    volume_24h: float = 0.0
    liquidity: float = 0.0
    spread: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    depth_within_2c: float = 0.0
    is_sports: bool = False
    is_crypto_intraday: bool = False
    has_clear_rules: bool = True
    reward_eligible: bool = False
    reward_daily_rate: float = 0.0
    reward_min_size: float = 0.0
    reward_max_spread: float = 0.0
    # Computed during scoring
    toxicity_estimate: float = 0.0
    resolution_risk: float = 0.0
    reward_ev: float = 0.0
    arb_ev: float = 0.0


class EligibilityResult(BaseModel):
    """Result of eligibility check for a market."""

    condition_id: str
    eligible: bool
    reasons: list[str] = Field(default_factory=list)


class ScoredMarket(BaseModel):
    """A market with its universe score."""

    metadata: MarketMetadata
    score: float = 0.0
    rank: int = 0
    eligible: bool = True
    eligibility_reasons: list[str] = Field(default_factory=list)


def check_eligibility(
    market: MarketMetadata,
    filters: MarketFiltersConfig,
    now: datetime | None = None,
) -> EligibilityResult:
    """Check if a market passes the eligibility filter from §5.

    eligible_i = active_i ∧ orderbook_i ∧ clearRules_i ∧ liquid_i ∧ safeTime_i
    """
    if now is None:
        now = datetime.now(timezone.utc)

    reasons: list[str] = []

    # Must be active and not closed
    if not market.active:
        reasons.append("not_active")
    if market.closed:
        reasons.append("closed")

    # Must have order book enabled
    if not market.enable_order_book:
        reasons.append("orderbook_disabled")

    # Sports filter
    if market.is_sports and not filters.allow_sports:
        reasons.append("sports_excluded")

    # Crypto intraday filter
    if market.is_crypto_intraday and not filters.allow_crypto_intraday:
        reasons.append("crypto_intraday_excluded")

    # Clear rules requirement
    if filters.require_clear_rules and not market.has_clear_rules:
        reasons.append("unclear_rules")

    # Time to end check (unless pure arb)
    if market.end_date:
        end_date = market.end_date if market.end_date.tzinfo else market.end_date.replace(tzinfo=timezone.utc)
        now_aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        hours_remaining = (end_date - now_aware).total_seconds() / 3600
        if hours_remaining < filters.min_time_to_end_hours:
            reasons.append(f"time_to_end={hours_remaining:.1f}h < {filters.min_time_to_end_hours}h")

    # Volume check — use total volume as fallback if 24h volume is 0
    effective_volume = market.volume_24h
    if effective_volume <= 0 and market.liquidity > 0:
        # Gamma often doesn't populate volume_24h; use liquidity as a rough proxy
        effective_volume = market.liquidity
    if effective_volume < filters.min_volume_24h_usd:
        reasons.append(f"volume_24h={effective_volume:.0f} < {filters.min_volume_24h_usd:.0f}")

    # Spread check (in cents) — only apply if spread data is actually available
    if market.spread > 0:
        spread_cents = market.spread * 100 if market.spread < 1 else market.spread
        if spread_cents > filters.max_top_spread_cents:
            reasons.append(f"spread={spread_cents:.1f}¢ > {filters.max_top_spread_cents}¢")

    # Depth check — only apply if depth data is actually available.
    # Gamma API doesn't provide depth_within_2c, so 0.0 means "no data" not "empty book".
    if market.depth_within_2c > 0 and market.depth_within_2c < filters.min_depth_within_2c_shares:
        reasons.append(f"depth_2c={market.depth_within_2c:.0f} < {filters.min_depth_within_2c_shares:.0f}")

    return EligibilityResult(
        condition_id=market.condition_id,
        eligible=len(reasons) == 0,
        reasons=reasons,
    )


def compute_universe_score(
    market: MarketMetadata,
    weights: UniverseWeights,
) -> float:
    """Compute universe score from §5.

    U_i = w_1·log(1+vol_24h) + w_2·log(1+depth_2c) − w_3·spread_i
          − w_4·tox_i − w_5·resolutionRisk_i + w_6·rewardEV_i + w_7·arbEV_i
    """
    score = (
        weights.w1_volume * math.log1p(market.volume_24h)
        + weights.w2_depth * math.log1p(market.depth_within_2c)
        - weights.w3_spread * (market.spread * 100)  # spread in cents
        - weights.w4_toxicity * market.toxicity_estimate
        - weights.w5_resolution_risk * market.resolution_risk
        + weights.w6_reward_ev * market.reward_ev
        + weights.w7_arb_ev * market.arb_ev
    )
    return score


def select_universe(
    markets: list[MarketMetadata],
    filters: MarketFiltersConfig,
    weights: UniverseWeights,
    max_markets: int = 20,
    now: datetime | None = None,
) -> list[ScoredMarket]:
    """Select top-K markets for quoting.

    1. Filter by eligibility
    2. Score each eligible market
    3. Sort by score descending
    4. Return top K

    Args:
        markets: All candidate markets with metadata.
        filters: Eligibility filter configuration.
        weights: Universe scoring weights.
        max_markets: Maximum number of markets to quote (K).
        now: Current time for time-based checks.

    Returns:
        Sorted list of top-K ScoredMarket objects.
    """
    scored: list[ScoredMarket] = []

    for market in markets:
        eligibility = check_eligibility(market, filters, now)

        if eligibility.eligible:
            score = compute_universe_score(market, weights)
            scored.append(ScoredMarket(
                metadata=market,
                score=score,
                eligible=True,
            ))
        else:
            scored.append(ScoredMarket(
                metadata=market,
                score=0.0,
                eligible=False,
                eligibility_reasons=eligibility.reasons,
            ))

    # Sort eligible markets by score descending
    eligible = [s for s in scored if s.eligible]
    eligible.sort(key=lambda s: s.score, reverse=True)

    # Assign ranks and truncate to top K
    selected = eligible[:max_markets]
    for i, s in enumerate(selected):
        s.rank = i + 1

    logger.info(
        "universe_selected",
        total_candidates=len(markets),
        eligible=len(eligible),
        selected=len(selected),
        top_score=selected[0].score if selected else 0.0,
    )

    return selected


def estimate_toxicity(
    recent_trades: list[dict[str, Any]],
    book_imbalance: float,
    spread: float,
) -> float:
    """Estimate market toxicity (adverse selection risk).

    Higher toxicity = more informed flow = wider spreads needed.
    Simple heuristic v1: combines trade aggressiveness + imbalance + narrow spread.
    """
    if not recent_trades:
        return 0.0

    # Count aggressive trades (taker-initiated)
    total_trades = len(recent_trades)
    aggressive = sum(
        1 for t in recent_trades
        if t.get("type") in ("FAK", "FOK", "market")
    )
    aggression_ratio = aggressive / total_trades if total_trades > 0 else 0.0

    # High imbalance + narrow spread = potential informed flow
    imbalance_signal = abs(book_imbalance)

    # Narrow spread with high volume = potentially toxic
    spread_signal = max(0.0, 1.0 - spread * 50)  # Normalize: 2¢ → 0, 0¢ → 1

    toxicity = 0.4 * aggression_ratio + 0.3 * imbalance_signal + 0.3 * spread_signal
    return min(1.0, max(0.0, toxicity))


def estimate_resolution_risk(
    end_date: datetime | None,
    has_disputes: bool = False,
    has_clarifications: bool = False,
    now: datetime | None = None,
) -> float:
    """Estimate resolution risk for a market.

    Higher near resolution time, with disputes, or unclear rules.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    risk = 0.0

    if end_date:
        hours_remaining = max(0, (end_date - now).total_seconds() / 3600)
        # Sigmoid-ish: risk increases as we approach resolution
        if hours_remaining < 1:
            risk += 0.8
        elif hours_remaining < 6:
            risk += 0.5
        elif hours_remaining < 24:
            risk += 0.2
        elif hours_remaining < 72:
            risk += 0.05

    if has_disputes:
        risk += 0.3
    if has_clarifications:
        risk += 0.15

    return min(1.0, risk)

"""Universe builder — integration function that ties everything together.

Fetches markets from Gamma, enriches with reward/fee data, and returns
scored EnrichedMarket objects ready for trading.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

from pmm2.universe.fee_surface import FeeSurface
from pmm2.universe.metadata import EnrichedMarket, compute_ambiguity_score
from pmm2.universe.reward_surface import RewardSurface

if TYPE_CHECKING:
    from pmm1.api.gamma import GammaClient
    from pmm1.api.rewards import RewardsClient

logger = structlog.get_logger(__name__)


async def build_enriched_universe(
    gamma_client: GammaClient,
    rewards_client: RewardsClient,
    settings: Any | None = None,
    max_pages: int = 1,
) -> list[EnrichedMarket]:
    """Build enriched universe from Gamma markets + reward/fee surfaces.

    Steps:
    1. Fetch top 200 markets from Gamma (max_pages=1, limit=200)
    2. Refresh reward surface
    3. Build fee surface from Gamma data
    4. Enrich each market with reward/fee/metadata
    5. Return list of EnrichedMarket objects

    This function does NOT do scoring or selection — that's the job of UniverseScorer.
    It just builds the enriched metadata layer.

    Args:
        gamma_client: GammaClient instance.
        rewards_client: RewardsClient instance with API credentials.
        settings: Optional settings object (reserved for future use).
        max_pages: Maximum pages to fetch from Gamma (default 1 = 200 markets).

    Returns:
        List of EnrichedMarket objects.
    """
    logger.info("build_enriched_universe_start", max_pages=max_pages)

    # Step 1: Fetch markets from Gamma
    # Sort by volume_24hr descending to get most liquid markets first
    raw_markets = await gamma_client.get_markets(
        active=True,
        closed=False,
        limit=200,
        order_by="volume24hr",
        ascending=False,
        max_pages=max_pages,
    )

    logger.info("gamma_markets_fetched", count=len(raw_markets))

    # Step 2: Refresh reward surface
    reward_surface = RewardSurface(ttl_seconds=300)
    await reward_surface.refresh(rewards_client)

    # Step 3: Build fee surface from Gamma data
    # Convert GammaMarket pydantic models to dicts for fee_surface
    fee_surface = FeeSurface(ttl_seconds=300)
    raw_dicts = [m.model_dump() for m in raw_markets]
    fee_surface.update_from_markets(raw_dicts)

    # Step 4: Enrich each market
    enriched: list[EnrichedMarket] = []
    now = datetime.now(timezone.utc)

    for market in raw_markets:
        # Parse token IDs from clobTokenIds JSON
        token_ids = market.token_ids  # Uses @property
        token_id_yes = token_ids[0] if len(token_ids) > 0 else ""
        token_id_no = token_ids[1] if len(token_ids) > 1 else ""

        # Compute mid and spread
        mid = 0.0
        spread_cents = 0.0
        if market.best_bid > 0 and market.best_ask > 0:
            mid = (market.best_bid + market.best_ask) / 2.0
            spread = market.best_ask - market.best_bid
            spread_cents = spread * 100.0 if spread < 1.0 else spread

        # Get reward info
        reward_eligible, reward_daily_rate, reward_min_size, reward_max_spread = (
            reward_surface.get_reward_info(
                condition_id=market.condition_id,
                token_id_yes=token_id_yes,
                token_id_no=token_id_no,
            )
        )

        # Get fee info
        fees_enabled, fee_rate = fee_surface.get_fee_info(market.condition_id)

        # Compute time to resolution
        hours_to_resolution = 0.0
        if market.end_date:
            end_dt = market.end_date
            # Ensure timezone-aware comparison
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            delta = end_dt - now
            hours_to_resolution = max(0.0, delta.total_seconds() / 3600.0)

        # Compute ambiguity score
        ambiguity_score = compute_ambiguity_score(
            question=market.question,
            description=market.description,
        )

        # Build EnrichedMarket
        enriched_market = EnrichedMarket(
            condition_id=market.condition_id,
            question=market.question,
            token_id_yes=token_id_yes,
            token_id_no=token_id_no,
            event_id="",  # TODO: Extract from Gamma if available
            best_bid=market.best_bid,
            best_ask=market.best_ask,
            mid=mid,
            spread_cents=spread_cents,
            volume_24h=market.volume_24hr,
            liquidity=market.liquidity,
            reward_eligible=reward_eligible,
            reward_daily_rate=reward_daily_rate,
            reward_min_size=reward_min_size,
            reward_max_spread=reward_max_spread,
            fees_enabled=fees_enabled,
            fee_rate=fee_rate,
            hours_to_resolution=hours_to_resolution,
            is_neg_risk=market.neg_risk,
            has_placeholder_outcomes=False,  # TODO: Implement detection
            ambiguity_score=ambiguity_score,
            tick_size="0.01",  # Standard Polymarket tick size
            accepting_orders=market.accepting_orders,
            active=market.active,
        )

        enriched.append(enriched_market)

    logger.info(
        "enriched_universe_built",
        total=len(enriched),
        reward_eligible=sum(1 for m in enriched if m.reward_eligible),
        fee_enabled=sum(1 for m in enriched if m.fees_enabled),
    )

    return enriched

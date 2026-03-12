"""Quote planner — convert allocator output into concrete quote plans.

Transforms QuoteBundles from allocator into target quote ladders with:
- Concrete prices (adjusted for tick size)
- Concrete sizes (in shares)
- Intent classification (reward_core, reward_depth, edge_extension)
- Reprice rate limiting
"""

from __future__ import annotations

import time

import structlog
from pydantic import BaseModel

from pmm2.scorer.bundles import QuoteBundle

logger = structlog.get_logger(__name__)


class QuoteLadderRung(BaseModel):
    """A single order in the target quote plan."""

    token_id: str
    condition_id: str
    side: str  # BUY or SELL
    price: float
    size: float
    capital_usdc: float = 0.0
    fill_prob_30s: float = 0.0
    quote_role: str = ""
    intent: str = "reward_core"  # reward_core, reward_depth, edge_extension
    bundle_type: str = "B1"  # B1, B2, B3
    neg_risk: bool = False

    model_config = {"frozen": False}


class TargetQuotePlan(BaseModel):
    """Target quote plan for a market."""

    condition_id: str
    target_capital_usdc: float = 0.0
    target_slots: int = 0
    ladder: list[QuoteLadderRung] = []
    max_reprices_per_minute: int = 3
    priority: str = "reward_core"  # reward_core, reward_depth, edge_extension

    model_config = {"frozen": False}


class QuotePlanner:
    """Convert allocator output into target quote plans.

    Input: AllocationPlan (funded bundles per market)
    Output: TargetQuotePlan per market (concrete prices, sizes, intents)

    Key features:
    - Converts QuoteBundle (bid/ask pairs) into individual rungs
    - Maps bundle types to intent classification
    - Rate-limits repricing to prevent spam
    - Handles neg-risk token routing
    """

    def __init__(self, max_reprices_per_minute: int = 3):
        """Initialize quote planner.

        Args:
            max_reprices_per_minute: max repricing events per market per minute
        """
        self.max_reprices_per_minute = max_reprices_per_minute
        self.reprice_counts: dict[str, list[float]] = {}  # condition_id → timestamps

    def plan_market(
        self,
        bundles: list[QuoteBundle],
        token_id_yes: str,
        token_id_no: str,
        condition_id: str,
        neg_risk: bool = False,
        tick_size: float = 0.01,
    ) -> TargetQuotePlan:
        """Generate target quote ladder from funded bundles.

        Each bundle → 2 rungs (bid + ask).
        B1 at inside, B2 one tick deeper, B3 two ticks deeper.

        Args:
            bundles: list of funded QuoteBundles for this market
            token_id_yes: YES token ID
            token_id_no: NO token ID
            condition_id: market condition ID
            neg_risk: if True, use neg-risk routing
            tick_size: price tick size

        Returns:
            TargetQuotePlan with concrete ladder
        """
        if not bundles:
            logger.debug("plan_market_no_bundles", condition_id=condition_id)
            return TargetQuotePlan(condition_id=condition_id)

        ladder: list[QuoteLadderRung] = []
        total_capital = 0.0
        total_slots = 0

        # Bundle type → intent mapping
        intent_map = {
            "B1": "reward_core",
            "B2": "reward_depth",
            "B3": "edge_extension",
        }

        # Priority: B1 > B2 > B3
        priority = "reward_core"
        if any(b.bundle_type == "B2" for b in bundles):
            priority = "reward_depth"
        if any(b.bundle_type == "B3" for b in bundles):
            priority = "edge_extension"

        for bundle in bundles:
            intent = intent_map.get(bundle.bundle_type, "reward_core")

            # Bid rung (BUY YES or SELL NO depending on neg_risk)
            if bundle.bid_size > 0 and bundle.bid_price > 0:
                # Neg-risk: BUY NO token at bid price
                # Normal: BUY YES token at bid price
                bid_token = token_id_no if neg_risk else token_id_yes
                bid_side = "BUY"

                bid_rung = QuoteLadderRung(
                    token_id=bid_token,
                    condition_id=condition_id,
                    side=bid_side,
                    price=round(bundle.bid_price / tick_size) * tick_size,
                    size=bundle.bid_size,
                    capital_usdc=bundle.bid_size * bundle.bid_price,
                    fill_prob_30s=bundle.fill_prob_bid,
                    quote_role="bid",
                    intent=intent,
                    bundle_type=bundle.bundle_type,
                    neg_risk=neg_risk,
                )
                ladder.append(bid_rung)
                total_slots += 1

            # Ask rung (SELL YES or BUY NO depending on neg_risk)
            if bundle.ask_size > 0 and bundle.ask_price > 0:
                # Neg-risk: SELL NO token at ask price → convert to BUY YES at (1 - ask)
                # Normal: SELL YES token at ask price
                if neg_risk:
                    ask_token = token_id_yes
                    ask_side = "BUY"
                    # Convert NO sell @ 0.35 → YES buy @ 0.65
                    ask_price = 1.0 - bundle.ask_price
                else:
                    ask_token = token_id_yes
                    ask_side = "SELL"
                    ask_price = bundle.ask_price

                ask_rung = QuoteLadderRung(
                    token_id=ask_token,
                    condition_id=condition_id,
                    side=ask_side,
                    price=round(ask_price / tick_size) * tick_size,
                    size=bundle.ask_size,
                    capital_usdc=bundle.ask_size * max(0.0, 1.0 - ask_price),
                    fill_prob_30s=bundle.fill_prob_ask,
                    quote_role="ask",
                    intent=intent,
                    bundle_type=bundle.bundle_type,
                    neg_risk=neg_risk,
                )
                ladder.append(ask_rung)
                total_slots += 1

            total_capital += bundle.capital_usdc

        plan = TargetQuotePlan(
            condition_id=condition_id,
            target_capital_usdc=total_capital,
            target_slots=total_slots,
            ladder=ladder,
            max_reprices_per_minute=self.max_reprices_per_minute,
            priority=priority,
        )

        logger.info(
            "quote_plan_generated",
            condition_id=condition_id,
            rungs=len(ladder),
            capital=total_capital,
            slots=total_slots,
            priority=priority,
        )

        return plan

    def can_reprice(self, condition_id: str) -> bool:
        """Check if we're within max_reprices_per_minute for this market.

        Args:
            condition_id: market condition ID

        Returns:
            True if reprice is allowed, False otherwise
        """
        now = time.time()
        cutoff = now - 60.0  # 1 minute ago

        # Get recent reprice timestamps
        recent = self.reprice_counts.get(condition_id, [])

        # Filter to last minute
        recent = [ts for ts in recent if ts >= cutoff]

        # Update stored list
        self.reprice_counts[condition_id] = recent

        # Check limit
        allowed = len(recent) < self.max_reprices_per_minute

        if not allowed:
            logger.warning(
                "reprice_rate_limit_hit",
                condition_id=condition_id,
                recent_count=len(recent),
                limit=self.max_reprices_per_minute,
            )

        return allowed

    def record_reprice(self, condition_id: str):
        """Record a reprice event.

        Args:
            condition_id: market condition ID
        """
        now = time.time()
        recent = self.reprice_counts.get(condition_id, [])
        recent.append(now)
        self.reprice_counts[condition_id] = recent

        logger.debug(
            "reprice_recorded",
            condition_id=condition_id,
            count=len(recent),
        )

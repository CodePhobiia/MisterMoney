"""Neg-risk arbitrage with two-phase commit and rollback.

Phase 1: Buy all required NO tokens (FOK). If any leg fails, sell back filled legs.
Phase 2: On-chain conversion (buy NO tokens → redeem for USDC → buy YES tokens).
Phase 3: Sell YES tokens to close the arb.

Each phase has explicit rollback on failure.

STATUS: DISABLED — framework stub for future implementation.
         Neg-risk arb remains disabled in config (T0-09).
         This file provides the architectural skeleton for a safe,
         atomic arb execution with proper rollback semantics.
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


class ArbLeg(BaseModel):
    """A single leg of a neg-risk arb."""

    token_id: str
    side: str  # BUY or SELL
    price: float
    size: float
    order_type: str = "FOK"  # Fill-or-kill for atomicity
    filled: bool = False
    fill_price: float = 0.0
    fill_size: float = 0.0
    order_id: str = ""


class ArbPlan(BaseModel):
    """Complete arb execution plan."""

    event_id: str
    expected_profit_usd: float
    legs: list[ArbLeg]
    phase: str = "pending"  # pending, phase1, phase2, phase3, complete, rolled_back
    rollback_legs: list[ArbLeg] = []


class NegRiskArbV2:
    """Two-phase commit neg-risk arbitrage executor.

    DISABLED by default. Enable only after thorough testing.

    Architecture:
    1. Plan: Identify arb opportunity, compute legs
    2. Phase 1 (Acquire): FOK orders for all NO tokens
       - If any leg fails → rollback (sell filled legs)
    3. Phase 2 (Convert): On-chain NO→USDC→YES conversion
       - If conversion fails → sell NO tokens back
    4. Phase 3 (Close): Sell YES tokens to realize profit
       - If sell fails → hold position (manual intervention)

    Safety:
    - FOK orders ensure legs don't partially fill
    - Explicit rollback at each phase
    - Max loss bounded by spread × total size
    - Disabled by default in config
    """

    def __init__(self, clob_client=None, risk_limits=None, enabled: bool = False):
        self.clob_client = clob_client
        self.risk_limits = risk_limits
        self.enabled = enabled
        self._active_plans: dict[str, ArbPlan] = {}

    async def evaluate_opportunity(
        self,
        event_id: str,
        market_prices: dict[str, float],
        market_sizes: dict[str, float],
    ) -> ArbPlan | None:
        """Evaluate if a neg-risk arb opportunity exists.

        For binary markets: YES + NO should equal $1.
        If Σ(best_ask_NO) < 1.0, buying all NOs and converting is profitable.

        Args:
            event_id: Event/condition cluster ID
            market_prices: token_id → best ask price
            market_sizes: token_id → available size at best ask

        Returns:
            ArbPlan if opportunity exists, None otherwise.
        """
        if not self.enabled:
            return None

        # TODO: Implement opportunity evaluation
        # 1. Sum best ask prices for all NO tokens in event
        # 2. If sum < 1.0 (minus fees), there's an arb
        # 3. Compute optimal size (min across all legs)
        # 4. Build ArbPlan with legs

        logger.debug(
            "neg_risk_arb_evaluate",
            event_id=event_id,
            enabled=self.enabled,
        )
        return None

    async def execute_plan(self, plan: ArbPlan) -> bool:
        """Execute an arb plan with two-phase commit.

        Returns True if arb completed successfully, False if rolled back.
        """
        if not self.enabled:
            logger.warning("neg_risk_arb_disabled")
            return False

        # TODO: Implement three-phase execution
        # Phase 1: Place FOK orders for all legs
        # Phase 2: On-chain conversion
        # Phase 3: Sell to close

        logger.info(
            "neg_risk_arb_execute_stub",
            event_id=plan.event_id,
            expected_profit=plan.expected_profit_usd,
            legs=len(plan.legs),
        )
        return False

    async def _rollback_phase1(self, plan: ArbPlan) -> None:
        """Rollback Phase 1 — sell back any filled NO legs."""
        # TODO: Implement rollback
        logger.warning(
            "neg_risk_arb_rollback_phase1",
            event_id=plan.event_id,
            filled_legs=sum(1 for leg in plan.legs if leg.filled),
        )
        plan.phase = "rolled_back"

    async def _rollback_phase2(self, plan: ArbPlan) -> None:
        """Rollback Phase 2 — sell NO tokens back if conversion fails."""
        # TODO: Implement rollback
        logger.warning(
            "neg_risk_arb_rollback_phase2",
            event_id=plan.event_id,
        )
        plan.phase = "rolled_back"

"""Resolution risk handling from §13.

- Stop quoting well before resolution
- Freeze inventory accumulation if dispute/clarification risk elevated
- Redeem only after confirmed resolution
- Disputes can escalate through challenge rounds + UMA voting (days)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from enum import Enum

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class ResolutionState(str, Enum):
    """Resolution lifecycle states."""

    ACTIVE = "active"
    APPROACHING = "approaching"  # Within caution window
    IMMINENT = "imminent"  # Within hard stop window
    RESOLVING = "resolving"  # Resolution submitted
    DISPUTED = "disputed"  # Under dispute
    RESOLVED = "resolved"  # Final resolution confirmed
    REDEEMABLE = "redeemable"  # Can redeem tokens


class MarketResolutionState(BaseModel):
    """Resolution state for a single market."""

    condition_id: str
    state: ResolutionState = ResolutionState.ACTIVE
    end_date: datetime | None = None
    hours_remaining: float = float("inf")
    has_dispute: bool = False
    has_clarification: bool = False
    dispute_start_time: float | None = None
    resolved_outcome: str | None = None  # "YES", "NO", or None
    should_quote: bool = True
    should_accumulate: bool = True
    can_redeem: bool = False
    last_update: float = Field(default_factory=time.time)


class ResolutionRiskManager:
    """Manages resolution risk across all active markets.

    Policy:
    - Stop quoting when time_remaining < hard_stop_hours
    - Reduce position sizes when time_remaining < caution_hours
    - Freeze accumulation on dispute/clarification
    - Only redeem after confirmed resolution
    """

    def __init__(
        self,
        caution_hours: float = 6.0,
        hard_stop_hours: float = 1.0,
        dispute_freeze: bool = True,
    ) -> None:
        self._caution_hours = caution_hours
        self._hard_stop_hours = hard_stop_hours
        self._dispute_freeze = dispute_freeze
        self._states: dict[str, MarketResolutionState] = {}

    def update_market(
        self,
        condition_id: str,
        end_date: datetime | None = None,
        has_dispute: bool = False,
        has_clarification: bool = False,
        is_resolved: bool = False,
        resolved_outcome: str | None = None,
    ) -> MarketResolutionState:
        """Update resolution state for a market."""
        now = datetime.now(timezone.utc)

        # Calculate hours remaining
        hours_remaining = float("inf")
        if end_date:
            delta = (end_date - now).total_seconds()
            hours_remaining = max(0, delta / 3600)

        # Determine state
        if is_resolved:
            state = ResolutionState.RESOLVED
        elif has_dispute:
            state = ResolutionState.DISPUTED
        elif hours_remaining <= self._hard_stop_hours:
            state = ResolutionState.IMMINENT
        elif hours_remaining <= self._caution_hours:
            state = ResolutionState.APPROACHING
        else:
            state = ResolutionState.ACTIVE

        # Determine actions
        should_quote = state in (ResolutionState.ACTIVE, ResolutionState.APPROACHING)
        should_accumulate = state == ResolutionState.ACTIVE

        if has_dispute and self._dispute_freeze:
            should_accumulate = False

        if has_clarification:
            should_accumulate = False

        can_redeem = state == ResolutionState.RESOLVED and resolved_outcome is not None

        market_state = MarketResolutionState(
            condition_id=condition_id,
            state=state,
            end_date=end_date,
            hours_remaining=hours_remaining,
            has_dispute=has_dispute,
            has_clarification=has_clarification,
            dispute_start_time=time.time() if has_dispute else None,
            resolved_outcome=resolved_outcome,
            should_quote=should_quote,
            should_accumulate=should_accumulate,
            can_redeem=can_redeem,
        )

        old_state = self._states.get(condition_id)
        if old_state and old_state.state != market_state.state:
            logger.info(
                "resolution_state_changed",
                condition_id=condition_id[:16],
                old_state=old_state.state.value,
                new_state=market_state.state.value,
                hours_remaining=f"{hours_remaining:.1f}",
            )

        self._states[condition_id] = market_state
        return market_state

    def get(self, condition_id: str) -> MarketResolutionState | None:
        """Get resolution state for a market."""
        return self._states.get(condition_id)

    def should_quote(self, condition_id: str) -> bool:
        """Check if we should still be quoting this market."""
        state = self._states.get(condition_id)
        if state is None:
            return True  # Unknown market, assume OK
        return state.should_quote

    def should_accumulate(self, condition_id: str) -> bool:
        """Check if we should accumulate new positions in this market."""
        state = self._states.get(condition_id)
        if state is None:
            return True
        return state.should_accumulate

    def get_size_multiplier(self, condition_id: str) -> float:
        """Get position size multiplier based on resolution risk.

        1.0 = normal, 0.0 = no new positions
        """
        state = self._states.get(condition_id)
        if state is None:
            return 1.0

        if state.state == ResolutionState.ACTIVE:
            return 1.0
        elif state.state == ResolutionState.APPROACHING:
            # Scale down as we approach resolution
            if state.hours_remaining > self._caution_hours:
                return 1.0
            # Linear scale from 1.0 at caution_hours to 0.3 at hard_stop_hours
            ratio = (state.hours_remaining - self._hard_stop_hours) / (
                self._caution_hours - self._hard_stop_hours
            )
            return max(0.3, min(1.0, 0.3 + 0.7 * ratio))
        elif state.state == ResolutionState.DISPUTED:
            return 0.0  # No new positions during dispute
        else:
            return 0.0

    def get_markets_to_stop(self) -> list[str]:
        """Get condition_ids of markets that should stop quoting."""
        return [
            cid for cid, state in self._states.items()
            if not state.should_quote
        ]

    def get_redeemable_markets(self) -> list[MarketResolutionState]:
        """Get markets ready for redemption."""
        return [
            state for state in self._states.values()
            if state.can_redeem
        ]

    def remove(self, condition_id: str) -> None:
        """Remove a market from tracking."""
        self._states.pop(condition_id, None)

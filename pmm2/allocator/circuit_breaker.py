"""Circuit breaker — fast-path toxic market detection.

If markout_1s > toxicity_multiplier * historical average for a market,
immediately flag STALE → persistence optimizer can EXIT.

Cooldown before re-entering.
"""

from __future__ import annotations

import time

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


class MarketCircuitState(BaseModel):
    """Track toxic market detection."""

    condition_id: str
    markout_1s_avg: float = 0.0
    markout_1s_recent: list[float] = []  # last N markouts
    tripped: bool = False
    tripped_at: float = 0.0
    cooldown_sec: float = 300.0  # 5 min cooldown


class CircuitBreaker:
    """Fast-path toxic market detection.

    If markout_1s > 3x historical average for a market,
    immediately flag STALE → persistence optimizer can EXIT.

    Cooldown before re-entering.
    """

    def __init__(
        self,
        toxicity_multiplier: float = 3.0,
        cooldown_sec: float = 300.0,
        min_fills_for_baseline: int = 5,
        max_recent_history: int = 20,
    ):
        """Initialize circuit breaker.

        Args:
            toxicity_multiplier: trip if markout > avg * multiplier (3x)
            cooldown_sec: cooldown before re-entering (300s = 5 min)
            min_fills_for_baseline: minimum fills needed to compute baseline
            max_recent_history: max length of recent markout history
        """
        self.toxicity_multiplier = toxicity_multiplier
        self.cooldown_sec = cooldown_sec
        self.min_fills_for_baseline = min_fills_for_baseline
        self.max_recent_history = max_recent_history
        self.states: dict[str, MarketCircuitState] = {}

        logger.info(
            "circuit_breaker_initialized",
            toxicity_multiplier=toxicity_multiplier,
            cooldown_sec=cooldown_sec,
            min_fills_for_baseline=min_fills_for_baseline,
        )

    def record_fill_markout(self, condition_id: str, markout_1s: float) -> None:
        """Record a new 1s markout for circuit breaker tracking.

        Args:
            condition_id: market condition ID
            markout_1s: 1-second markout after fill (bps or decimal)
        """
        if condition_id not in self.states:
            self.states[condition_id] = MarketCircuitState(
                condition_id=condition_id,
                markout_1s_avg=markout_1s,
                markout_1s_recent=[markout_1s],
                tripped=False,
                cooldown_sec=self.cooldown_sec,
            )
        else:
            state = self.states[condition_id]
            state.markout_1s_recent.append(markout_1s)

            # Trim history if too long
            if len(state.markout_1s_recent) > self.max_recent_history:
                state.markout_1s_recent = state.markout_1s_recent[-self.max_recent_history :]

            # Update rolling average
            if len(state.markout_1s_recent) >= self.min_fills_for_baseline:
                state.markout_1s_avg = sum(state.markout_1s_recent) / len(
                    state.markout_1s_recent
                )

        logger.debug(
            "circuit_breaker_markout_recorded",
            condition_id=condition_id,
            markout_1s=markout_1s,
            avg=self.states[condition_id].markout_1s_avg,
            history_len=len(self.states[condition_id].markout_1s_recent),
        )

    def is_tripped(self, condition_id: str) -> bool:
        """Check if circuit breaker is active for this market.

        Args:
            condition_id: market condition ID

        Returns:
            True if circuit breaker is tripped
        """
        if condition_id not in self.states:
            return False

        state = self.states[condition_id]

        # Check if cooled down
        if state.tripped:
            elapsed = time.time() - state.tripped_at
            if elapsed >= state.cooldown_sec:
                # Cooldown elapsed, reset
                state.tripped = False
                logger.info(
                    "circuit_breaker_cooled_down",
                    condition_id=condition_id,
                    elapsed=elapsed,
                )
                return False
            else:
                return True

        return False

    def check_and_trip(self, condition_id: str, markout_1s: float) -> bool:
        """Record markout and trip if threshold exceeded.

        Args:
            condition_id: market condition ID
            markout_1s: 1-second markout after fill

        Returns:
            True if tripped
        """
        # Record the markout
        self.record_fill_markout(condition_id, markout_1s)

        state = self.states.get(condition_id)
        if state is None:
            return False

        # Check if we have enough history to compute baseline
        if len(state.markout_1s_recent) < self.min_fills_for_baseline:
            logger.debug(
                "circuit_breaker_insufficient_history",
                condition_id=condition_id,
                history_len=len(state.markout_1s_recent),
            )
            return False

        # Check if markout exceeds threshold
        threshold = state.markout_1s_avg * self.toxicity_multiplier
        if markout_1s > threshold:
            # Trip the circuit breaker
            state.tripped = True
            state.tripped_at = time.time()

            logger.warning(
                "circuit_breaker_tripped",
                condition_id=condition_id,
                markout_1s=markout_1s,
                avg=state.markout_1s_avg,
                threshold=threshold,
                multiplier=self.toxicity_multiplier,
            )
            return True

        return False

    def reset_if_cooled(self, condition_id: str) -> bool:
        """Reset circuit breaker if cooldown elapsed.

        Args:
            condition_id: market condition ID

        Returns:
            True if reset (was tripped, now cooled)
        """
        state = self.states.get(condition_id)
        if state is None:
            return False

        if state.tripped:
            elapsed = time.time() - state.tripped_at
            if elapsed >= state.cooldown_sec:
                state.tripped = False
                logger.info(
                    "circuit_breaker_reset",
                    condition_id=condition_id,
                    elapsed=elapsed,
                )
                return True

        return False

    def force_reset(self, condition_id: str) -> None:
        """Force reset circuit breaker (manual override).

        Args:
            condition_id: market condition ID
        """
        state = self.states.get(condition_id)
        if state:
            state.tripped = False
            logger.info("circuit_breaker_force_reset", condition_id=condition_id)

    def get_state(self, condition_id: str) -> MarketCircuitState | None:
        """Get circuit breaker state for a market.

        Args:
            condition_id: market condition ID

        Returns:
            MarketCircuitState or None if not tracked
        """
        return self.states.get(condition_id)

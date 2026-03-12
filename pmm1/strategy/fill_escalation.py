"""Fill Escalation — tracks time since last fill and escalates quote aggressiveness.

When fills stop, this module:
1. Improves quotes progressively (closer to top-of-book)
2. Eventually takes liquidity with small taker orders to bootstrap inventory

§ Fill-Speed Mode: designed to solve the "no fills" problem for small capital (<$100).
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


class FillEscalationConfig(BaseModel):
    """Configuration for fill escalation ladder."""

    enabled: bool = True
    # Time thresholds (seconds) and tick improvements
    level_1_secs: int = 15 * 60  # 15 min
    level_1_ticks: int = 1
    level_2_secs: int = 30 * 60  # 30 min
    level_2_ticks: int = 2
    level_3_secs: int = 45 * 60  # 45 min
    level_3_ticks: int = 3
    # Taker bootstrap
    taker_enabled: bool = True
    taker_trigger_secs: int = 20 * 60  # 20 min
    taker_min_shares: float = 5.0


class FillEscalator:
    """Tracks time since last fill and escalates quote aggressiveness.

    Usage:
        escalator = FillEscalator(config)
        
        # In quote loop:
        improvement_ticks = escalator.get_escalation_ticks()
        bid_price += improvement_ticks * tick_size
        ask_price -= improvement_ticks * tick_size
        
        # In fill callback:
        escalator.record_fill()

        # Taker bootstrap:
        if escalator.should_take_liquidity():
            submit_taker_order(...)
            # Do not reset here; a fill should clear the one-shot guard.
    """

    def __init__(self, config: FillEscalationConfig) -> None:
        self.config = config
        self.last_fill_time = time.time()
        self._has_taken_this_cycle = False
        self._last_escalation_level = 0

    def record_fill(self) -> None:
        """Record that a fill occurred — resets escalation."""
        prev_elapsed = time.time() - self.last_fill_time
        self.last_fill_time = time.time()
        self._has_taken_this_cycle = False
        self._last_escalation_level = 0

        logger.info(
            "fill_recorded_escalation_reset",
            prev_no_fill_mins=f"{prev_elapsed / 60:.1f}",
        )

    def get_escalation_ticks(self) -> int:
        """Returns number of extra ticks to improve quotes by.

        Returns:
            Number of ticks to add to bid / subtract from ask.
        """
        if not self.config.enabled:
            return 0

        elapsed = time.time() - self.last_fill_time

        # Find the highest applicable escalation level
        improvement = 0
        current_level = 0

        if elapsed >= self.config.level_3_secs:
            improvement = self.config.level_3_ticks
            current_level = 3
        elif elapsed >= self.config.level_2_secs:
            improvement = self.config.level_2_ticks
            current_level = 2
        elif elapsed >= self.config.level_1_secs:
            improvement = self.config.level_1_ticks
            current_level = 1

        # Log level changes
        if current_level != self._last_escalation_level:
            logger.info(
                "escalation_level_changed",
                from_level=self._last_escalation_level,
                to_level=current_level,
                improvement_ticks=improvement,
                no_fill_mins=f"{elapsed / 60:.1f}",
            )
            self._last_escalation_level = current_level

        return improvement

    def should_take_liquidity(self) -> bool:
        """After configured time with zero fills, allow ONE small taker order.

        Returns:
            True if we should submit a taker order this cycle.
        """
        if not self.config.enabled or not self.config.taker_enabled:
            return False

        elapsed = time.time() - self.last_fill_time

        if elapsed >= self.config.taker_trigger_secs and not self._has_taken_this_cycle:
            logger.info(
                "taker_bootstrap_triggered",
                no_fill_mins=f"{elapsed / 60:.1f}",
            )
            self._has_taken_this_cycle = True
            return True

        return False

    def reset_taker_cycle(self) -> None:
        """Manually allow another taker attempt after a failed submission.

        Successful taker submissions should stay latched until `record_fill()`.
        """
        self._has_taken_this_cycle = False
        logger.debug("taker_cycle_reset")

    def get_status(self) -> dict[str, Any]:
        """Get current escalation status for logging/monitoring."""
        elapsed = time.time() - self.last_fill_time
        return {
            "enabled": self.config.enabled,
            "no_fill_secs": elapsed,
            "no_fill_mins": elapsed / 60,
            "current_level": self._last_escalation_level,
            "escalation_ticks": self.get_escalation_ticks(),
            "has_taken_this_cycle": self._has_taken_this_cycle,
        }

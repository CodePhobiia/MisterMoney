"""Drawdown governor — 3 tiers from §14.

| Trigger               | Action                        |
|-----------------------|-------------------------------|
| Daily DD > 1.5% NAV  | Pause taker trades            |
| Daily DD > 2.5% NAV  | Quote wider, cut sizes 50%    |
| Daily DD > 4% NAV    | FLATTEN_ONLY                  |
"""

from __future__ import annotations

import time
from enum import Enum

import structlog
from pydantic import BaseModel, Field

from pmm1.settings import RiskConfig

logger = structlog.get_logger(__name__)


class DrawdownTier(str, Enum):
    """Drawdown severity levels."""

    NORMAL = "normal"
    TIER1_PAUSE_TAKER = "tier1_pause_taker"
    TIER2_WIDER_SMALLER = "tier2_wider_smaller"
    TIER3_FLATTEN_ONLY = "tier3_flatten_only"


class DrawdownState(BaseModel):
    """Current drawdown tracking state."""

    tier: DrawdownTier = DrawdownTier.NORMAL
    daily_high_watermark: float = 0.0
    current_nav: float = 0.0
    daily_pnl: float = 0.0
    drawdown_pct: float = 0.0
    day_start_nav: float = 0.0
    day_start_time: float = Field(default_factory=time.time)
    tier_changed_at: float = 0.0

    @property
    def is_normal(self) -> bool:
        return self.tier == DrawdownTier.NORMAL

    @property
    def should_pause_taker(self) -> bool:
        return self.tier in (
            DrawdownTier.TIER1_PAUSE_TAKER,
            DrawdownTier.TIER2_WIDER_SMALLER,
            DrawdownTier.TIER3_FLATTEN_ONLY,
        )

    @property
    def should_widen_quotes(self) -> bool:
        return self.tier in (
            DrawdownTier.TIER2_WIDER_SMALLER,
            DrawdownTier.TIER3_FLATTEN_ONLY,
        )

    @property
    def should_flatten_only(self) -> bool:
        return self.tier == DrawdownTier.TIER3_FLATTEN_ONLY

    @property
    def size_multiplier(self) -> float:
        """Size multiplier based on current tier."""
        if self.tier == DrawdownTier.TIER2_WIDER_SMALLER:
            return 0.5
        elif self.tier == DrawdownTier.TIER3_FLATTEN_ONLY:
            return 0.0
        return 1.0

    @property
    def spread_multiplier(self) -> float:
        """Spread multiplier (wider quotes in tier 2+)."""
        if self.tier == DrawdownTier.TIER2_WIDER_SMALLER:
            return 1.5  # 50% wider
        elif self.tier == DrawdownTier.TIER3_FLATTEN_ONLY:
            return 3.0
        return 1.0


class DrawdownGovernor:
    """Monitors PnL and enforces drawdown limits.

    Tracks daily drawdown relative to NAV and transitions between tiers.
    Resets at start of each trading day.
    """

    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self._state = DrawdownState()
        self._day_boundary_hour = 0  # UTC midnight
        self._on_tier_change = None  # Optional async callback

    def set_on_tier_change(self, callback) -> None:
        """Set an optional callback invoked when drawdown tier changes.

        Callback signature: async def cb(old_tier: str, new_tier: str, dd_pct: float) -> None
        """
        self._on_tier_change = callback

    @property
    def state(self) -> DrawdownState:
        return self._state

    def initialize(self, starting_nav: float) -> None:
        """Initialize at the start of a trading day."""
        self._state = DrawdownState(
            daily_high_watermark=starting_nav,
            current_nav=starting_nav,
            day_start_nav=starting_nav,
            day_start_time=time.time(),
        )
        logger.info("drawdown_governor_initialized", nav=starting_nav)

    def reset_daily(self, current_nav: float) -> None:
        """Reset for a new trading day."""
        logger.info(
            "drawdown_daily_reset",
            previous_pnl=self._state.daily_pnl,
            previous_tier=self._state.tier.value,
        )
        self.initialize(current_nav)

    def update(self, current_nav: float) -> DrawdownState:
        """Update with latest NAV and compute drawdown tier.

        Should be called frequently (every quote cycle).

        Args:
            current_nav: Current total NAV.

        Returns:
            Updated DrawdownState.
        """
        old_tier = self._state.tier

        self._state.current_nav = current_nav

        # Update high watermark
        if current_nav > self._state.daily_high_watermark:
            self._state.daily_high_watermark = current_nav

        # Daily PnL
        self._state.daily_pnl = current_nav - self._state.day_start_nav

        # Drawdown from high-water mark (as percentage of peak NAV)
        if self._state.daily_high_watermark > 0:
            self._state.drawdown_pct = (
                (self._state.daily_high_watermark - current_nav) / self._state.daily_high_watermark
            )
        else:
            self._state.drawdown_pct = 0.0

        # Determine tier
        dd = self._state.drawdown_pct

        if dd >= self.config.daily_flatten_drawdown_nav:
            self._state.tier = DrawdownTier.TIER3_FLATTEN_ONLY
        elif dd >= self.config.daily_wider_drawdown_nav:
            self._state.tier = DrawdownTier.TIER2_WIDER_SMALLER
        elif dd >= self.config.daily_pause_drawdown_nav:
            self._state.tier = DrawdownTier.TIER1_PAUSE_TAKER
        else:
            self._state.tier = DrawdownTier.NORMAL

        # Log tier transitions
        if self._state.tier != old_tier:
            self._state.tier_changed_at = time.time()
            level = "critical" if self._state.tier == DrawdownTier.TIER3_FLATTEN_ONLY else "warning"
            log_fn = getattr(logger, level)
            log_fn(
                "drawdown_tier_changed",
                old_tier=old_tier.value,
                new_tier=self._state.tier.value,
                drawdown_pct=f"{dd*100:.2f}%",
                daily_pnl=f"{self._state.daily_pnl:.2f}",
                nav=f"{current_nav:.2f}",
            )

            # Fire optional notification callback
            if self._on_tier_change:
                import asyncio
                try:
                    coro = self._on_tier_change(old_tier.value, self._state.tier.value, dd * 100)
                    if asyncio.iscoroutine(coro):
                        asyncio.ensure_future(coro)
                except Exception:
                    pass  # Don't let notification failures affect drawdown governor

        return self._state

    def should_check_daily_reset(self) -> bool:
        """Check if it's time for a daily reset (UTC midnight crossing)."""
        import datetime as dt

        now = dt.datetime.now(dt.timezone.utc)
        day_start = dt.datetime.fromtimestamp(
            self._state.day_start_time, tz=dt.timezone.utc
        )
        return now.date() > day_start.date()

    def get_adjustments(self) -> dict[str, float]:
        """Get current adjustments based on drawdown tier."""
        return {
            "size_multiplier": self._state.size_multiplier,
            "spread_multiplier": self._state.spread_multiplier,
            "should_pause_taker": float(self._state.should_pause_taker),
            "should_flatten_only": float(self._state.should_flatten_only),
        }

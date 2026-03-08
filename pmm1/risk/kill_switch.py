"""Kill switch — immediate cancelAll() conditions from §14.

Kill switches trigger on:
- Stale market feed
- Heartbeat failure
- Position breach
- Repeated 400/401 auth failures
- 503 exchange pause/cancel-only
- Unresolved reconciliation mismatch
"""

from __future__ import annotations

import time
from enum import Enum

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class KillSwitchReason(str, Enum):
    """Reasons for triggering the kill switch."""

    STALE_MARKET_FEED = "stale_market_feed"
    HEARTBEAT_FAILURE = "heartbeat_failure"
    POSITION_BREACH = "position_breach"
    AUTH_FAILURE = "auth_failure"
    EXCHANGE_PAUSED = "exchange_paused"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    MANUAL = "manual"
    DRAWDOWN = "drawdown"


class KillSwitchEvent(BaseModel):
    """A kill switch trigger event."""

    reason: KillSwitchReason
    message: str = ""
    timestamp: float = Field(default_factory=time.time)
    auto_clear_after_s: float = 0.0  # If > 0, auto-clear after this many seconds


class KillSwitch:
    """Manages kill switch state — when triggered, all orders are canceled.

    Once triggered, the bot enters FLATTEN_ONLY mode until the condition
    is resolved and the switch is manually or automatically cleared.
    """

    def __init__(
        self,
        ws_stale_kill_s: float = 2.0,
        max_auth_failures: int = 3,
        max_reconciliation_mismatches: int = 2,
    ) -> None:
        self._ws_stale_kill_s = ws_stale_kill_s
        self._max_auth_failures = max_auth_failures
        self._max_reconciliation_mismatches = max_reconciliation_mismatches

        self._is_triggered = False
        self._active_reasons: dict[KillSwitchReason, KillSwitchEvent] = {}
        self._trigger_history: list[KillSwitchEvent] = []
        self._auth_failure_count = 0
        self._reconciliation_mismatch_count = 0

    @property
    def is_triggered(self) -> bool:
        """Check if kill switch is currently active."""
        # Auto-clear expired events
        self._check_auto_clear()
        return bool(self._active_reasons)

    @property
    def active_reasons(self) -> list[KillSwitchReason]:
        return list(self._active_reasons.keys())

    def _trigger(self, reason: KillSwitchReason, message: str = "", auto_clear_s: float = 0.0) -> None:
        """Trigger the kill switch."""
        event = KillSwitchEvent(
            reason=reason,
            message=message,
            auto_clear_after_s=auto_clear_s,
        )
        self._active_reasons[reason] = event
        self._trigger_history.append(event)
        self._is_triggered = True

        logger.critical(
            "kill_switch_triggered",
            reason=reason.value,
            message=message,
            active_reasons=[r.value for r in self._active_reasons],
        )

    def _check_auto_clear(self) -> None:
        """Clear expired auto-clear events."""
        now = time.time()
        to_clear = []
        for reason, event in self._active_reasons.items():
            if event.auto_clear_after_s > 0:
                if now - event.timestamp > event.auto_clear_after_s:
                    to_clear.append(reason)

        for reason in to_clear:
            self.clear(reason)

    def check_stale_feed(self, seconds_since_last_message: float) -> bool:
        """Check if market data feed is stale → trigger if > ws_stale_kill_s."""
        if seconds_since_last_message > self._ws_stale_kill_s:
            self._trigger(
                KillSwitchReason.STALE_MARKET_FEED,
                f"No market data for {seconds_since_last_message:.1f}s > {self._ws_stale_kill_s}s",
                auto_clear_s=120.0,  # Auto-clear after 120s — exchange restarts take ~90s
            )
            return True
        # Clear if feed is back
        if KillSwitchReason.STALE_MARKET_FEED in self._active_reasons:
            self.clear(KillSwitchReason.STALE_MARKET_FEED)
        return False

    def check_heartbeat(self, is_healthy: bool, consecutive_failures: int) -> bool:
        """Check heartbeat health → trigger on failure."""
        if not is_healthy or consecutive_failures >= 2:
            self._trigger(
                KillSwitchReason.HEARTBEAT_FAILURE,
                f"Heartbeat unhealthy, {consecutive_failures} consecutive failures",
            )
            return True
        if KillSwitchReason.HEARTBEAT_FAILURE in self._active_reasons:
            self.clear(KillSwitchReason.HEARTBEAT_FAILURE)
        return False

    def check_position_breach(self, has_breach: bool, details: str = "") -> bool:
        """Check if any position limit is breached."""
        if has_breach:
            self._trigger(
                KillSwitchReason.POSITION_BREACH,
                f"Position limit breached: {details}",
            )
            return True
        if KillSwitchReason.POSITION_BREACH in self._active_reasons:
            self.clear(KillSwitchReason.POSITION_BREACH)
        return False

    def report_auth_failure(self) -> bool:
        """Report an auth failure. Returns True if kill switch triggered."""
        self._auth_failure_count += 1
        if self._auth_failure_count >= self._max_auth_failures:
            self._trigger(
                KillSwitchReason.AUTH_FAILURE,
                f"{self._auth_failure_count} consecutive auth failures",
            )
            return True
        return False

    def report_auth_success(self) -> None:
        """Reset auth failure counter on success."""
        self._auth_failure_count = 0
        if KillSwitchReason.AUTH_FAILURE in self._active_reasons:
            self.clear(KillSwitchReason.AUTH_FAILURE)

    def report_exchange_paused(self) -> None:
        """Report that exchange is in cancel-only/paused mode (503)."""
        self._trigger(
            KillSwitchReason.EXCHANGE_PAUSED,
            "Exchange paused / cancel-only (HTTP 503)",
            auto_clear_s=120.0,
        )

    def report_reconciliation_mismatch(self, details: str = "") -> bool:
        """Report reconciliation mismatch. Returns True if kill switch triggered."""
        self._reconciliation_mismatch_count += 1
        if self._reconciliation_mismatch_count >= self._max_reconciliation_mismatches:
            self._trigger(
                KillSwitchReason.RECONCILIATION_MISMATCH,
                f"{self._reconciliation_mismatch_count} mismatches: {details}",
            )
            return True
        return False

    def report_reconciliation_clean(self) -> None:
        """Reset reconciliation mismatch counter."""
        self._reconciliation_mismatch_count = 0
        if KillSwitchReason.RECONCILIATION_MISMATCH in self._active_reasons:
            self.clear(KillSwitchReason.RECONCILIATION_MISMATCH)

    def trigger_manual(self, message: str = "Manual kill switch") -> None:
        """Manually trigger the kill switch."""
        self._trigger(KillSwitchReason.MANUAL, message)

    def trigger_drawdown(self, message: str = "Drawdown limit breached") -> None:
        """Trigger kill switch due to drawdown."""
        self._trigger(KillSwitchReason.DRAWDOWN, message)

    def clear(self, reason: KillSwitchReason | None = None) -> None:
        """Clear a specific reason or all reasons."""
        if reason is None:
            self._active_reasons.clear()
            self._auth_failure_count = 0
            self._reconciliation_mismatch_count = 0
            logger.info("kill_switch_cleared_all")
        elif reason in self._active_reasons:
            del self._active_reasons[reason]
            logger.info("kill_switch_cleared", reason=reason.value)

    def get_status(self) -> dict:
        return {
            "is_triggered": self.is_triggered,
            "active_reasons": [r.value for r in self._active_reasons],
            "auth_failure_count": self._auth_failure_count,
            "reconciliation_mismatch_count": self._reconciliation_mismatch_count,
            "total_triggers": len(self._trigger_history),
        }

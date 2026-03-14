"""Telegram notification module for fills, exits, and operational alerts."""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum

import aiohttp
import structlog

logger = structlog.get_logger(__name__)

# Get token from env or use default from openclaw config
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


class AlertSeverity(StrEnum):
    """Operational alert severities."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


_ALERT_PREFIX = {
    AlertSeverity.INFO: ("INFO", "ℹ️"),
    AlertSeverity.WARNING: ("WARNING", "⚠️"),
    AlertSeverity.CRITICAL: ("CRITICAL", "🚨"),
}


async def send_telegram(message: str) -> None:
    """Send a message via Telegram bot API.

    Args:
        message: Message text to send (supports Markdown).
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("telegram_not_configured")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message,
                    "parse_mode": "Markdown",
                },
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as response:
                if response.status != 200:
                    logger.warning(
                        "telegram_send_failed",
                        status=response.status,
                        error=await response.text(),
                    )
    except TimeoutError:
        logger.warning("telegram_send_timeout")
    except Exception as e:
        logger.warning("telegram_send_failed", error=str(e))


def format_alert(
    severity: AlertSeverity,
    event_type: str,
    details: str,
) -> str:
    """Format an operational alert message."""
    label, icon = _ALERT_PREFIX[severity]
    return f"{icon} *{label}: {event_type}*\n{details}"


async def send_alert(
    event_type: str,
    details: str,
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> None:
    """Send an operational alert via Telegram."""
    await send_telegram(format_alert(severity, event_type, details))


class AlertManager:
    """Cooldown-aware alert sender for operational signals."""

    def __init__(
        self,
        *,
        default_cooldown_s: float = 300.0,
        sender: Callable[[str], Awaitable[None]] = send_telegram,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._default_cooldown_s = default_cooldown_s
        self._sender = sender
        self._clock = clock
        self._last_sent: dict[tuple[str, str], float] = {}

    async def notify(
        self,
        severity: AlertSeverity,
        event_type: str,
        details: str,
        *,
        dedupe_key: str | None = None,
        cooldown_s: float | None = None,
    ) -> bool:
        """Send an alert if the cooldown for this key has elapsed."""
        key = dedupe_key or event_type
        cooldown = self._default_cooldown_s if cooldown_s is None else cooldown_s
        now = self._clock()
        bucket = (severity.value, key)
        last_sent = self._last_sent.get(bucket, 0.0)

        if cooldown > 0 and (now - last_sent) < cooldown:
            logger.debug(
                "ops_alert_suppressed",
                severity=severity.value,
                event_type=event_type,
                dedupe_key=key,
                cooldown_s=cooldown,
            )
            return False

        self._last_sent[bucket] = now
        await self._sender(format_alert(severity, event_type, details))
        logger.info(
            "ops_alert_sent",
            severity=severity.value,
            event_type=event_type,
            dedupe_key=key,
        )
        return True

    async def info(
        self,
        event_type: str,
        details: str,
        *,
        dedupe_key: str | None = None,
        cooldown_s: float | None = None,
    ) -> bool:
        return await self.notify(
            AlertSeverity.INFO, event_type, details,
            dedupe_key=dedupe_key, cooldown_s=cooldown_s,
        )

    async def warning(
        self,
        event_type: str,
        details: str,
        *,
        dedupe_key: str | None = None,
        cooldown_s: float | None = None,
    ) -> bool:
        return await self.notify(
            AlertSeverity.WARNING, event_type, details,
            dedupe_key=dedupe_key, cooldown_s=cooldown_s,
        )

    async def critical(
        self,
        event_type: str,
        details: str,
        *,
        dedupe_key: str | None = None,
        cooldown_s: float | None = None,
    ) -> bool:
        return await self.notify(
            AlertSeverity.CRITICAL, event_type, details,
            dedupe_key=dedupe_key, cooldown_s=cooldown_s,
        )


def format_fill_notification(
    side: str,
    size: float,
    price: float,
    token_id: str,
    order_id: str,
    is_scoring: bool = False,
) -> str:
    """Format a fill notification message.

    Args:
        side: BUY or SELL
        size: Fill size in shares
        price: Fill price
        token_id: Token ID (first 16 chars will be shown)
        order_id: Order ID (first 16 chars will be shown)
        is_scoring: Whether the order was scoring for rewards

    Returns:
        Formatted message string
    """
    emoji = "🟢" if side.upper() == "BUY" else "🔴"
    dollar_value = size * price
    scoring_badge = " 💰" if is_scoring else ""
    return f"""{emoji} {side.upper()} FILLED{scoring_badge}
Shares: {size:.1f} @ ${price:.3f}
Value: ${dollar_value:.2f}
Token: {token_id[:16]}..."""


async def send_critical_alert(event_type: str, details: str) -> None:
    """Send a critical alert via Telegram."""
    await send_alert(event_type, details, AlertSeverity.CRITICAL)


async def send_warning_alert(event_type: str, details: str) -> None:
    """Send a warning alert via Telegram."""
    await send_alert(event_type, details, AlertSeverity.WARNING)


async def send_info_alert(event_type: str, details: str) -> None:
    """Send an informational alert via Telegram."""
    await send_alert(event_type, details, AlertSeverity.INFO)


def format_exit_notification(
    exit_type: str,
    token_id: str,
    price: float,
    size: float,
) -> str:
    """Format an exit signal notification.

    Args:
        exit_type: Exit type (e.g., "take_profit", "stop_loss")
        token_id: Token ID (first 16 chars will be shown)
        price: Current price
        size: Position size

    Returns:
        Formatted message string
    """
    return f"""🔴 EXIT SIGNAL: {exit_type}
Token: {token_id[:16]}...
Price: ${price:.2f}, Size: {size:.1f}"""

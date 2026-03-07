"""Telegram notification module for fills and exits."""

from __future__ import annotations

import asyncio
import os
import aiohttp
import structlog

logger = structlog.get_logger(__name__)

# Get token from env or use default from openclaw config
TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "8548123005:AAGpxJFaxbJeRH6CtMw2oGvRVWakwW-lm8U"
)
TELEGRAM_CHAT_ID = "7916400037"


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
    except asyncio.TimeoutError:
        logger.warning("telegram_send_timeout")
    except Exception as e:
        logger.warning("telegram_send_failed", error=str(e))


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

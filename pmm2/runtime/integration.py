"""Integration hooks for PMM-2.

These functions are called FROM main.py at appropriate points.
PMM-2 is opt-in via config flag pmm2.enabled.

Main.py integration points:
1. Startup: await maybe_init_pmm2(settings, db, bot_state)
2. Book WS delta: pmm2_on_book_delta(runtime, token_id, price, old_size, new_size)
3. Fill WS event: pmm2_on_fill(runtime, order_id, fill_size, fill_price)
4. Order live: pmm2_on_order_live(runtime, order_id, token_id, side, price, size, book_depth)
5. Order canceled: pmm2_on_order_canceled(runtime, order_id)

DO NOT modify main.py in this sprint — just provide the hooks.
"""

from __future__ import annotations

import structlog

from pmm2.config import load_pmm2_config
from pmm2.runtime.loops import PMM2Runtime
from pmm2.runtime.v1_bridge import V1Bridge

logger = structlog.get_logger(__name__)


async def maybe_init_pmm2(settings, db, bot_state) -> PMM2Runtime | None:
    """Initialize PMM-2 if enabled in config.

    Checks config for pmm2.enabled flag.
    If True, creates V1Bridge and PMM2Runtime and starts all loops.

    Args:
        settings: V1 settings object (has raw_config dict)
        db: Database instance
        bot_state: V1 bot state object (has order_manager, risk_limits, etc.)

    Returns:
        PMM2Runtime instance if enabled, None otherwise
    """
    try:
        # Load config
        if not hasattr(settings, "raw_config"):
            logger.warning("settings_missing_raw_config")
            return None

        config = load_pmm2_config(settings.raw_config)

        if not config.enabled:
            logger.info("pmm2_disabled_in_config")
            return None

        # Create V1 bridge
        order_manager = getattr(bot_state, "order_manager", None)
        risk_limits = getattr(bot_state, "risk_limits", None)
        if config.is_live and order_manager is None:
            raise RuntimeError("pmm2 live mode requires bot_state.order_manager")

        bridge = V1Bridge(
            order_manager=order_manager,
            risk_limits=risk_limits,
            shadow_mode=config.shadow_mode,
            controller_label=config.controller_label,
            stage_name=config.stage_name,
            live_capital_pct=config.live_capital_pct,
            strategy_label=config.controller_strategy,
        )

        # Create runtime
        runtime = PMM2Runtime(config, db, bridge)

        # Start all loops
        tasks = await runtime.start(bot_state, settings)

        logger.info(
            "pmm2_initialized",
            shadow=config.shadow_mode,
            controller=config.controller_label,
            stage=config.stage_name,
            live_pct=config.live_capital_pct,
            tasks=len(tasks),
        )

        return runtime

    except Exception as e:
        logger.error("pmm2_init_failed", error=str(e), exc_info=True)
        return None


def pmm2_on_book_delta(
    runtime: PMM2Runtime | None,
    token_id: str,
    price: float,
    old_size: float,
    new_size: float,
):
    """Forward book delta to PMM-2 queue estimator.

    Called from book WS handler in main.py.

    Args:
        runtime: PMM2Runtime instance (or None if not initialized)
        token_id: token ID
        price: price level that changed
        old_size: previous size
        new_size: new size
    """
    if runtime:
        runtime.on_book_delta(token_id, price, old_size, new_size)


def pmm2_on_fill(
    runtime: PMM2Runtime | None,
    order_id: str,
    fill_size: float,
    fill_price: float,
):
    """Forward fill event to PMM-2.

    Called from fill WS handler in main.py.

    Args:
        runtime: PMM2Runtime instance (or None)
        order_id: order ID that filled
        fill_size: size filled
        fill_price: fill price
    """
    if runtime:
        runtime.on_fill(order_id, fill_size, fill_price)


def pmm2_on_order_live(
    runtime: PMM2Runtime | None,
    order_id: str,
    token_id: str,
    side: str,
    price: float,
    size: float,
    book_depth: float,
):
    """Forward order-live event to PMM-2.

    Called when an order goes live (after placement confirmation).

    Args:
        runtime: PMM2Runtime instance (or None)
        order_id: order ID
        token_id: token ID
        side: BUY or SELL
        price: order price
        size: order size
        book_depth: visible size at this price level
    """
    if runtime:
        runtime.on_order_live(order_id, token_id, side, price, size, book_depth)


def pmm2_on_order_canceled(runtime: PMM2Runtime | None, order_id: str):
    """Forward order-canceled event to PMM-2.

    Called when an order is canceled (user action or exchange).

    Args:
        runtime: PMM2Runtime instance (or None)
        order_id: order ID
    """
    if runtime:
        runtime.on_order_canceled(order_id)

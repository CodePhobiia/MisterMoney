"""Periodic reconciliation — from §12.

- Open orders every 30s
- Positions/trades every 60s
- After reconnect: full reconciliation before resuming
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from pmm1.api.clob_private import ClobPrivateClient
from pmm1.api.data_api import DataApiClient
from pmm1.state.orders import OrderTracker
from pmm1.state.positions import PositionTracker

logger = structlog.get_logger(__name__)


class ReconciliationResult:
    """Result of a reconciliation cycle."""

    def __init__(self) -> None:
        self.order_mismatches: dict[str, Any] = {}
        self.position_mismatches: dict[str, Any] = {}
        self.success: bool = True
        self.timestamp: float = time.time()
        self.errors: list[str] = []


class Reconciler:
    """Periodically reconciles local state with exchange truth.

    Two reconciliation loops:
    1. Orders: every reconcile_orders_s (default 30s)
    2. Positions: every reconcile_positions_s (default 60s)

    After any reconnect (WS or API), a full reconciliation is triggered
    before resuming normal quoting.
    """

    def __init__(
        self,
        clob_client: ClobPrivateClient,
        data_client: DataApiClient,
        order_tracker: OrderTracker,
        position_tracker: PositionTracker,
        wallet_address: str = "",
        reconcile_orders_s: float = 30.0,
        reconcile_positions_s: float = 60.0,
    ) -> None:
        self._clob = clob_client
        self._data = data_client
        self._orders = order_tracker
        self._positions = position_tracker
        self._wallet = wallet_address
        self._reconcile_orders_s = reconcile_orders_s
        self._reconcile_positions_s = reconcile_positions_s

        self._last_order_reconcile: float = 0.0
        self._last_position_reconcile: float = 0.0
        self._is_running = False
        self._task: asyncio.Task | None = None
        self._reconcile_count: int = 0
        self._mismatch_count: int = 0

    async def reconcile_orders(self) -> ReconciliationResult:
        """Reconcile local order state with exchange open orders."""
        result = ReconciliationResult()

        try:
            exchange_orders = await self._clob.get_open_orders()
            exchange_dicts = [
                {
                    "orderID": o.order_id,
                    "id": o.id,
                    "asset_id": o.asset_id,
                    "side": o.side,
                    "price": o.price,
                    "originalSize": o.original_size,
                    "sizeMatched": o.size_matched,
                    "status": o.status,
                }
                for o in exchange_orders
            ]

            mismatches = self._orders.reconcile_with_exchange(exchange_dicts)
            result.order_mismatches = mismatches

            unknown = mismatches.get("unknown_on_exchange", [])
            missing = mismatches.get("missing_from_exchange", [])

            if unknown or missing:
                self._mismatch_count += 1
                logger.warning(
                    "order_reconciliation_mismatches",
                    unknown_count=len(unknown),
                    missing_count=len(missing),
                    total_mismatches=self._mismatch_count,
                )
            else:
                logger.debug(
                    "order_reconciliation_clean",
                    matched=mismatches.get("matched", 0),
                )

            self._last_order_reconcile = time.time()

        except Exception as e:
            result.success = False
            result.errors.append(f"order_reconcile_error: {e}")
            logger.error("order_reconciliation_failed", error=str(e))

        return result

    async def reconcile_positions(self) -> ReconciliationResult:
        """Reconcile local positions with exchange positions."""
        result = ReconciliationResult()

        if not self._wallet:
            logger.debug("position_reconcile_skipped_no_wallet")
            return result

        try:
            exchange_positions = await self._data.get_positions(self._wallet)
            exchange_dicts = [
                {
                    "asset": p.asset,
                    "conditionId": p.condition_id,
                    "size": p.size,
                    "outcome": p.outcome,
                    "avgPrice": p.avg_price,
                }
                for p in exchange_positions
                if p.size > 0
            ]

            mismatches = self._positions.reconcile_with_exchange(exchange_dicts)
            result.position_mismatches = mismatches

            mismatch_count = mismatches.get("count", 0)
            if mismatch_count > 0:
                self._mismatch_count += mismatch_count
                logger.warning(
                    "position_reconciliation_mismatches",
                    count=mismatch_count,
                )
            else:
                logger.debug("position_reconciliation_clean")

            self._last_position_reconcile = time.time()

        except Exception as e:
            result.success = False
            result.errors.append(f"position_reconcile_error: {e}")
            logger.error("position_reconciliation_failed", error=str(e))

        return result

    async def full_reconciliation(self) -> ReconciliationResult:
        """Full reconciliation — run both order and position checks.

        Called after reconnect before resuming normal operations.
        """
        logger.info("full_reconciliation_started")

        order_result = await self.reconcile_orders()
        position_result = await self.reconcile_positions()

        # Combine results
        combined = ReconciliationResult()
        combined.order_mismatches = order_result.order_mismatches
        combined.position_mismatches = position_result.position_mismatches
        combined.success = order_result.success and position_result.success
        combined.errors = order_result.errors + position_result.errors

        self._reconcile_count += 1

        logger.info(
            "full_reconciliation_complete",
            success=combined.success,
            errors=combined.errors,
            reconcile_count=self._reconcile_count,
        )

        return combined

    async def _reconcile_loop(self) -> None:
        """Main reconciliation loop."""
        logger.info(
            "reconcile_loop_started",
            orders_interval=self._reconcile_orders_s,
            positions_interval=self._reconcile_positions_s,
        )

        while self._is_running:
            now = time.time()

            # Order reconciliation
            if now - self._last_order_reconcile >= self._reconcile_orders_s:
                await self.reconcile_orders()

            # Position reconciliation
            if now - self._last_position_reconcile >= self._reconcile_positions_s:
                await self.reconcile_positions()

            # Sleep for the shorter interval
            await asyncio.sleep(min(self._reconcile_orders_s, self._reconcile_positions_s) / 2)

    def start(self) -> asyncio.Task:
        """Start the reconciliation loop."""
        self._is_running = True
        self._task = asyncio.create_task(self._reconcile_loop())
        return self._task

    async def stop(self) -> None:
        """Stop the reconciliation loop."""
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("reconcile_loop_stopped")

    def get_stats(self) -> dict[str, Any]:
        return {
            "reconcile_count": self._reconcile_count,
            "mismatch_count": self._mismatch_count,
            "seconds_since_order_reconcile": time.time() - self._last_order_reconcile if self._last_order_reconcile else float("inf"),
            "seconds_since_position_reconcile": time.time() - self._last_position_reconcile if self._last_position_reconcile else float("inf"),
        }

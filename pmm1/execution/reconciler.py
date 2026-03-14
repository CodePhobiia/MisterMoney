"""Periodic reconciliation — from §12.

- Open orders every 30s
- Positions/trades every 60s
- After reconnect: full reconciliation before resuming
"""

from __future__ import annotations

import asyncio
import inspect
import random
import time
from typing import TYPE_CHECKING, Any

import structlog

from pmm1.api.clob_private import ClobPrivateClient
from pmm1.api.data_api import DataApiClient
from pmm1.state.orders import OrderTracker
from pmm1.state.positions import PositionTracker

if TYPE_CHECKING:
    from pmm1.storage.spine import SpineEmitter

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
        spine_emitter: SpineEmitter | None = None,
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
        self._task: asyncio.Task[None] | None = None
        self._reconcile_count: int = 0
        self._order_mismatch_streak: int = 0
        self._position_mismatch_streak: int = 0
        self._total_mismatch_events: int = 0
        self._last_mismatch_at: float = 0.0
        self._last_mismatch_details: str = ""
        self._resume_epoch: int = 0
        self._resume_ready_epoch: int = -1
        self._resume_invalid_reason: str = "startup"
        self._last_resume_invalidated_at: float = time.time()
        self._last_successful_full_reconciliation_at: float = 0.0
        self._kill_switch = None  # Set via set_kill_switch()
        self._on_mismatch = None
        self._spine = spine_emitter

    def set_kill_switch(self, kill_switch: Any) -> None:
        """Set the kill switch for escalation on persistent mismatches."""
        self._kill_switch = kill_switch

    def set_on_mismatch(self, callback: Any) -> None:
        """Register an optional callback for mismatch events."""
        self._on_mismatch = callback

    @property
    def resume_token_valid(self) -> bool:
        return self._resume_ready_epoch == self._resume_epoch and self._resume_ready_epoch >= 0

    @property
    def resume_invalid_reason(self) -> str:
        return self._resume_invalid_reason

    def invalidate_resume_token(self, reason: str) -> None:
        """Invalidate quote resume until a clean full reconciliation succeeds."""
        normalized_reason = (reason or "unknown").strip() or "unknown"
        if self.resume_token_valid:
            self._resume_epoch += 1
        self._resume_invalid_reason = normalized_reason
        self._last_resume_invalidated_at = time.time()
        logger.info(
            "resume_token_invalidated",
            epoch=self._resume_epoch,
            reason=normalized_reason,
        )

    def _mark_resume_token_valid(self) -> None:
        self._resume_ready_epoch = self._resume_epoch
        self._resume_invalid_reason = ""
        self._last_successful_full_reconciliation_at = time.time()
        logger.info(
            "resume_token_valid",
            epoch=self._resume_epoch,
            last_successful_full_reconciliation_at=self._last_successful_full_reconciliation_at,
        )

    async def _emit_mismatch(
        self,
        *,
        kind: str,
        count: int,
        details: str,
        streak: int,
    ) -> None:
        """Notify ops hooks about a reconciliation mismatch."""
        self._last_mismatch_at = time.time()
        self._last_mismatch_details = details
        if not self._on_mismatch:
            return
        try:
            maybe_coro = self._on_mismatch(
                kind=kind,
                count=count,
                details=details,
                streak=streak,
            )
            if inspect.isawaitable(maybe_coro):
                await maybe_coro
        except Exception as callback_error:
            logger.warning("reconciliation_mismatch_callback_failed", error=str(callback_error))

    def _maybe_clear_kill_switch(self) -> None:
        """Clear reconciliation kill-switch state after clean cycles."""
        if (
            self._kill_switch
            and self._order_mismatch_streak == 0
            and self._position_mismatch_streak == 0
        ):
            self._kill_switch.report_reconciliation_clean()

    async def _emit_reconciliation_event(
        self,
        *,
        event_type: str,
        kind: str,
        success: bool,
        payload_json: dict[str, Any],
    ) -> None:
        if self._spine is None:
            return
        await self._spine.emit_event(
            event_type=event_type,
            strategy="ops",
            payload_json={
                "kind": kind,
                "success": success,
                **payload_json,
            },
        )

    async def reconcile_orders(self) -> ReconciliationResult:
        """Reconcile local order state with exchange open orders."""
        result = ReconciliationResult()

        try:
            exchange_orders = await self._clob.get_open_orders()
            exchange_dicts = [
                {
                    "orderID": o.order_id,
                    "id": o.id,
                    "market": o.market,
                    "asset_id": o.asset_id,
                    "side": o.side,
                    "price": o.price,
                    "originalSize": o.original_size,
                    "sizeMatched": o.size_matched,
                    "status": o.status,
                    "expiration": o.expiration,
                    "createdAt": o.created_at,
                }
                for o in exchange_orders
            ]

            mismatches = self._orders.reconcile_with_exchange(exchange_dicts)
            result.order_mismatches = mismatches

            unknown = mismatches.get("unknown_on_exchange", [])
            imported = mismatches.get("imported_from_exchange", [])
            missing = mismatches.get("missing_from_exchange", [])

            if unknown or missing:
                self._order_mismatch_streak += 1
                self._total_mismatch_events += 1
                details = f"orders: {len(unknown)} unknown, {len(missing)} missing"
                logger.warning(
                    "order_reconciliation_mismatches",
                    unknown_count=len(unknown),
                    imported_count=len(imported),
                    missing_count=len(missing),
                    streak=self._order_mismatch_streak,
                )
                # Escalate only after persistent mismatches and never on the
                # very first reconciliation window after startup/import, when
                # exchange truth may still lag briefly.
                if (
                    self._kill_switch
                    and self._last_order_reconcile > 0
                    and self._order_mismatch_streak >= 3
                ):
                    self._kill_switch.report_reconciliation_mismatch(details)
                await self._emit_mismatch(
                    kind="orders",
                    count=len(unknown) + len(missing),
                    details=details,
                    streak=self._order_mismatch_streak,
                )
                await self._emit_reconciliation_event(
                    event_type="position_mismatch_detected",
                    kind="orders",
                    success=result.success,
                    payload_json={
                        "unknown_count": len(unknown),
                        "imported_count": len(imported),
                        "missing_count": len(missing),
                        "streak": self._order_mismatch_streak,
                        "details": details,
                    },
                )
            else:
                self._order_mismatch_streak = 0
                self._maybe_clear_kill_switch()
                logger.debug(
                    "order_reconciliation_clean",
                    matched=mismatches.get("matched", 0),
                )
                await self._emit_reconciliation_event(
                    event_type="position_reconciled",
                    kind="orders",
                    success=result.success,
                    payload_json={
                        "matched": mismatches.get("matched", 0),
                        "imported_count": len(imported),
                    },
                )

            self._last_order_reconcile = time.time()

        except Exception as e:
            result.success = False
            result.errors.append(f"order_reconcile_error: {e}")
            logger.error("order_reconciliation_failed", error=str(e))
            self.invalidate_resume_token("order_reconciliation_failed")
            await self._emit_reconciliation_event(
                event_type="position_mismatch_detected",
                kind="orders",
                success=False,
                payload_json={"errors": list(result.errors)},
            )

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
                self._position_mismatch_streak += 1
                self._total_mismatch_events += 1
                details = f"positions: {mismatch_count} mismatches"
                logger.warning(
                    "position_reconciliation_mismatches",
                    count=mismatch_count,
                    streak=self._position_mismatch_streak,
                )
                if self._kill_switch:
                    self._kill_switch.report_reconciliation_mismatch(details)
                await self._emit_mismatch(
                    kind="positions",
                    count=mismatch_count,
                    details=details,
                    streak=self._position_mismatch_streak,
                )
                await self._emit_reconciliation_event(
                    event_type="position_mismatch_detected",
                    kind="positions",
                    success=result.success,
                    payload_json={
                        "count": mismatch_count,
                        "streak": self._position_mismatch_streak,
                        "details": details,
                    },
                )
            else:
                self._position_mismatch_streak = 0
                self._maybe_clear_kill_switch()
                logger.debug("position_reconciliation_clean")
                await self._emit_reconciliation_event(
                    event_type="position_reconciled",
                    kind="positions",
                    success=result.success,
                    payload_json={"count": 0},
                )

            self._last_position_reconcile = time.time()

        except Exception as e:
            result.success = False
            result.errors.append(f"position_reconcile_error: {e}")
            logger.error("position_reconciliation_failed", error=str(e))
            self.invalidate_resume_token("position_reconciliation_failed")
            await self._emit_reconciliation_event(
                event_type="position_mismatch_detected",
                kind="positions",
                success=False,
                payload_json={"errors": list(result.errors)},
            )

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
        order_unknown = list(combined.order_mismatches.get("unknown_on_exchange", []))
        order_missing = list(combined.order_mismatches.get("missing_from_exchange", []))
        position_mismatch_count = int(combined.position_mismatches.get("count", 0) or 0)
        if (
            combined.success
            and not combined.errors
            and not order_unknown
            and not order_missing
            and position_mismatch_count == 0
        ):
            self._mark_resume_token_valid()
        else:
            self.invalidate_resume_token("full_reconciliation_not_clean")
        await self._emit_reconciliation_event(
            event_type="position_reconciled" if combined.success else "position_mismatch_detected",
            kind="full",
            success=combined.success,
            payload_json={
                "order_mismatches": combined.order_mismatches,
                "position_mismatches": combined.position_mismatches,
                "errors": list(combined.errors),
                "reconcile_count": self._reconcile_count,
            },
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

            # S-H5: Add jitter to prevent thundering herd with multiple instances
            base_sleep = min(self._reconcile_orders_s, self._reconcile_positions_s) / 2
            jitter = random.uniform(0, base_sleep * 0.3)
            await asyncio.sleep(base_sleep + jitter)

    def start(self) -> asyncio.Task[None]:
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
        """Return reconciliation health for status checks."""
        now = time.time()
        return {
            "order_mismatch_streak": self._order_mismatch_streak,
            "position_mismatch_streak": self._position_mismatch_streak,
            "total_mismatch_events": self._total_mismatch_events,
            "last_mismatch_at": self._last_mismatch_at,
            "last_mismatch_details": self._last_mismatch_details,
            "seconds_since_order_reconcile": (
                now - self._last_order_reconcile if self._last_order_reconcile else float("inf")
            ),
            "seconds_since_position_reconcile": (
                now - self._last_position_reconcile
                if self._last_position_reconcile
                else float("inf")
            ),
            "last_successful_full_reconciliation_at": self._last_successful_full_reconciliation_at,
            "resume_token_valid": self.resume_token_valid,
            "resume_invalid_reason": self._resume_invalid_reason,
            "resume_epoch": self._resume_epoch,
            "resume_ready_epoch": self._resume_ready_epoch,
            "last_resume_invalidated_at": self._last_resume_invalidated_at,
            "reconcile_count": self._reconcile_count,
            "is_running": self._is_running,
        }

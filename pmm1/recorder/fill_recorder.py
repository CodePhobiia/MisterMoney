"""Fill recorder with markout tracking for PMM-2.

Records fill data to SQLite and schedules async markout calculations.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pmm1.storage.database import Database
    from pmm1.state.books import BookManager

logger = structlog.get_logger(__name__)


class FillRecorder:
    """Records fills and schedules markout tracking."""

    def __init__(self, db: Database, book_manager: BookManager) -> None:
        """Initialize fill recorder.

        Args:
            db: Database instance for persistence
            book_manager: BookManager for reading current book midpoints
        """
        self.db = db
        self.book_manager = book_manager
        self._on_failure = None

    def set_on_failure(self, callback) -> None:
        """Register an optional callback for fill recorder failures."""
        self._on_failure = callback

    async def _emit_failure(
        self,
        *,
        stage: str,
        token_id: str,
        order_id: str = "",
        error: Exception,
        delay_sec: int | None = None,
    ) -> None:
        """Notify runtime ops hooks about a recorder failure."""
        if not self._on_failure:
            return
        try:
            maybe_coro = self._on_failure(
                stage=stage,
                token_id=token_id,
                order_id=order_id,
                error=str(error),
                delay_sec=delay_sec,
            )
            if inspect.isawaitable(maybe_coro):
                await maybe_coro
        except Exception as callback_error:
            logger.warning("fill_recorder_failure_callback_failed", error=str(callback_error))

    async def record_fill(
        self,
        ts: datetime,
        condition_id: str,
        token_id: str,
        order_id: str,
        side: str,
        price: float,
        size: float,
        fee: float,
        mid_at_fill: float | None,
        is_scoring: bool,
        reward_eligible: bool,
    ) -> int:
        """Record a fill to the database.

        Args:
            ts: Fill timestamp
            condition_id: Market condition ID
            token_id: Token ID
            order_id: Order ID
            side: BUY or SELL
            price: Fill price
            size: Fill size in shares
            fee: Fee paid (if any)
            mid_at_fill: Book midpoint at fill time
            is_scoring: Whether order was scoring for rewards
            reward_eligible: Whether market is reward-eligible

        Returns:
            Fill record ID (database rowid)
        """
        try:
            dollar_value = size * price
            ts_iso = ts.isoformat()

            sql = """
                INSERT INTO fill_record (
                    ts, condition_id, token_id, order_id, side,
                    price, size, dollar_value, fee,
                    mid_at_fill, is_scoring, reward_eligible
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            
            await self.db.execute(
                sql,
                (
                    ts_iso,
                    condition_id,
                    token_id,
                    order_id,
                    side,
                    price,
                    size,
                    dollar_value,
                    fee,
                    mid_at_fill,
                    1 if is_scoring else 0,
                    1 if reward_eligible else 0,
                ),
            )

            fill_id = await self.db.get_last_insert_rowid()

            logger.debug(
                "fill_recorded",
                fill_id=fill_id,
                token_id=token_id[:16],
                order_id=order_id[:16],
                side=side,
                price=price,
                size=size,
                is_scoring=is_scoring,
            )

            # Schedule markout calculations
            asyncio.create_task(self._schedule_markout(fill_id, token_id, side, price, 1))
            asyncio.create_task(self._schedule_markout(fill_id, token_id, side, price, 5))
            asyncio.create_task(self._schedule_markout(fill_id, token_id, side, price, 30))

            return fill_id

        except Exception as e:
            logger.error(
                "fill_record_failed",
                token_id=token_id[:16] if token_id else "?",
                order_id=order_id[:16] if order_id else "?",
                error=str(e),
            )
            await self._emit_failure(
                stage="record_fill",
                token_id=token_id,
                order_id=order_id,
                error=e,
            )
            return 0

    async def _schedule_markout(
        self,
        fill_id: int,
        token_id: str,
        side: str,
        fill_price: float,
        delay_sec: int,
    ) -> None:
        """Schedule a markout calculation after a delay.

        Args:
            fill_id: Database rowid of the fill
            token_id: Token ID for reading book
            side: BUY or SELL (determines markout sign)
            fill_price: Original fill price
            delay_sec: Delay in seconds (1, 5, or 30)
        """
        try:
            # Wait for the specified delay
            await asyncio.sleep(delay_sec)

            # Read current book midpoint
            book = self.book_manager.get(token_id)
            if not book:
                logger.warning(
                    "markout_no_book",
                    fill_id=fill_id,
                    token_id=token_id[:16],
                    delay_sec=delay_sec,
                )
                return

            current_mid = book.get_midpoint()
            if current_mid is None:
                logger.warning(
                    "markout_no_mid",
                    fill_id=fill_id,
                    token_id=token_id[:16],
                    delay_sec=delay_sec,
                )
                return

            # Calculate markout (signed by side)
            # For BUY: positive markout = price went up (good)
            # For SELL: positive markout = price went down (good)
            if side == "BUY":
                markout = current_mid - fill_price
            else:  # SELL
                markout = fill_price - current_mid

            # Update database
            column_name = f"markout_{delay_sec}s"
            sql = f"UPDATE fill_record SET {column_name} = ? WHERE id = ?"
            await self.db.execute(sql, (markout, fill_id))

            logger.debug(
                "markout_recorded",
                fill_id=fill_id,
                token_id=token_id[:16],
                delay_sec=delay_sec,
                markout=round(markout, 4),
                side=side,
            )

        except Exception as e:
            logger.error(
                "markout_calculation_failed",
                fill_id=fill_id,
                token_id=token_id[:16] if token_id else "?",
                delay_sec=delay_sec,
                error=str(e),
            )
            await self._emit_failure(
                stage="markout",
                token_id=token_id,
                error=e,
                delay_sec=delay_sec,
            )

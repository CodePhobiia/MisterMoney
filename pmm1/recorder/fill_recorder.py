"""Fill recorder with markout tracking for PMM-2.

Records fill data to SQLite and schedules async markout calculations.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pmm1.state.books import BookManager
    from pmm1.storage.database import Database

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
        self._on_failure: Callable[..., Any] | None = None

    def set_on_failure(self, callback: Callable[..., Any] | None) -> None:
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
        fee: float | None,
        mid_at_fill: float | None,
        is_scoring: bool,
        reward_eligible: bool,
        *,
        exchange_trade_id: str = "",
        fill_identity: str = "",
        fee_known: bool = True,
        fee_source: str = "unknown",
        ingest_state: str = "applied",
        raw_event_json: dict[str, Any] | None = None,
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
                    exchange_trade_id, fill_identity,
                    price, size, dollar_value, fee, fee_known, fee_source, ingest_state,
                    raw_event_json, mid_at_fill, is_scoring, reward_eligible
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

            await self.db.execute(
                sql,
                (
                    ts_iso,
                    condition_id,
                    token_id,
                    order_id,
                    side,
                    exchange_trade_id,
                    fill_identity,
                    price,
                    size,
                    dollar_value,
                    fee,
                    1 if fee_known else 0,
                    fee_source,
                    ingest_state,
                    json.dumps(raw_event_json or {}, sort_keys=True),
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
                ingest_state=ingest_state,
            )

            # Schedule markout calculations only once the fill has been fully applied.
            if ingest_state == "applied":
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

    async def get_pending_fills(self, order_id: str | None = None) -> list[dict[str, Any]]:
        """Return pending fills that were persisted before order truth was available."""
        sql = """
            SELECT *
            FROM fill_record
            WHERE ingest_state != 'applied'
        """
        parameters: tuple[Any, ...] | None = None
        if order_id:
            sql += " AND order_id = ?"
            parameters = (order_id,)
        sql += " ORDER BY id ASC"
        return await self.db.fetch_all(sql, parameters)

    async def mark_fill_applied(self, fill_id: int) -> None:
        """Mark a pending fill as fully resolved and applied exactly once."""
        await self.db.execute(
            """
            UPDATE fill_record
            SET ingest_state = 'applied',
                resolved_at = ?
            WHERE id = ?
            """,
            (datetime.now(UTC).isoformat(), fill_id),
        )

    async def resolve_pending_fill(
        self,
        fill_id: int,
        *,
        condition_id: str,
        token_id: str,
        order_id: str,
        side: str,
        fee: float | None,
        fee_known: bool,
        fee_source: str,
        mid_at_fill: float | None,
        is_scoring: bool,
        reward_eligible: bool,
        raw_event_json: dict[str, Any] | None = None,
    ) -> None:
        """Promote a pending fill into the normal applied state."""
        resolved_at = datetime.now(UTC).isoformat()
        await self.db.execute(
            """
            UPDATE fill_record
            SET condition_id = ?,
                token_id = ?,
                order_id = ?,
                side = ?,
                fee = ?,
                fee_known = ?,
                fee_source = ?,
                mid_at_fill = ?,
                is_scoring = ?,
                reward_eligible = ?,
                ingest_state = 'applied',
                raw_event_json = ?,
                resolved_at = ?
            WHERE id = ?
            """,
            (
                condition_id,
                token_id,
                order_id,
                side,
                fee,
                1 if fee_known else 0,
                fee_source,
                mid_at_fill,
                1 if is_scoring else 0,
                1 if reward_eligible else 0,
                json.dumps(raw_event_json or {}, sort_keys=True),
                resolved_at,
                fill_id,
            ),
        )

        row = await self.db.fetch_one(
            "SELECT price, side, token_id FROM fill_record WHERE id = ?",
            (fill_id,),
        )
        if row is None:
            return

        fill_price = float(row.get("price", 0.0) or 0.0)
        fill_side = str(row.get("side", side) or side)
        fill_token = str(row.get("token_id", token_id) or token_id)
        if fill_price > 0.0 and fill_token:
            asyncio.create_task(self._schedule_markout(
                fill_id, fill_token, fill_side, fill_price, 1,
            ))
            asyncio.create_task(self._schedule_markout(
                fill_id, fill_token, fill_side, fill_price, 5,
            ))
            asyncio.create_task(self._schedule_markout(
                fill_id, fill_token, fill_side, fill_price, 30,
            ))

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

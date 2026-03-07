"""Book snapshot recorder for PMM-2.

Records periodic book snapshots for queue estimation replay.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pmm1.storage.database import Database
    from pmm1.state.books import BookManager
    from pmm1.strategy.universe import MarketMetadata

logger = structlog.get_logger(__name__)


class BookRecorder:
    """Records periodic book snapshots."""

    def __init__(self, db: Database) -> None:
        """Initialize book recorder.

        Args:
            db: Database instance for persistence
        """
        self.db = db

    async def snapshot_books(
        self,
        active_markets: dict[str, MarketMetadata],
        book_manager: BookManager,
    ) -> int:
        """Snapshot current book state for all active markets.

        Args:
            active_markets: Dict of condition_id -> MarketMetadata
            book_manager: BookManager with current book state

        Returns:
            Number of books successfully recorded
        """
        ts = datetime.now(timezone.utc).isoformat()
        recorded_count = 0

        for condition_id, market in active_markets.items():
            # Snapshot YES token
            if market.token_id_yes:
                success = await self._snapshot_token(
                    ts, condition_id, market.token_id_yes, book_manager
                )
                if success:
                    recorded_count += 1

            # Snapshot NO token
            if market.token_id_no:
                success = await self._snapshot_token(
                    ts, condition_id, market.token_id_no, book_manager
                )
                if success:
                    recorded_count += 1

        if recorded_count > 0:
            logger.debug("books_snapshotted", count=recorded_count)

        return recorded_count

    async def _snapshot_token(
        self,
        ts: str,
        condition_id: str,
        token_id: str,
        book_manager: BookManager,
    ) -> bool:
        """Snapshot a single token's book.

        Args:
            ts: ISO8601 timestamp
            condition_id: Market condition ID
            token_id: Token ID
            book_manager: BookManager with current book state

        Returns:
            True if successfully recorded, False otherwise
        """
        try:
            book = book_manager.get(token_id)
            if not book:
                return False

            # Get top-of-book data
            best_bid = book.get_best_bid()
            best_ask = book.get_best_ask()

            if not best_bid or not best_ask:
                # Skip if book is empty
                return False

            # Calculate depth within 5 levels
            bids_5 = book.get_bids(depth=5)
            asks_5 = book.get_asks(depth=5)
            bid_depth_5 = sum(level.size_float for level in bids_5)
            ask_depth_5 = sum(level.size_float for level in asks_5)

            # Calculate spread and mid
            spread_cents = book.get_spread_cents()
            mid = book.get_midpoint()

            # Insert into database
            sql = """
                INSERT INTO book_snapshot (
                    ts, condition_id, token_id,
                    best_bid, best_ask,
                    bid_depth_5, ask_depth_5,
                    spread_cents, mid
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

            await self.db.execute(
                sql,
                (
                    ts,
                    condition_id,
                    token_id,
                    best_bid.price_float,
                    best_ask.price_float,
                    bid_depth_5,
                    ask_depth_5,
                    spread_cents,
                    mid,
                ),
            )

            return True

        except Exception as e:
            logger.error(
                "book_snapshot_failed",
                token_id=token_id[:16] if token_id else "?",
                condition_id=condition_id[:16] if condition_id else "?",
                error=str(e),
            )
            return False

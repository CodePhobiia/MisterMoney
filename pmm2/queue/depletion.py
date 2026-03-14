"""Depletion rate calculator using historical book snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pmm1.storage.database import Database

logger = structlog.get_logger(__name__)


class DepletionCalculator:
    """Calculate queue depletion rates from book snapshot history."""

    def __init__(self) -> None:
        """Initialize depletion calculator."""
        self.default_rate = 1.0  # shares/sec default

    async def compute_from_snapshots(
        self, db: Database, token_id: str, lookback_hours: int = 4
    ) -> float:
        """Compute average depletion rate from recent book snapshots.

        Args:
            db: database connection
            token_id: token to analyze
            lookback_hours: how far back to look for snapshots

        Returns:
            Average depletion rate in shares/sec
        """
        # Query recent book snapshots for this token
        lookback_sec = lookback_hours * 3600
        current_time = __import__("time").time()
        cutoff_time = current_time - lookback_sec

        rows = await db.fetch_all(
            """
            SELECT timestamp, bid_depth_5, ask_depth_5
            FROM book_snapshot
            WHERE token_id = ?
              AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (token_id, cutoff_time),
        )

        if len(rows) < 2:
            logger.debug(
                "depletion_insufficient_snapshots",
                token_id=token_id,
                count=len(rows),
            )
            return self.default_rate

        # Calculate depletion rate from depth changes over time
        total_depletion = 0.0
        total_time = 0.0
        prev_timestamp: float | None = None
        prev_bid_depth: float | None = None
        prev_ask_depth: float | None = None

        for row in rows:
            timestamp = float(row["timestamp"])
            bid_depth = float(row["bid_depth_5"])
            ask_depth = float(row["ask_depth_5"])
            if prev_timestamp is not None:
                time_delta = timestamp - prev_timestamp
                if time_delta > 0:
                    # Measure net depletion (positive = queue consumed)
                    assert prev_bid_depth is not None
                    assert prev_ask_depth is not None
                    bid_depletion = max(prev_bid_depth - bid_depth, 0.0)
                    ask_depletion = max(prev_ask_depth - ask_depth, 0.0)
                    avg_depletion = (bid_depletion + ask_depletion) / 2.0

                    total_depletion += avg_depletion
                    total_time += time_delta

            prev_timestamp = timestamp
            prev_bid_depth = bid_depth
            prev_ask_depth = ask_depth

        if total_time <= 0:
            logger.debug(
                "depletion_zero_time",
                token_id=token_id,
            )
            return self.default_rate

        # Average depletion rate in shares/sec
        depletion_rate = total_depletion / total_time

        # Clamp to reasonable range (0.1 to 100 shares/sec)
        depletion_rate = max(0.1, min(depletion_rate, 100.0))

        logger.info(
            "depletion_rate_computed",
            token_id=token_id,
            rate=depletion_rate,
            snapshots=len(rows),
            lookback_hours=lookback_hours,
        )

        return depletion_rate

    async def refresh_all(
        self, db: Database, active_token_ids: list[str], hazard_calculator: Any
    ) -> None:
        """Batch update depletion rates for all active tokens.

        Args:
            db: database connection
            active_token_ids: list of token IDs to update
            hazard_calculator: FillHazard instance to update
        """
        if not active_token_ids:
            return

        logger.info("depletion_refresh_start", token_count=len(active_token_ids))

        for token_id in active_token_ids:
            try:
                rate = await self.compute_from_snapshots(db, token_id)
                hazard_calculator.update_depletion_rate(token_id, rate)
            except Exception as e:
                logger.error(
                    "depletion_refresh_failed",
                    token_id=token_id,
                    error=str(e),
                )

        logger.info("depletion_refresh_complete", token_count=len(active_token_ids))

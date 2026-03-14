"""Track and calibrate liquidity reward estimates vs actuals."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pmm1.storage.database import Database

logger = structlog.get_logger(__name__)


class RewardTracker:
    """Track and calibrate liquidity reward estimates vs actuals.

    Compares our internal reward EV estimates against venue truth:
    - /order-scoring tells us if orders are actually scoring
    - /rebates/current tells us realized daily rebates
    """

    def __init__(self, db: Database, half_life_days: float = 7.0):
        """Initialize reward tracker.

        Args:
            db: Database instance
            half_life_days: Half-life for EMA correction factor
        """
        self.db = db
        self.half_life_days = half_life_days
        # EMA correction factor (initialized at 1.0 = trust our estimates)
        self.reward_correction: dict[str, float] = {}  # condition_id → factor

    async def record_scoring_snapshot(
        self,
        condition_id: str,
        orders_total: int,
        orders_scoring: int
    ) -> None:
        """Record how many of our orders are scoring in this market.

        scoring_uptime = orders_scoring / orders_total

        Args:
            condition_id: Market condition ID
            orders_total: Total number of our orders
            orders_scoring: Number of orders currently scoring
        """
        if orders_total <= 0:
            return

        scoring_uptime = orders_scoring / orders_total

        await self.db.execute(
            """
            INSERT INTO scoring_history (ts, order_id, condition_id, is_scoring)
            VALUES (datetime('now'), 'SNAPSHOT', ?, ?)
            """,
            (condition_id, int(scoring_uptime * 100))  # Store as percentage
        )

        logger.debug(
            "scoring_snapshot_recorded",
            condition_id=condition_id,
            orders_total=orders_total,
            orders_scoring=orders_scoring,
            uptime_pct=scoring_uptime * 100
        )

    async def record_reward_actual(
        self,
        date: str,
        condition_id: str,
        realized_usdc: float,
        estimated_usdc: float
    ) -> None:
        """Record actual vs estimated reward for a market-day.

        Writes to reward_actual table.
        capture_efficiency = realized / estimated

        Args:
            date: Date string (YYYY-MM-DD)
            condition_id: Market condition ID
            realized_usdc: Actual reward received
            estimated_usdc: Our estimated reward
        """
        capture_efficiency = (
            realized_usdc / estimated_usdc if estimated_usdc > 0 else 0.0
        )

        await self.db.execute(
            """
            INSERT OR REPLACE INTO reward_actual
            (date, condition_id, realized_liq_reward_usdc, est_liq_reward_usdc, capture_efficiency)
            VALUES (?, ?, ?, ?, ?)
            """,
            (date, condition_id, realized_usdc, estimated_usdc, capture_efficiency)
        )

        logger.info(
            "reward_actual_recorded",
            date=date,
            condition_id=condition_id,
            realized=realized_usdc,
            estimated=estimated_usdc,
            efficiency=capture_efficiency
        )

    async def update_correction_factors(self) -> None:
        """Recompute correction factors from recent actuals.

        Uses exponential weighted average with half_life_days.
        correction = EMA(realized / estimated)

        If correction < 0.5 for 3 consecutive epochs → alert
        """
        # Fetch recent reward actuals (last 30 days, grouped by condition_id)
        rows = await self.db.fetch_all(
            """
            SELECT
                condition_id,
                AVG(capture_efficiency) as avg_efficiency,
                COUNT(*) as data_points,
                MIN(date) as first_date,
                MAX(date) as last_date
            FROM reward_actual
            WHERE date >= date('now', '-30 days')
            GROUP BY condition_id
            HAVING data_points >= 3
            """
        )

        # Compute decay factor for EMA
        # alpha = 1 - exp(-ln(2) / half_life)
        alpha = 1.0 - math.exp(-math.log(2) / self.half_life_days)

        for row in rows:
            condition_id = row["condition_id"]
            avg_efficiency = row["avg_efficiency"]
            data_points = row["data_points"]

            # Update correction factor using EMA
            current_correction = self.reward_correction.get(condition_id, 1.0)
            new_correction = (
                alpha * avg_efficiency + (1 - alpha) * current_correction
            )

            self.reward_correction[condition_id] = new_correction

            logger.info(
                "reward_correction_updated",
                condition_id=condition_id,
                avg_efficiency=avg_efficiency,
                data_points=data_points,
                old_correction=current_correction,
                new_correction=new_correction
            )

            # Alert if correction is persistently low
            if new_correction < 0.5 and data_points >= 3:
                logger.warning(
                    "low_reward_capture_detected",
                    condition_id=condition_id,
                    correction=new_correction,
                    data_points=data_points,
                    message=f"Reward capture efficiency below 50% for {condition_id}"
                )

    def get_correction(self, condition_id: str) -> float:
        """Get current correction factor for reward estimates.

        Args:
            condition_id: Market condition ID

        Returns:
            Correction factor (1.0 = trust estimates fully)
        """
        return self.reward_correction.get(condition_id, 1.0)

"""Track maker rebate estimates vs actuals."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pmm1.storage.database import Database

logger = structlog.get_logger(__name__)


class RebateTracker:
    """Track maker rebate estimates vs actuals.

    Similar structure to RewardTracker but for fee rebates.
    Only relevant for fee-enabled markets.
    """

    def __init__(self, db: Database, half_life_days: float = 14.0):
        """Initialize rebate tracker.

        Args:
            db: Database instance
            half_life_days: Half-life for EMA correction factor
        """
        self.db = db
        self.half_life_days = half_life_days
        self.rebate_correction: dict[str, float] = {}  # condition_id → factor

    async def record_rebate_actual(
        self,
        date: str,
        condition_id: str,
        realized_usdc: float,
        estimated_usdc: float
    ) -> None:
        """Record actual vs estimated rebate.

        Args:
            date: Date string (YYYY-MM-DD)
            condition_id: Market condition ID
            realized_usdc: Actual rebate received
            estimated_usdc: Our estimated rebate
        """
        capture_efficiency = (
            realized_usdc / estimated_usdc if estimated_usdc > 0 else 0.0
        )

        await self.db.execute(
            """
            INSERT OR REPLACE INTO rebate_actual
            (date, condition_id, realized_rebate_usdc, est_rebate_usdc, capture_efficiency)
            VALUES (?, ?, ?, ?, ?)
            """,
            (date, condition_id, realized_usdc, estimated_usdc, capture_efficiency)
        )

        logger.info(
            "rebate_actual_recorded",
            date=date,
            condition_id=condition_id,
            realized=realized_usdc,
            estimated=estimated_usdc,
            efficiency=capture_efficiency
        )

    async def fetch_and_record_daily(
        self,
        maker_address: str,
        rewards_client: Any,
        date: str
    ) -> None:
        """Pull realized rebates from /rebates/current and record.

        Compare against our estimates from scorer.

        Args:
            maker_address: Maker wallet address
            rewards_client: RewardsClient instance from pmm1.api.rewards
            date: Date string (YYYY-MM-DD)
        """
        try:
            # Fetch rebates from API
            rebates = await rewards_client.fetch_rebates()

            if not rebates:
                logger.debug("no_rebates_fetched", date=date)
                return

            # Fetch our estimates from market_score table for this date
            estimates = await self.db.fetch_all(
                """
                SELECT
                    condition_id,
                    SUM(rebate_ev_bps * target_capital_usdc / 10000.0) as est_rebate_usdc
                FROM market_score
                WHERE DATE(ts) = ?
                GROUP BY condition_id
                """,
                (date,)
            )

            # Build estimate lookup
            estimate_map = {
                row["condition_id"]: row["est_rebate_usdc"]
                for row in estimates
            }

            # Process each rebate record
            for rebate_data in rebates:
                condition_id = rebate_data.get("condition_id")
                realized_usdc = rebate_data.get("rebate_usdc", 0.0)

                if not condition_id:
                    continue

                estimated_usdc = estimate_map.get(condition_id, 0.0)

                await self.record_rebate_actual(
                    date, condition_id, realized_usdc, estimated_usdc
                )

            logger.info(
                "rebates_fetched_and_recorded",
                date=date,
                rebate_count=len(rebates)
            )

        except Exception as e:
            logger.error(
                "rebate_fetch_failed",
                date=date,
                error=str(e)
            )

    async def update_correction_factors(self) -> None:
        """Recompute correction factors from recent actuals.

        Uses exponential weighted average with half_life_days.
        """
        # Fetch recent rebate actuals (last 30 days, grouped by condition_id)
        rows = await self.db.fetch_all(
            """
            SELECT
                condition_id,
                AVG(capture_efficiency) as avg_efficiency,
                COUNT(*) as data_points,
                MIN(date) as first_date,
                MAX(date) as last_date
            FROM rebate_actual
            WHERE date >= date('now', '-30 days')
            GROUP BY condition_id
            HAVING data_points >= 3
            """
        )

        # Compute decay factor for EMA
        alpha = 1.0 - math.exp(-math.log(2) / self.half_life_days)

        for row in rows:
            condition_id = row["condition_id"]
            avg_efficiency = row["avg_efficiency"]

            # Update correction factor using EMA
            current_correction = self.rebate_correction.get(condition_id, 1.0)
            new_correction = (
                alpha * avg_efficiency + (1 - alpha) * current_correction
            )

            self.rebate_correction[condition_id] = new_correction

            logger.info(
                "rebate_correction_updated",
                condition_id=condition_id,
                avg_efficiency=avg_efficiency,
                new_correction=new_correction
            )

    def get_correction(self, condition_id: str) -> float:
        """Get current correction factor for rebate estimates.

        Args:
            condition_id: Market condition ID

        Returns:
            Correction factor (1.0 = trust estimates fully)
        """
        return self.rebate_correction.get(condition_id, 1.0)

"""Nightly calibration — fits models from accumulated data."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class NightlyCalibrator:
    """Runs calibration jobs daily at 03:00 UTC."""

    def __init__(
        self, db: Any, fill_hazard: Any,
        fill_calibrator: Any,
        toxicity_fitter: Any = None,
    ) -> None:
        self.db = db
        self.fill_hazard = fill_hazard
        self.fill_calibrator = fill_calibrator
        self.toxicity_fitter = toxicity_fitter
        self._last_run: datetime | None = None
        self._is_stale = True  # Start stale

    @property
    def is_stale(self) -> bool:
        """True if calibration hasn't run in 48+ hours."""
        if self._last_run is None:
            return True
        age_hours = (
            datetime.now(UTC) - self._last_run
        ).total_seconds() / 3600
        return age_hours > 48

    async def run_calibration(self) -> dict[str, object]:
        """Run all calibration jobs. Returns summary."""
        results: dict[str, object] = {}

        # 1. Fill calibration
        try:
            corrections = await self.fill_calibrator.compute_calibration(
                self.fill_hazard
            )
            if corrections:
                await self.fill_calibrator.apply_calibration(
                    self.fill_hazard, corrections
                )
                results["fill_calibration"] = corrections
            else:
                results["fill_calibration"] = "no_data"
        except Exception as e:
            logger.error("fill_calibration_failed", error=str(e))
            results["fill_calibration"] = f"error: {e}"

        # 2. Toxicity calibration (if fitter available)
        if self.toxicity_fitter:
            try:
                tox_result = await self.toxicity_fitter.fit()
                results["toxicity_calibration"] = tox_result
            except Exception as e:
                logger.error("toxicity_calibration_failed", error=str(e))
                results["toxicity_calibration"] = f"error: {e}"

        self._last_run = datetime.now(UTC)
        self._is_stale = False
        logger.info("nightly_calibration_complete", results=results)
        return results

    async def calibration_loop(self, run_hour_utc: int = 3) -> None:
        """Background loop — runs calibration at specified UTC hour daily."""
        while True:
            now = datetime.now(UTC)
            # Calculate seconds until next run
            target = now.replace(
                hour=run_hour_utc, minute=0, second=0, microsecond=0
            )
            if target <= now:
                # Already past today's target, schedule for tomorrow
                target = target.replace(day=target.day + 1)
            wait_seconds = (target - now).total_seconds()

            logger.info(
                "calibration_next_run",
                wait_seconds=wait_seconds,
                target=target.isoformat(),
            )

            await asyncio.sleep(wait_seconds)

            try:
                await self.run_calibration()
            except Exception as e:
                logger.error("calibration_loop_error", error=str(e))

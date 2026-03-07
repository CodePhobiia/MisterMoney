"""Top-level calibration orchestrator."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from pmm2.calibration.attribution import AttributionEngine
from pmm2.calibration.fill_calibrator import FillCalibrator
from pmm2.calibration.rebate_tracker import RebateTracker
from pmm2.calibration.reward_tracker import RewardTracker
from pmm2.calibration.toxicity_fitter import ToxicityFitter

if TYPE_CHECKING:
    from pmm1.storage.database import Database

logger = structlog.get_logger(__name__)


class CalibrationRunner:
    """Top-level calibration orchestrator.
    
    Runs daily calibration cycle:
    1. Pull realized rewards/rebates
    2. Update correction factors
    3. Calibrate fill probabilities
    4. Fit toxicity weights (if enough data)
    5. Generate and send attribution report
    """
    
    def __init__(
        self, 
        db: Database, 
        fill_hazard, 
        maker_address: str,
        rewards_client=None
    ):
        """Initialize calibration runner.
        
        Args:
            db: Database instance
            fill_hazard: FillHazard instance
            maker_address: Maker wallet address
            rewards_client: Optional RewardsClient instance
        """
        self.reward_tracker = RewardTracker(db)
        self.rebate_tracker = RebateTracker(db)
        self.fill_calibrator = FillCalibrator(db)
        self.toxicity_fitter = ToxicityFitter(db)
        self.attribution = AttributionEngine(db)
        self.fill_hazard = fill_hazard
        self.maker_address = maker_address
        self.rewards_client = rewards_client
    
    async def run_daily_calibration(self, date: str):
        """Run full daily calibration cycle.
        
        Args:
            date: Date string (YYYY-MM-DD)
        """
        logger.info("calibration_daily_start", date=date)
        
        try:
            # 1. Pull and record reward/rebate actuals
            if self.rewards_client:
                await self.rebate_tracker.fetch_and_record_daily(
                    self.maker_address, self.rewards_client, date
                )
            
            # 2. Update correction factors
            await self.reward_tracker.update_correction_factors()
            await self.rebate_tracker.update_correction_factors()
            
            # 3. Calibrate fill probabilities
            corrections = await self.fill_calibrator.compute_calibration(
                self.fill_hazard
            )
            if corrections:
                await self.fill_calibrator.apply_calibration(
                    self.fill_hazard, corrections
                )
                logger.info("fill_calibration_applied", **corrections)
            
            # 4. Fit toxicity weights
            weights = await self.toxicity_fitter.fit()
            if weights:
                logger.info(
                    "toxicity_weights_fitted",
                    w_1s=weights[0],
                    w_5s=weights[1],
                    w_30s=weights[2]
                )
            
            # 5. Attribution report
            await self.attribution.send_daily_report(date)
            
            logger.info("calibration_daily_complete", date=date)
            
        except Exception as e:
            logger.error(
                "calibration_daily_failed",
                date=date,
                error=str(e),
                exc_info=True
            )
            raise

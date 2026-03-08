"""Calibrate fill probability estimates against actual fill rates."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pmm1.storage.database import Database

logger = structlog.get_logger(__name__)


class FillCalibrator:
    """Calibrate fill probability estimates against actual fill rates.
    
    Compares predicted P^fill(H_Q) vs observed fill rate per price level.
    Adjusts FillHazard parameters (lambda, kappa) via EMA.
    """
    
    def __init__(self, db: Database, lookback_hours: int = 168):  # 7 days
        """Initialize fill calibrator.
        
        Args:
            db: Database instance
            lookback_hours: Hours of history to use for calibration
        """
        self.db = db
        self.lookback_hours = lookback_hours
    
    async def compute_actual_fill_rates(self) -> dict[str, dict]:
        """Compute actual fill rates from fill_record + queue_state history.
        
        For each price level we've quoted:
        - Count orders placed
        - Count orders filled
        - actual_rate = fills / placed
        
        Group by (condition_id, price_bucket).
        Returns {condition_id: {price_bucket: {placed: N, filled: N, rate: float}}}
        """
        # Query all orders from queue_state (placed orders)
        placed_orders = await self.db.fetch_all(
            """
            SELECT 
                condition_id,
                token_id,
                ROUND(price, 2) as price_bucket,
                COUNT(*) as placed_count
            FROM queue_state
            WHERE last_update >= datetime('now', ? || ' hours')
            GROUP BY condition_id, token_id, price_bucket
            """,
            (f"-{self.lookback_hours}",)
        )
        
        # Query all fills from fill_record
        filled_orders = await self.db.fetch_all(
            """
            SELECT 
                condition_id,
                token_id,
                ROUND(price, 2) as price_bucket,
                COUNT(*) as filled_count
            FROM fill_record
            WHERE ts >= datetime('now', ? || ' hours')
            GROUP BY condition_id, token_id, price_bucket
            """,
            (f"-{self.lookback_hours}",)
        )
        
        # Build fill rate map
        result: dict[str, dict] = {}
        
        # Index filled orders
        filled_map = {}
        for row in filled_orders:
            key = (row["condition_id"], row["token_id"], row["price_bucket"])
            filled_map[key] = row["filled_count"]
        
        # Compute fill rates
        for row in placed_orders:
            condition_id = row["condition_id"]
            token_id = row["token_id"]
            price_bucket = row["price_bucket"]
            placed = row["placed_count"]
            
            key = (condition_id, token_id, price_bucket)
            filled = filled_map.get(key, 0)
            
            fill_rate = filled / placed if placed > 0 else 0.0
            
            if condition_id not in result:
                result[condition_id] = {}
            
            result[condition_id][price_bucket] = {
                "placed": placed,
                "filled": filled,
                "rate": fill_rate
            }
        
        logger.debug(
            "fill_rates_computed",
            markets=len(result),
            lookback_hours=self.lookback_hours
        )
        
        return result
    
    async def compute_calibration(self, fill_hazard) -> dict[str, float]:
        """Compare predicted vs actual and compute parameter adjustments.
        
        Returns suggested parameter updates:
        {
            'lambda_correction': 1.05,  # we're underestimating fills by 5%
            'kappa_correction': 0.98,
        }
        
        Uses Brier score for calibration quality.
        
        Args:
            fill_hazard: FillHazard instance
            
        Returns:
            Dictionary of correction factors
        """
        fill_rates = await self.compute_actual_fill_rates()
        
        if not fill_rates:
            logger.debug("no_fill_data_for_calibration")
            return {}
        
        # Aggregate actual vs predicted fill probabilities
        total_samples = 0
        sum_actual = 0.0
        sum_predicted = 0.0
        brier_sum = 0.0
        
        for condition_id, price_buckets in fill_rates.items():
            for price_bucket, stats in price_buckets.items():
                actual_rate = stats["rate"]
                placed = stats["placed"]
                
                # Compute predicted fill probability from hazard model
                # for this price level using a 30s horizon baseline
                if fill_hazard and hasattr(fill_hazard, 'fill_probability'):
                    # Use condition_id to look up depletion rate if available
                    _dr = fill_hazard.default_depletion_rate
                    if hasattr(fill_hazard, 'depletion_rates') and fill_hazard.depletion_rates:
                        # Try to find a depletion rate for any token in this market
                        for _tid, _rate in fill_hazard.depletion_rates.items():
                            if _rate > 0:
                                _dr = _rate
                                break
                    predicted_rate = fill_hazard.fill_probability(
                        queue_ahead=0.0,  # Assume front of queue for baseline
                        order_size=1.0,
                        horizon_sec=30.0,
                        depletion_rate=_dr,
                    )
                    predicted_rate = max(predicted_rate, 0.001)  # Avoid division by zero
                else:
                    predicted_rate = 0.5  # True fallback only if no hazard model
                
                total_samples += placed
                sum_actual += actual_rate * placed
                sum_predicted += predicted_rate * placed
                brier_sum += ((predicted_rate - actual_rate) ** 2) * placed
        
        if total_samples == 0:
            return {}
        
        # Compute average actual vs predicted
        avg_actual = sum_actual / total_samples
        avg_predicted = sum_predicted / total_samples
        brier_score = brier_sum / total_samples
        
        # Compute correction factors
        # If we're underfilling (avg_actual > avg_predicted), increase lambda
        lambda_correction = avg_actual / avg_predicted if avg_predicted > 0 else 1.0
        
        # Clamp corrections to reasonable range [0.8, 1.2]
        lambda_correction = max(0.8, min(1.2, lambda_correction))
        
        logger.info(
            "fill_calibration_computed",
            total_samples=total_samples,
            avg_actual=avg_actual,
            avg_predicted=avg_predicted,
            brier_score=brier_score,
            lambda_correction=lambda_correction
        )
        
        return {
            "lambda_correction": lambda_correction,
            "kappa_correction": 1.0,  # Keep kappa stable for now
        }
    
    async def apply_calibration(self, fill_hazard, corrections: dict):
        """Apply calibration corrections to FillHazard parameters.
        
        Args:
            fill_hazard: FillHazard instance to update
            corrections: Dictionary of correction factors
        """
        lambda_correction = corrections.get("lambda_correction", 1.0)
        kappa_correction = corrections.get("kappa_correction", 1.0)
        
        # Apply corrections to FillHazard parameters
        # Note: FillHazard uses kappa and rho, not lambda
        # We'll adjust the default_depletion_rate instead
        if hasattr(fill_hazard, "default_depletion_rate"):
            fill_hazard.default_depletion_rate *= lambda_correction
        
        if hasattr(fill_hazard, "kappa"):
            fill_hazard.kappa *= kappa_correction
        
        logger.info(
            "fill_calibration_applied",
            lambda_correction=lambda_correction,
            kappa_correction=kappa_correction,
            new_kappa=getattr(fill_hazard, "kappa", None),
            new_depletion_rate=getattr(fill_hazard, "default_depletion_rate", None)
        )

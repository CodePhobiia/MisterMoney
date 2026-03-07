"""
V3 Shadow Metrics
Tracks prediction quality (Brier scores) and latency distributions
"""

from datetime import datetime
from typing import Dict, List
import statistics
import structlog

from v3.evidence.db import Database

log = structlog.get_logger()


class BrierScoreTracker:
    """Tracks prediction quality on resolved markets"""
    
    def __init__(self, db: Database):
        """
        Initialize Brier score tracker
        
        Args:
            db: Database instance
        """
        self.db = db
    
    async def record_prediction(
        self, 
        condition_id: str, 
        route: str,
        p_predicted: float, 
        timestamp: datetime
    ) -> None:
        """
        Store prediction for later scoring when market resolves
        
        Args:
            condition_id: Market condition ID
            route: Route that generated prediction
            p_predicted: Predicted probability
            timestamp: When prediction was made
        """
        if self.db.pool is None:
            log.error("database_not_connected")
            return
        
        try:
            await self.db.pool.execute(
                """
                INSERT INTO v3_predictions 
                    (condition_id, route, p_predicted, predicted_at)
                VALUES ($1, $2, $3, $4)
                """,
                condition_id, route, p_predicted, timestamp
            )
            
            log.debug("prediction_recorded", 
                     condition_id=condition_id,
                     route=route,
                     p_predicted=p_predicted)
        except Exception as e:
            log.error("failed_to_record_prediction", 
                     condition_id=condition_id,
                     error=str(e))
    
    async def score_resolved(
        self, 
        condition_id: str, 
        actual_outcome: float
    ) -> dict:
        """
        Score all predictions for a resolved market
        
        Brier score = (p_predicted - actual_outcome)^2
        Lower is better (0 = perfect, 1 = worst)
        
        Args:
            condition_id: Market condition ID
            actual_outcome: Actual outcome (0.0 or 1.0)
            
        Returns:
            Dict with {brier_score, route, prediction_count, p_predicted, actual}
        """
        if self.db.pool is None:
            log.error("database_not_connected")
            return {}
        
        try:
            # Get all predictions for this market
            rows = await self.db.pool.fetch(
                """
                SELECT id, route, p_predicted, predicted_at
                FROM v3_predictions
                WHERE condition_id = $1
                  AND actual_outcome IS NULL
                """,
                condition_id
            )
            
            if not rows:
                log.warning("no_predictions_to_score", 
                           condition_id=condition_id)
                return {
                    "condition_id": condition_id,
                    "prediction_count": 0,
                }
            
            # Calculate Brier scores
            results = []
            
            for row in rows:
                p_predicted = row['p_predicted']
                brier = (p_predicted - actual_outcome) ** 2
                
                # Update record with score
                await self.db.pool.execute(
                    """
                    UPDATE v3_predictions
                    SET actual_outcome = $1,
                        brier_score = $2,
                        scored_at = $3
                    WHERE id = $4
                    """,
                    actual_outcome, brier, datetime.utcnow(), row['id']
                )
                
                results.append({
                    "route": row['route'],
                    "p_predicted": p_predicted,
                    "brier_score": brier,
                })
            
            # Calculate average Brier score
            avg_brier = statistics.mean([r['brier_score'] for r in results])
            
            log.info("market_scored", 
                    condition_id=condition_id,
                    prediction_count=len(results),
                    avg_brier_score=avg_brier,
                    actual_outcome=actual_outcome)
            
            return {
                "condition_id": condition_id,
                "prediction_count": len(results),
                "avg_brier_score": avg_brier,
                "actual_outcome": actual_outcome,
                "predictions": results,
            }
        
        except Exception as e:
            log.error("failed_to_score_market", 
                     condition_id=condition_id,
                     error=str(e))
            return {}
    
    async def get_route_summary(self) -> dict:
        """
        Per-route Brier scores and prediction counts
        
        Returns:
            Dict like:
            {
                "simple": {"brier": 0.18, "n": 42},
                "numeric": {"brier": 0.12, "n": 15},
                "rule": {"brier": 0.22, "n": 8},
                "dossier": {"brier": 0.15, "n": 3},
            }
        """
        if self.db.pool is None:
            log.error("database_not_connected")
            return {}
        
        try:
            rows = await self.db.pool.fetch(
                """
                SELECT route,
                       COUNT(*) as count,
                       AVG(brier_score) as avg_brier
                FROM v3_predictions
                WHERE brier_score IS NOT NULL
                GROUP BY route
                """
            )
            
            summary = {}
            
            for row in rows:
                summary[row['route']] = {
                    "brier": round(row['avg_brier'], 4) if row['avg_brier'] else None,
                    "n": row['count'],
                }
            
            log.info("route_summary_generated", 
                    routes=list(summary.keys()))
            
            return summary
        
        except Exception as e:
            log.error("failed_to_get_route_summary", error=str(e))
            return {}
    
    async def get_recent_predictions(self, days: int = 7) -> List[dict]:
        """
        Get recent predictions for analysis
        
        Args:
            days: Number of days to look back
            
        Returns:
            List of prediction dicts
        """
        if self.db.pool is None:
            log.error("database_not_connected")
            return []
        
        try:
            rows = await self.db.pool.fetch(
                """
                SELECT condition_id, route, p_predicted, predicted_at,
                       actual_outcome, brier_score, scored_at
                FROM v3_predictions
                WHERE predicted_at > NOW() - INTERVAL '%s days'
                ORDER BY predicted_at DESC
                """,
                days
            )
            
            return [dict(row) for row in rows]
        
        except Exception as e:
            log.error("failed_to_get_recent_predictions", error=str(e))
            return []


class LatencyTracker:
    """Tracks per-route, per-provider latency distributions"""
    
    def __init__(self):
        """Initialize latency tracker with in-memory storage"""
        # Dict[route][provider] -> List[latency_ms]
        self.latencies: Dict[str, Dict[str, List[float]]] = {}
    
    def record(self, route: str, provider: str, latency_ms: float) -> None:
        """
        Record latency observation
        
        Args:
            route: Route name (numeric/simple/rule/dossier)
            provider: Provider name (sonnet/opus/gpt5/gemini)
            latency_ms: Latency in milliseconds
        """
        if route not in self.latencies:
            self.latencies[route] = {}
        
        if provider not in self.latencies[route]:
            self.latencies[route][provider] = []
        
        self.latencies[route][provider].append(latency_ms)
        
        log.debug("latency_recorded", 
                 route=route, 
                 provider=provider, 
                 latency_ms=latency_ms)
    
    def get_summary(self) -> dict:
        """
        Returns p50, p95, p99 per route and provider
        
        Returns:
            Dict like:
            {
                "simple": {
                    "sonnet": {"p50": 12000, "p95": 18000, "p99": 22000, "n": 42},
                    "gpt5": {"p50": 15000, "p95": 20000, "p99": 25000, "n": 38},
                },
                "numeric": {
                    "sonnet": {"p50": 500, "p95": 800, "p99": 1200, "n": 15},
                },
                ...
            }
        """
        summary = {}
        
        for route, providers in self.latencies.items():
            summary[route] = {}
            
            for provider, measurements in providers.items():
                if not measurements:
                    continue
                
                sorted_measurements = sorted(measurements)
                n = len(sorted_measurements)
                
                summary[route][provider] = {
                    "p50": self._percentile(sorted_measurements, 50),
                    "p95": self._percentile(sorted_measurements, 95),
                    "p99": self._percentile(sorted_measurements, 99),
                    "n": n,
                }
        
        return summary
    
    def _percentile(self, sorted_data: List[float], percentile: int) -> float:
        """
        Calculate percentile from sorted data
        
        Args:
            sorted_data: Sorted list of values
            percentile: Percentile to calculate (0-100)
            
        Returns:
            Percentile value
        """
        if not sorted_data:
            return 0.0
        
        n = len(sorted_data)
        k = (n - 1) * percentile / 100
        f = int(k)
        c = int(k) + 1
        
        if c >= n:
            return sorted_data[-1]
        
        # Linear interpolation
        return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])
    
    def reset(self) -> None:
        """Reset all latency data (e.g., daily rotation)"""
        self.latencies.clear()
        log.info("latency_tracker_reset")

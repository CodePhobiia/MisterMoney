"""
Route-Specific Calibration Models
Per-route calibrators with learnable weights and conformal prediction intervals
"""

import json
import math
from pathlib import Path
from typing import Literal
import structlog

log = structlog.get_logger()


def _logit(p: float) -> float:
    """Convert probability to logit (log-odds), clipped to avoid infinities"""
    p_clipped = max(0.001, min(0.999, p))
    return math.log(p_clipped / (1 - p_clipped))


def _sigmoid(x: float) -> float:
    """Convert logit back to probability"""
    return 1.0 / (1.0 + math.exp(-x))


class RouteCalibrator:
    """Per-route calibration with learnable weights and conformal intervals"""
    
    COLD_START_THRESHOLD = 50  # Need 50+ resolved markets to move beyond cold start
    
    def __init__(self, route: Literal["numeric", "simple", "rule", "dossier"]):
        """
        Initialize route-specific calibrator
        
        Args:
            route: Route name (numeric, simple, rule, dossier)
        """
        self.route = route
        # Cold start: beta = [0, 1, 0, 0, 0, 0, 0] (pass through market prior)
        self.beta = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        self.resolved_market_count = 0
        self.calibration_stats = {}
        
    def _extract_features(self, raw_p: float, features: dict) -> list[float]:
        """
        Extract feature vector from raw probability and metadata
        
        Feature vector x:
        - x[0] = logit(raw_p)           # model's raw estimate
        - x[1] = logit(market_mid)       # market prior
        - x[2] = uncertainty             # model's self-assessed uncertainty
        - x[3] = evidence_count          # number of evidence items
        - x[4] = source_reliability_avg  # average reliability of evidence
        - x[5] = hours_to_resolution     # time pressure
        - x[6] = volume_24h_log          # market liquidity signal
        """
        x = [
            _logit(raw_p),
            _logit(features.get('market_mid', 0.5)),
            features.get('uncertainty', 0.5),
            min(features.get('evidence_count', 0) / 10.0, 1.0),  # normalize to [0, 1]
            features.get('source_reliability_avg', 0.5),
            min(features.get('hours_to_resolution', 168) / 168.0, 1.0),  # normalize by week
            math.log1p(features.get('volume_24h', 0)) / 10.0,  # log scale, normalize
        ]
        return x
        
    def calibrate(self, raw_p: float, features: dict) -> float:
        """
        Calibrate raw probability using learned weights
        
        p_calibrated = sigmoid(beta^T @ x)
        
        Args:
            raw_p: Model's raw probability estimate
            features: Dict with market_mid, uncertainty, evidence_count, 
                     source_reliability_avg, hours_to_resolution, volume_24h
                     
        Returns:
            Calibrated probability in [0, 1]
        """
        x = self._extract_features(raw_p, features)
        
        # Compute beta^T @ x (dot product)
        logit_calibrated = sum(b * xi for b, xi in zip(self.beta, x))
        
        # Convert back to probability
        p_calibrated = _sigmoid(logit_calibrated)
        
        log.debug(
            "calibrate",
            route=self.route,
            raw_p=round(raw_p, 4),
            p_calibrated=round(p_calibrated, 4),
            resolved_count=self.resolved_market_count
        )
        
        return p_calibrated
        
    def conformal_interval(self, raw_p: float, features: dict) -> tuple[float, float]:
        """
        Compute conformal prediction interval
        
        Cold start: wide intervals [max(0, p-0.20), min(1, p+0.20)]
        With data: calibrated from resolved market residuals
        
        Args:
            raw_p: Model's raw probability estimate
            features: Same as calibrate()
            
        Returns:
            (p_low, p_high) tuple representing 90% prediction interval
        """
        p_calibrated = self.calibrate(raw_p, features)
        
        if self.resolved_market_count < self.COLD_START_THRESHOLD:
            # Cold start: wide intervals (±0.20)
            margin = 0.20
        else:
            # Use calibrated margin from stats
            margin = self.calibration_stats.get('interval_width', 0.15) / 2.0
            
        p_low = max(0.0, p_calibrated - margin)
        p_high = min(1.0, p_calibrated + margin)
        
        return (p_low, p_high)
        
    def update_weights(self, resolved_markets: list[dict]) -> None:
        """
        Retrain beta from resolved markets using logistic regression
        
        Requires 50+ resolved markets to move beyond cold start.
        Uses gradient descent on (features, actual_outcome) pairs.
        
        Args:
            resolved_markets: List of dicts with 'raw_p', 'features', 'outcome' (0 or 1)
        """
        if len(resolved_markets) < self.COLD_START_THRESHOLD:
            log.info(
                "calibrator_cold_start",
                route=self.route,
                count=len(resolved_markets),
                threshold=self.COLD_START_THRESHOLD
            )
            self.resolved_market_count = len(resolved_markets)
            return
            
        log.info(
            "calibrator_retraining",
            route=self.route,
            count=len(resolved_markets)
        )
        
        # Extract feature matrix and outcomes
        X = []
        y = []
        for market in resolved_markets:
            x = self._extract_features(market['raw_p'], market['features'])
            X.append(x)
            y.append(market['outcome'])
            
        # Simple gradient descent for logistic regression
        # Learning rate and iterations
        lr = 0.01
        iterations = 1000
        n = len(X)
        
        # Initialize beta if still at cold start values
        if self.resolved_market_count < self.COLD_START_THRESHOLD:
            # Start with equal weighting of model and market
            self.beta = [0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0]
            
        # Gradient descent
        for _ in range(iterations):
            # Compute predictions
            predictions = [_sigmoid(sum(b * xi for b, xi in zip(self.beta, x))) for x in X]
            
            # Compute gradients
            gradients = [0.0] * 7
            for i in range(n):
                error = predictions[i] - y[i]
                for j in range(7):
                    gradients[j] += error * X[i][j]
                    
            # Update weights
            for j in range(7):
                self.beta[j] -= lr * gradients[j] / n
                
        # Compute calibration stats
        predictions = [_sigmoid(sum(b * xi for b, xi in zip(self.beta, x))) for x in X]
        residuals = [abs(pred - actual) for pred, actual in zip(predictions, y)]
        
        self.calibration_stats = {
            'mean_residual': sum(residuals) / len(residuals),
            'interval_width': sorted(residuals)[int(0.9 * len(residuals))],  # 90th percentile
            'log_loss': -sum(
                y[i] * math.log(max(predictions[i], 1e-10)) + 
                (1 - y[i]) * math.log(max(1 - predictions[i], 1e-10))
                for i in range(n)
            ) / n
        }
        
        self.resolved_market_count = len(resolved_markets)
        
        log.info(
            "calibrator_trained",
            route=self.route,
            count=self.resolved_market_count,
            beta=[round(b, 4) for b in self.beta],
            stats={k: round(v, 4) for k, v in self.calibration_stats.items()}
        )
        
    def save(self, path: str) -> None:
        """
        Save calibrator state to JSON file
        
        Args:
            path: File path to save to
        """
        state = {
            'route': self.route,
            'beta': self.beta,
            'resolved_market_count': self.resolved_market_count,
            'calibration_stats': self.calibration_stats,
        }
        
        path_obj = Path(path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        
        with open(path, 'w') as f:
            json.dump(state, f, indent=2)
            
        log.info("calibrator_saved", route=self.route, path=path)
        
    def load(self, path: str) -> None:
        """
        Load calibrator state from JSON file
        
        Args:
            path: File path to load from
        """
        with open(path, 'r') as f:
            state = json.load(f)
            
        self.route = state['route']
        self.beta = state['beta']
        self.resolved_market_count = state['resolved_market_count']
        self.calibration_stats = state['calibration_stats']
        
        log.info(
            "calibrator_loaded",
            route=self.route,
            path=path,
            count=self.resolved_market_count
        )


class CalibrationManager:
    """Manages all 4 route calibrators"""
    
    ROUTES = ["numeric", "simple", "rule", "dossier"]
    
    def __init__(self, data_dir: str = "data/v3/calibration"):
        """
        Initialize calibration manager
        
        Args:
            data_dir: Directory to store calibrator state files
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize all route calibrators
        self.calibrators = {
            route: RouteCalibrator(route)
            for route in self.ROUTES
        }
        
        # Try to load existing calibrators
        self._load_all()
        
    def _load_all(self) -> None:
        """Load all calibrators from disk if they exist"""
        for route in self.ROUTES:
            path = self.data_dir / f"{route}_calibrator.json"
            if path.exists():
                try:
                    self.calibrators[route].load(str(path))
                except Exception as e:
                    log.warning("calibrator_load_failed", route=route, error=str(e))
                    
    def get_calibrator(self, route: str) -> RouteCalibrator:
        """
        Get calibrator for a specific route
        
        Args:
            route: Route name
            
        Returns:
            RouteCalibrator instance
        """
        if route not in self.calibrators:
            raise ValueError(f"Unknown route: {route}. Must be one of {self.ROUTES}")
        return self.calibrators[route]
        
    async def retrain_all(self, db) -> dict:
        """
        Retrain all calibrators from resolved markets in database
        
        Args:
            db: Database instance (v3.evidence.db.Database)
            
        Returns:
            Dict with per-route training stats
        """
        from v3.evidence.db import Database
        
        log.info("retraining_all_calibrators")
        
        results = {}
        
        for route in self.ROUTES:
            # Query resolved markets for this route from database
            # NOTE: This assumes a resolved_markets table exists
            # For now, we'll use a mock query structure
            query = """
                SELECT 
                    fvs.p_calibrated as raw_p,
                    fvs.uncertainty,
                    array_length(fvs.evidence_ids, 1) as evidence_count,
                    rm.outcome,
                    rm.market_mid,
                    rm.hours_to_resolution,
                    rm.volume_24h
                FROM fair_value_signals fvs
                JOIN resolved_markets rm ON rm.condition_id = fvs.condition_id
                WHERE fvs.route = $1
                  AND rm.outcome IS NOT NULL
                ORDER BY fvs.generated_at DESC
                LIMIT 500
            """
            
            try:
                rows = await db.fetch(query, route)
                
                if not rows:
                    log.info("no_resolved_markets", route=route)
                    results[route] = {
                        'status': 'no_data',
                        'count': 0
                    }
                    continue
                    
                # Convert to format expected by update_weights
                resolved_markets = []
                for row in rows:
                    resolved_markets.append({
                        'raw_p': row['raw_p'],
                        'features': {
                            'market_mid': row.get('market_mid', 0.5),
                            'uncertainty': row.get('uncertainty', 0.5),
                            'evidence_count': row.get('evidence_count', 0),
                            'source_reliability_avg': 0.7,  # TODO: compute from evidence
                            'hours_to_resolution': row.get('hours_to_resolution', 168),
                            'volume_24h': row.get('volume_24h', 0),
                        },
                        'outcome': row['outcome']
                    })
                    
                # Retrain calibrator
                calibrator = self.get_calibrator(route)
                calibrator.update_weights(resolved_markets)
                
                # Save updated calibrator
                path = self.data_dir / f"{route}_calibrator.json"
                calibrator.save(str(path))
                
                results[route] = {
                    'status': 'trained',
                    'count': len(resolved_markets),
                    'beta': calibrator.beta,
                    'stats': calibrator.calibration_stats
                }
                
            except Exception as e:
                log.error("retrain_failed", route=route, error=str(e))
                results[route] = {
                    'status': 'error',
                    'error': str(e)
                }
                
        log.info("retrain_all_complete", results=results)
        return results

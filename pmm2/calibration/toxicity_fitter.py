"""Fit toxicity weights from fill markout data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pmm1.storage.database import Database

logger = structlog.get_logger(__name__)


class ToxicityFitter:
    """Fit toxicity weights from fill markout data.

    After 7+ days of data, fit optimal weights:
    adverse_pnl ~ w1*markout_1s + w2*markout_5s + w3*markout_30s

    Default weights: (0.5, 0.3, 0.2)
    """

    def __init__(self, db: Database, min_fills: int = 50):
        """Initialize toxicity fitter.

        Args:
            db: Database instance
            min_fills: Minimum fills required for fitting
        """
        self.db = db
        self.min_fills = min_fills
        self.fitted_weights: tuple[float, float, float] = (0.5, 0.3, 0.2)

    async def fit(self) -> tuple[float, float, float] | None:
        """Fit toxicity weights from fill data.

        Returns (w_1s, w_5s, w_30s) tuple or None if insufficient data.

        Method: Simple OLS regression
        - Dependent: realized adverse PnL per fill
        - Independent: markout_1s, markout_5s, markout_30s

        Constraints: all weights >= 0, sum to 1
        """
        # Fetch fill data with markouts
        fills = await self.db.fetch_all(
            """
            SELECT
                side,
                price,
                size,
                dollar_value,
                markout_1s,
                markout_5s,
                markout_30s,
                mid_at_fill
            FROM fill_record
            WHERE
                markout_1s IS NOT NULL
                AND markout_5s IS NOT NULL
                AND markout_30s IS NOT NULL
                AND mid_at_fill IS NOT NULL
            ORDER BY ts DESC
            LIMIT 500
            """
        )

        if len(fills) < self.min_fills:
            logger.debug(
                "insufficient_data_for_toxicity_fit",
                fills=len(fills),
                required=self.min_fills
            )
            return None

        # Prepare data for regression
        features = []  # [markout_1s, markout_5s, markout_30s]
        y = []  # adverse_pnl

        for fill in fills:
            # Compute adverse PnL
            # For BUY: adverse if price moved up (we bought before spike)
            # For SELL: adverse if price moved down (we sold before drop)
            side = fill["side"]
            markout_1s = fill["markout_1s"]
            markout_5s = fill["markout_5s"]
            markout_30s = fill["markout_30s"]
            size = fill["size"]

            # Adverse PnL = -size * markout (negative markout = adverse)
            # For BUY: if markout > 0 (price went up), we made money (not adverse)
            # For SELL: if markout < 0 (price went down), we made money (not adverse)
            # So adverse_pnl = -size * markout for both sides
            if side == "BUY":
                adverse_pnl_1s = -size * markout_1s
                adverse_pnl_5s = -size * markout_5s
                adverse_pnl_30s = -size * markout_30s
            else:  # SELL
                adverse_pnl_1s = size * markout_1s
                adverse_pnl_5s = size * markout_5s
                adverse_pnl_30s = size * markout_30s

            # Use average adverse PnL as dependent variable
            avg_adverse_pnl = (adverse_pnl_1s + adverse_pnl_5s + adverse_pnl_30s) / 3.0

            features.append([markout_1s, markout_5s, markout_30s])
            y.append(avg_adverse_pnl)

        # Attempt to import numpy for OLS
        try:
            import numpy as np

            features_np = np.array(features)
            y_np = np.array(y)

            # OLS: β = (X'X)^(-1) X'y
            # But we want constrained: weights >= 0, sum = 1
            # For simplicity, use non-negative least squares approximation

            # Compute covariance matrix
            cov = np.dot(features_np.T, features_np)
            cov_y = np.dot(features_np.T, y_np)

            # Solve using pseudo-inverse (unconstrained)
            try:
                weights_raw = np.linalg.solve(cov, cov_y)
            except np.linalg.LinAlgError:
                # Singular matrix, use pseudo-inverse
                weights_raw = np.linalg.lstsq(features_np, y_np, rcond=None)[0]

            # Apply constraints: clip negative weights to 0
            weights_clipped = np.maximum(weights_raw, 0)

            # Normalize to sum to 1
            weight_sum = np.sum(weights_clipped)
            if weight_sum > 0:
                weights_normalized = weights_clipped / weight_sum
                fitted_weights = tuple(weights_normalized)
            else:
                # All weights negative, revert to default
                fitted_weights = (0.5, 0.3, 0.2)

        except ImportError:
            logger.warning("numpy_not_available_using_simple_averaging")
            # Fallback: simple correlation-based weighting
            # Compute correlation of each markout with average adverse PnL
            corr_1s = self._simple_correlation([x[0] for x in features], y)
            corr_5s = self._simple_correlation([x[1] for x in features], y)
            corr_30s = self._simple_correlation([x[2] for x in features], y)

            # Use absolute correlations as weights
            total_corr = abs(corr_1s) + abs(corr_5s) + abs(corr_30s)
            if total_corr > 0:
                fitted_weights = (
                    abs(corr_1s) / total_corr,
                    abs(corr_5s) / total_corr,
                    abs(corr_30s) / total_corr
                )
            else:
                fitted_weights = (0.5, 0.3, 0.2)

        self.fitted_weights = fitted_weights

        logger.info(
            "toxicity_weights_fitted",
            fills_used=len(fills),
            w_1s=fitted_weights[0],
            w_5s=fitted_weights[1],
            w_30s=fitted_weights[2]
        )

        return fitted_weights

    def _simple_correlation(self, x: list[float], y: list[float]) -> float:
        """Compute simple correlation coefficient.

        Args:
            x: Independent variable values
            y: Dependent variable values

        Returns:
            Correlation coefficient
        """
        n = len(x)
        if n == 0:
            return 0.0

        mean_x = sum(x) / n
        mean_y = sum(y) / n

        numerator = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))

        var_x = sum((x[i] - mean_x) ** 2 for i in range(n))
        var_y = sum((y[i] - mean_y) ** 2 for i in range(n))

        denominator = (var_x * var_y) ** 0.5

        if denominator == 0:
            return 0.0

        return float(numerator / denominator)

    async def get_data_summary(self) -> dict[str, Any]:
        """Return summary of available markout data.

        {fills_total: N, fills_with_markouts: N,
         avg_markout_1s: float, avg_markout_5s: float, avg_markout_30s: float,
         data_days: float}
        """
        summary = await self.db.fetch_one(
            """
            SELECT
                COUNT(*) as fills_total,
                SUM(CASE WHEN markout_1s IS NOT NULL THEN 1 ELSE 0 END) as fills_with_markouts,
                AVG(markout_1s) as avg_markout_1s,
                AVG(markout_5s) as avg_markout_5s,
                AVG(markout_30s) as avg_markout_30s,
                JULIANDAY('now') - JULIANDAY(MIN(ts)) as data_days
            FROM fill_record
            """
        )

        return summary or {}

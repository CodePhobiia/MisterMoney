"""Fair value model from §6.

Logit-space fair value:
    x_t = β_0 + β_1·logit(m_t) + β_2·logit(μ_t) + β_3·I_t + β_4·F_t + β_5·R_t + β_6·E_t
    p̂_t = σ(x_t)

Model haircut:
    h_t = h_0 + k_1·V_t + k_2·stale_t + k_3·resolutionRisk_t + k_4·modelError_t
"""

from __future__ import annotations

import math

import structlog
from pydantic import BaseModel

from pmm1.settings import PricingConfig
from pmm1.strategy.features import FeatureVector

logger = structlog.get_logger(__name__)


def sigmoid(x: float) -> float:
    """Numerically stable sigmoid function."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    else:
        z = math.exp(x)
        return z / (1.0 + z)


def logit(p: float) -> float:
    """Logit function: log(p / (1-p)). Clamps to avoid infinity."""
    p = max(0.001, min(0.999, p))
    return math.log(p / (1.0 - p))


class FairValueEstimate(BaseModel):
    """Result of fair value computation."""

    fair_value: float  # p̂_t ∈ (0, 1)
    logit_value: float  # x_t (logit space)
    haircut: float  # h_t
    confidence: float  # 1 - haircut (how confident we are)
    components: dict[str, float] = {}  # Contribution of each feature

    @property
    def fair_value_pct(self) -> float:
        """Fair value as percentage."""
        return self.fair_value * 100

    @property
    def edge_vs_mid(self) -> float:
        """Edge = fair_value - midpoint (set externally)."""
        return 0.0  # Set by caller


class FairValueModel:
    """Logit-space fair value model.

    v1 is a simple logistic model with configurable coefficients.
    Later: boosted trees after live recorder + clean feature history.
    """

    def __init__(self, config: PricingConfig) -> None:
        self.config = config
        # Model error tracking for haircut
        self._prediction_errors: list[float] = []
        self._max_error_history = 100

    @staticmethod
    def compute_microprice(
        best_bid: float, best_ask: float, bid_size: float, ask_size: float
    ) -> float:
        """Volume-weighted midpoint — strictly better than simple midpoint for binary markets."""
        total = bid_size + ask_size
        if total <= 0:
            return (best_bid + best_ask) / 2
        return (best_bid * ask_size + best_ask * bid_size) / total

    def compute_fair_value(self, features: FeatureVector) -> FairValueEstimate:
        """Compute fair value from features using logit-space model.

        x_t = β_0 + β_1·logit(m_t) + β_2·logit(μ_t) + β_3·I_t
              + β_4·F_t + β_5·R_t + β_6·E_t
        p̂_t = σ(x_t)

        If the model is uncalibrated (default betas), use a microprice blend
        instead of the identity function (which just returns midpoint).
        """
        cfg = self.config

        # If model is uncalibrated (default betas), use microprice directly
        if abs(cfg.beta_2) < 1e-6 and abs(cfg.beta_3) < 1e-6:
            # Uncalibrated model — microprice is strictly better than identity
            if (
                features.microprice > 0
                and features.microprice != features.midpoint
                and features.midpoint > 0
            ):
                # Blend: 70% microprice + 30% midpoint for stability
                p_hat = 0.7 * features.microprice + 0.3 * features.midpoint
                p_hat = max(0.001, min(0.999, p_hat))

                haircut = self.compute_haircut(features)
                confidence = max(0.0, min(1.0, 1.0 - haircut))

                logger.debug(
                    "fair_value_microprice_blend",
                    token_id=features.token_id[:16],
                    fair_value=f"{p_hat:.4f}",
                    midpoint=f"{features.midpoint:.4f}",
                    microprice=f"{features.microprice:.4f}",
                )

                return FairValueEstimate(
                    fair_value=p_hat,
                    logit_value=logit(p_hat),
                    haircut=haircut,
                    confidence=confidence,
                    components={"microprice_blend": p_hat},
                )

        # Compute logit-space features
        logit_mid = features.logit_midpoint
        logit_micro = features.logit_microprice

        # Components
        components = {
            "intercept": cfg.beta_0,
            "logit_mid": cfg.beta_1 * logit_mid,
            "logit_micro": cfg.beta_2 * logit_micro,
            "imbalance": cfg.beta_3 * features.imbalance,
            "trade_flow": cfg.beta_4 * features.signed_trade_flow,
            "related_residual": cfg.beta_5 * features.related_market_residual,
            "external_signal": cfg.beta_6 * features.external_signal,
        }

        # Sum to get logit-space value
        x_t = sum(components.values())

        # Apply sigmoid to get probability
        p_hat = sigmoid(x_t)

        # Compute haircut
        haircut = self.compute_haircut(features)

        # Confidence = 1 - haircut, clamped
        confidence = max(0.0, min(1.0, 1.0 - haircut))

        estimate = FairValueEstimate(
            fair_value=p_hat,
            logit_value=x_t,
            haircut=haircut,
            confidence=confidence,
            components=components,
        )

        logger.debug(
            "fair_value_computed",
            token_id=features.token_id[:16],
            fair_value=f"{p_hat:.4f}",
            midpoint=f"{features.midpoint:.4f}",
            haircut=f"{haircut:.4f}",
        )

        return estimate

    def compute_haircut(self, features: FeatureVector) -> float:
        """Compute model haircut from §6.

        h_t = h_0 + k_1·V_t + k_2·stale_t + k_3·resolutionRisk_t + k_4·modelError_t

        No taking flow unless edge exceeds h_t.
        """
        cfg = self.config

        # Staleness indicator
        stale_t = 1.0 if features.is_stale else 0.0

        # Resolution risk based on time remaining
        resolution_risk = 0.0
        if features.time_to_resolution_hours < 1:
            resolution_risk = 1.0
        elif features.time_to_resolution_hours < 6:
            resolution_risk = 0.5
        elif features.time_to_resolution_hours < 24:
            resolution_risk = 0.2
        elif features.time_to_resolution_hours < 72:
            resolution_risk = 0.05

        # Model error from recent prediction accuracy
        model_error = self._get_model_error()

        haircut = (
            cfg.h_0
            + cfg.k_1 * features.realized_vol
            + cfg.k_2 * stale_t
            + cfg.k_3 * resolution_risk
            + cfg.k_4 * model_error
        )

        return max(0.0, min(0.5, haircut))  # Cap at 50%

    def _get_model_error(self) -> float:
        """Get recent model prediction error (MAE)."""
        if not self._prediction_errors:
            return 0.0
        return sum(abs(e) for e in self._prediction_errors) / len(self._prediction_errors)

    def record_prediction_error(self, predicted: float, actual: float) -> None:
        """Record a prediction error for haircut calibration.

        Called after a trade reveals the actual fair value.
        """
        error = predicted - actual
        self._prediction_errors.append(error)
        if len(self._prediction_errors) > self._max_error_history:
            self._prediction_errors.pop(0)

    def get_edge(
        self,
        fair_value: float,
        execution_price: float,
        side: str,
        haircut: float,
    ) -> float:
        """Compute edge for a potential taker trade.

        For BUY: edge = fair_value - execution_price - haircut
        For SELL: edge = execution_price - fair_value - haircut

        Only cross spread if edge > take_threshold_cents / 100.
        """
        if side == "BUY":
            return fair_value - execution_price - haircut
        else:
            return execution_price - fair_value - haircut

    def should_take(
        self,
        fair_value: float,
        execution_price: float,
        side: str,
        haircut: float,
        fee: float = 0.0,
        slippage: float = 0.0,
    ) -> bool:
        """Determine if we should cross the spread (take liquidity).

        From §7: takeEV = (p̂_t - p_exec)·Q - fee - slippage - h_t
        Only cross if takeEV > take_threshold.
        """
        edge = self.get_edge(fair_value, execution_price, side, haircut)
        net_edge = edge - fee - slippage
        threshold = self.config.take_threshold_cents / 100.0

        return net_edge > threshold

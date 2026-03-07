"""Fill hazard function for computing fill probability and ETA."""

from __future__ import annotations

import math


class FillHazard:
    """Compute fill probability and ETA from queue state."""

    def __init__(self, kappa: float = 1.0, rho: float = 0.5) -> None:
        """Initialize fill hazard calculator.

        Args:
            kappa: queue depth scaling factor for fill probability
            rho: fraction of own order that contributes to fill time
        """
        self.kappa = kappa
        self.rho = rho
        # Observed depletion rates per token (learned from book snapshots)
        self.depletion_rates: dict[str, float] = {}  # token_id -> shares/sec
        self.default_depletion_rate: float = 1.0  # shares/sec default

    def fill_probability(
        self,
        queue_ahead: float,
        order_size: float,
        horizon_sec: float,
        depletion_rate: float,
    ) -> float:
        """Calculate fill probability over a time horizon.

        P^fill(H_Q) = 1 - exp(-lambda * H_Q / (1 + kappa * A / Q))

        Args:
            queue_ahead: estimated shares ahead in queue
            order_size: our order size
            horizon_sec: time horizon in seconds
            depletion_rate: observed queue depletion intensity (shares/sec)

        Returns:
            Probability of fill in [0, 1]
        """
        if order_size <= 0 or depletion_rate <= 0:
            return 0.0
        ratio = self.kappa * queue_ahead / order_size if order_size > 0 else float("inf")
        exponent = -depletion_rate * horizon_sec / (1 + ratio)
        return 1.0 - math.exp(exponent)

    def eta(self, queue_ahead: float, order_size: float, depletion_rate: float) -> float:
        """Calculate estimated time to fill.

        ETA = (A + rho * Q) / d_hat

        Args:
            queue_ahead: estimated shares ahead in queue
            order_size: our order size
            depletion_rate: observed queue depletion intensity (shares/sec)

        Returns:
            Estimated seconds to fill (inf if depletion_rate <= 0)
        """
        if depletion_rate <= 0:
            return float("inf")
        return (queue_ahead + self.rho * order_size) / depletion_rate

    def update_depletion_rate(
        self, token_id: str, observed_rate: float, alpha: float = 0.1
    ) -> None:
        """Update depletion rate using exponential moving average.

        Args:
            token_id: token identifier
            observed_rate: newly observed depletion rate (shares/sec)
            alpha: learning rate for EMA (0 < alpha <= 1)
        """
        current = self.depletion_rates.get(token_id, self.default_depletion_rate)
        self.depletion_rates[token_id] = alpha * observed_rate + (1 - alpha) * current

    def get_depletion_rate(self, token_id: str) -> float:
        """Get current depletion rate estimate for a token.

        Args:
            token_id: token identifier

        Returns:
            Depletion rate in shares/sec
        """
        return self.depletion_rates.get(token_id, self.default_depletion_rate)

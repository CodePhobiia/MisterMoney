"""Queue position model from §7.

Fill model (queue-aware):
    P(fill | Δ, qpos, τ) = 1 − e^{−λ(Δ,qpos)·τ}
    λ(Δ, qpos) = exp(θ_0 − θ_1·Δ − θ_2·qpos + θ_3·flow)

Where:
    Δ = distance from best price
    qpos = estimated queue position
    flow = recent trade/sweep intensity
"""

from __future__ import annotations

import math
from decimal import Decimal

import structlog

from pmm1.state.books import OrderBook

logger = structlog.get_logger(__name__)


class QueuePositionModel:
    """Models queue position and fill probability for resting orders.

    Parameters:
        θ_0: Base arrival rate (intercept)
        θ_1: Distance penalty (higher = faster decay with distance)
        θ_2: Queue position penalty
        θ_3: Flow boost (higher flow = more fills)
    """

    def __init__(
        self,
        theta_0: float = -1.0,
        theta_1: float = 5.0,
        theta_2: float = 0.5,
        theta_3: float = 1.0,
    ) -> None:
        self.theta_0 = theta_0
        self.theta_1 = theta_1
        self.theta_2 = theta_2
        self.theta_3 = theta_3

    def compute_arrival_rate(
        self,
        distance_from_best: float,
        queue_position: float,
        flow_intensity: float = 0.0,
    ) -> float:
        """Compute arrival rate λ(Δ, qpos).

        λ(Δ, qpos) = exp(θ_0 − θ_1·Δ − θ_2·qpos + θ_3·flow)

        Args:
            distance_from_best: Price distance from best bid/ask (in raw units).
            queue_position: Estimated queue position (0 = front of queue).
            flow_intensity: Recent trade flow intensity.

        Returns:
            Arrival rate λ (fills per second).
        """
        exponent = (
            self.theta_0
            - self.theta_1 * distance_from_best
            - self.theta_2 * queue_position
            + self.theta_3 * flow_intensity
        )
        # Clamp to avoid overflow
        exponent = max(-10, min(10, exponent))
        return math.exp(exponent)

    def fill_probability(
        self,
        distance_from_best: float,
        queue_position: float,
        trade_size: float = 0.0,
        order_size: float = 0.0,
        tau: float = 1.0,
        flow_intensity: float = 0.0,
    ) -> float:
        """Compute fill probability.

        P(fill | Δ, qpos, τ) = 1 − e^{−λ(Δ,qpos)·τ}

        Also considers trade size vs queue position for immediate fills.

        Args:
            distance_from_best: Distance from best price.
            queue_position: Queue position in shares.
            trade_size: Incoming trade size (for immediate fill check).
            order_size: Our order size.
            tau: Time horizon in seconds.
            flow_intensity: Trade flow intensity.

        Returns:
            Fill probability in [0, 1].
        """
        # Immediate fill: if trade size exceeds our queue position
        if trade_size > 0 and queue_position >= 0:
            if trade_size > queue_position:
                remaining_after_queue = trade_size - queue_position
                if remaining_after_queue >= order_size:
                    return 1.0
                elif remaining_after_queue > 0:
                    return min(1.0, remaining_after_queue / order_size)

        # Time-based fill probability
        lam = self.compute_arrival_rate(
            distance_from_best, queue_position, flow_intensity
        )
        prob = 1.0 - math.exp(-lam * tau)
        return max(0.0, min(1.0, prob))

    def estimate_queue_position(
        self,
        price: float,
        side: str,
        book: OrderBook,
    ) -> float:
        """Estimate our queue position for a new order at given price.

        Queue position = total size ahead of us at the same price level,
        plus any better-priced orders.

        Args:
            price: Our order price.
            side: BUY or SELL.
            book: Current order book.

        Returns:
            Estimated queue position in shares.
        """
        queue = 0.0
        our_price = Decimal(str(price))

        if side == "BUY":
            # For bids: orders at higher price are ahead
            # Orders at same price are ahead (we're at the back)
            for level in book.get_bids():
                if level.price > our_price:
                    queue += level.size_float
                elif level.price == our_price:
                    queue += level.size_float  # We join the back
                else:
                    break  # Lower prices are behind us
        else:
            # For asks: orders at lower price are ahead
            for level in book.get_asks():
                if level.price < our_price:
                    queue += level.size_float
                elif level.price == our_price:
                    queue += level.size_float
                else:
                    break

        return queue

    def estimate_time_to_fill(
        self,
        distance_from_best: float,
        queue_position: float,
        flow_intensity: float = 0.0,
        target_probability: float = 0.5,
    ) -> float:
        """Estimate time to fill at given probability threshold.

        Solve: target = 1 - e^(-λ·τ) → τ = -ln(1-target) / λ

        Args:
            distance_from_best: Distance from best price.
            queue_position: Queue position in shares.
            flow_intensity: Trade flow intensity.
            target_probability: Target fill probability (default 50%).

        Returns:
            Estimated seconds to reach target fill probability.
        """
        lam = self.compute_arrival_rate(
            distance_from_best, queue_position, flow_intensity
        )

        if lam <= 0.0001:
            return float("inf")

        tau = -math.log(1.0 - target_probability) / lam
        return tau

    def expected_fill_value(
        self,
        price: float,
        size: float,
        side: str,
        reservation_price: float,
        distance_from_best: float,
        queue_position: float,
        tau: float = 30.0,
        flow_intensity: float = 0.0,
        adverse_selection_estimate: float = 0.0,
    ) -> float:
        """Compute expected value of a resting order.

        EV = P(fill) × (edge - adverse_selection)

        Args:
            price: Our order price.
            size: Our order size.
            side: BUY or SELL.
            reservation_price: Our fair value estimate.
            distance_from_best: Distance from best.
            queue_position: Queue position.
            tau: Time horizon.
            flow_intensity: Trade flow.
            adverse_selection_estimate: Expected adverse selection cost.

        Returns:
            Expected value of the order.
        """
        p_fill = self.fill_probability(
            distance_from_best, queue_position,
            order_size=size, tau=tau,
            flow_intensity=flow_intensity,
        )

        if side == "BUY":
            edge = reservation_price - price
        else:
            edge = price - reservation_price

        net_edge = edge - adverse_selection_estimate
        return p_fill * net_edge * size

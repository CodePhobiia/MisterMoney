"""Action EV calculator for persistence decisions.

Computes expected value for 7 possible actions:
- HOLD: keep order as-is
- IMPROVE1/IMPROVE2: move 1-2 ticks toward mid
- WIDEN1/WIDEN2: move 1-2 ticks away from mid
- CANCEL: remove order entirely
- CROSS: cross the spread (aggressive fill)
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pmm2.persistence.state_machine import PersistenceOrder
    from pmm2.queue.hazard import FillHazard


class PersistenceAction(str, Enum):
    """Possible persistence actions."""

    HOLD = "HOLD"
    IMPROVE1 = "IMPROVE1"  # move 1 tick toward mid
    IMPROVE2 = "IMPROVE2"  # move 2 ticks toward mid
    WIDEN1 = "WIDEN1"  # move 1 tick away from mid
    WIDEN2 = "WIDEN2"  # move 2 ticks away from mid
    CANCEL = "CANCEL"  # remove order entirely
    CROSS = "CROSS"  # cross the spread (aggressive)


class ActionEVCalculator:
    """Compute EV for each persistence action."""

    def __init__(self, fill_hazard: FillHazard, order_horizon_sec: float = 30.0) -> None:
        """Initialize action EV calculator.

        Args:
            fill_hazard: FillHazard instance for computing fill probabilities
            order_horizon_sec: time horizon for EV calculations
        """
        self.fill_hazard = fill_hazard
        self.horizon = order_horizon_sec

    def compute_action_ev(
        self,
        order: PersistenceOrder,
        action: PersistenceAction,
        reservation_price: float,
        target_price: float,
        queue_ahead_after: float,
        depletion_rate: float,
        reward_rate: float = 0.0,
        rebate_rate: float = 0.0,
        tox_cost: float = 0.0,
        reset_cost: float = 0.0,
    ) -> float:
        """Calculate EV for a specific action.

        EV_a = P^fill_a * Edge_a * Q_a + E^liq_a + E^reb_a - C^tox_a - ResetCost_a

        Args:
            order: order to evaluate
            action: action to take
            reservation_price: our reservation price (max buy / min sell)
            target_price: fair value / target price
            queue_ahead_after: queue position after action
            depletion_rate: queue depletion rate (shares/sec)
            reward_rate: expected rewards per second from scoring
            rebate_rate: rebate rate for maker orders
            tox_cost: toxicity cost (adverse selection)
            reset_cost: cost of resetting queue position (computed separately)

        Returns:
            Expected value in USDC
        """
        # CANCEL has zero EV (no exposure)
        if action == PersistenceAction.CANCEL:
            return 0.0

        # CROSS has immediate fill, no queue
        if action == PersistenceAction.CROSS:
            edge = self._compute_edge(order.side, reservation_price, target_price)
            return edge * order.size - tox_cost * order.size

        # For HOLD, IMPROVE, WIDEN: compute fill-based EV
        # Get new price after action
        new_price = self._compute_new_price(order.price, action, order.side, tick_size=0.01)

        # Edge at new price
        edge = self._compute_edge(order.side, reservation_price, new_price)

        # Fill probability at new position
        fill_prob = self.fill_hazard.fill_probability(
            queue_ahead=queue_ahead_after,
            order_size=order.size,
            horizon_sec=self.horizon,
            depletion_rate=depletion_rate,
        )

        # Base EV: fill probability * edge * size
        ev = fill_prob * edge * order.size

        # Liquidity rewards (if scoring)
        if order.is_scoring:
            ev += reward_rate * self.horizon

        # Rebates
        ev += fill_prob * rebate_rate * order.size

        # Toxicity cost
        ev -= fill_prob * tox_cost * order.size

        # Reset cost (only for non-HOLD actions)
        if action != PersistenceAction.HOLD:
            ev -= reset_cost

        return ev

    def compute_reset_cost(
        self,
        order: PersistenceOrder,
        queue_value: float,
        warmup_loss: float,
    ) -> float:
        """Calculate cost of resetting an order.

        ResetCost = QueueValue + WarmupLoss + CancelCost

        Args:
            order: order being reset
            queue_value: value of current queue position
            warmup_loss: loss from resetting scoring warmup

        Returns:
            Reset cost in USDC
        """
        # Queue value: opportunity cost of losing queue position
        # Warmup loss: cost of losing progress toward scoring
        # Cancel cost: fixed cost of API call, gas, etc. (typically small)
        cancel_cost = 0.01  # nominal cost
        return queue_value + warmup_loss + cancel_cost

    def enumerate_and_score(
        self,
        order: PersistenceOrder,
        reservation_price: float,
        target_price: float,
        depletion_rate: float,
        tick_size: float = 0.01,
        reward_rate: float = 0.0,
        rebate_rate: float = 0.0,
        tox_cost: float = 0.0,
        book_depth_at_prices: dict[float, float] | None = None,
        warmup_loss: float = 0.0,
    ) -> dict[PersistenceAction, float]:
        """Score all 7 actions and return {action: EV} dict.

        Args:
            order: order to evaluate
            reservation_price: our reservation price
            target_price: fair value
            depletion_rate: queue depletion rate
            tick_size: price tick size
            reward_rate: scoring rewards per second
            rebate_rate: maker rebate rate
            tox_cost: toxicity cost
            book_depth_at_prices: order book depth at each price level
            warmup_loss: warmup loss from WarmupEstimator

        Returns:
            Dict mapping each action to its EV
        """
        evs: dict[PersistenceAction, float] = {}

        # Compute queue positions and reset costs for each action
        for action in PersistenceAction:
            # Determine queue position after action
            if action == PersistenceAction.HOLD:
                # Keep current queue position
                queue_ahead = order.est_ahead
                reset_cost = 0.0
            elif action in (
                PersistenceAction.IMPROVE1,
                PersistenceAction.IMPROVE2,
                PersistenceAction.WIDEN1,
                PersistenceAction.WIDEN2,
            ):
                # Moving to new price → back of queue
                new_price = self._compute_new_price(order.price, action, order.side, tick_size)
                queue_ahead = self._estimate_queue_at_price(
                    new_price, book_depth_at_prices
                )
                # Reset cost: value of current queue + warmup loss
                queue_value = self._compute_queue_value(
                    order, reservation_price, depletion_rate, reward_rate, rebate_rate
                )
                reset_cost = self.compute_reset_cost(order, queue_value, warmup_loss)
            elif action == PersistenceAction.CANCEL:
                queue_ahead = 0.0
                reset_cost = 0.0
            elif action == PersistenceAction.CROSS:
                queue_ahead = 0.0
                reset_cost = 0.0
            else:
                queue_ahead = 0.0
                reset_cost = 0.0

            # Compute EV for this action
            ev = self.compute_action_ev(
                order=order,
                action=action,
                reservation_price=reservation_price,
                target_price=target_price,
                queue_ahead_after=queue_ahead,
                depletion_rate=depletion_rate,
                reward_rate=reward_rate,
                rebate_rate=rebate_rate,
                tox_cost=tox_cost,
                reset_cost=reset_cost,
            )
            evs[action] = ev

        return evs

    def _compute_new_price(
        self, current_price: float, action: PersistenceAction, side: str, tick_size: float
    ) -> float:
        """Compute new price after action."""
        if action == PersistenceAction.HOLD:
            return current_price

        # Determine direction
        if side == "BUY":
            # IMPROVE = increase price (toward mid)
            # WIDEN = decrease price (away from mid)
            improve_direction = 1
        else:  # SELL
            # IMPROVE = decrease price (toward mid)
            # WIDEN = increase price (away from mid)
            improve_direction = -1

        if action == PersistenceAction.IMPROVE1:
            return current_price + improve_direction * tick_size
        elif action == PersistenceAction.IMPROVE2:
            return current_price + improve_direction * 2 * tick_size
        elif action == PersistenceAction.WIDEN1:
            return current_price - improve_direction * tick_size
        elif action == PersistenceAction.WIDEN2:
            return current_price - improve_direction * 2 * tick_size
        else:
            return current_price

    def _compute_edge(self, side: str, reservation_price: float, execution_price: float) -> float:
        """Compute edge (profit per share).

        For BUY: edge = reservation_price - execution_price (buy cheap, sell at reservation)
        For SELL: edge = execution_price - reservation_price (sell high, buy at reservation)
        """
        if side == "BUY":
            return reservation_price - execution_price
        else:  # SELL
            return execution_price - reservation_price

    def _estimate_queue_at_price(
        self, price: float, book_depth: dict[float, float] | None
    ) -> float:
        """Estimate queue depth at a given price level.

        If book_depth is provided, use it. Otherwise assume back of queue = 100 shares.
        """
        if book_depth is None:
            return 100.0  # default assumption
        return book_depth.get(price, 100.0)

    def _compute_queue_value(
        self,
        order: PersistenceOrder,
        reservation_price: float,
        depletion_rate: float,
        reward_rate: float,
        rebate_rate: float,
    ) -> float:
        """Compute value of current queue position.

        QueueValue = (P^fill(current) - P^fill(back)) * (Edge + Rewards + Rebates) * Q

        This is the opportunity cost of losing our place in line.
        """
        # Fill probability at current position
        fill_prob_current = self.fill_hazard.fill_probability(
            queue_ahead=order.est_ahead,
            order_size=order.size,
            horizon_sec=self.horizon,
            depletion_rate=depletion_rate,
        )

        # Fill probability at back of queue (estimate)
        queue_at_price = 100.0  # rough estimate
        fill_prob_back = self.fill_hazard.fill_probability(
            queue_ahead=queue_at_price,
            order_size=order.size,
            horizon_sec=self.horizon,
            depletion_rate=depletion_rate,
        )

        # Probability gain from queue position
        prob_gain = fill_prob_current - fill_prob_back

        # Value per fill
        edge = self._compute_edge(order.side, reservation_price, order.price)
        value_per_fill = edge + rebate_rate

        # Total queue value
        queue_value = prob_gain * value_per_fill * order.size

        # Add expected rewards if scoring
        if order.is_scoring:
            queue_value += reward_rate * self.horizon

        return max(0.0, queue_value)

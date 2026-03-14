"""Persistence optimizer - main decision engine.

Decides the best action for each live order by:
1. Enumerating all 7 possible actions
2. Computing EV for each
3. Applying hysteresis gate to prevent unnecessary moves
4. Returning (action, ev) tuple

Philosophy: HOLD is the overwhelming default. Only move when math is CLEARLY better.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pmm2.persistence.action_ev import ActionEVCalculator, PersistenceAction
from pmm2.persistence.hysteresis import HysteresisGate
from pmm2.persistence.state_machine import OrderPersistenceState, StateMachine
from pmm2.persistence.warmup import WarmupEstimator

if TYPE_CHECKING:
    pass


class PersistenceOptimizer:
    """Top-level optimizer — decides action for each live order."""

    def __init__(
        self,
        state_machine: StateMachine,
        action_calculator: ActionEVCalculator,
        hysteresis: HysteresisGate,
        warmup: WarmupEstimator,
    ) -> None:
        """Initialize persistence optimizer.

        Args:
            state_machine: order state machine
            action_calculator: action EV calculator
            hysteresis: hysteresis gate
            warmup: warmup loss estimator
        """
        self.sm = state_machine
        self.calc = action_calculator
        self.gate = hysteresis
        self.warmup = warmup

    def decide(
        self,
        order_id: str,
        reservation_price: float,
        target_price: float,
        depletion_rate: float,
        inventory_skew: float = 0.0,
        tick_size: float = 0.01,
        reward_rate: float = 0.0,
        rebate_rate: float = 0.0,
        tox_cost: float = 0.0,
        book_depth: dict[float, float] | None = None,
    ) -> tuple[PersistenceAction, float]:
        """Decide the best action for an order.

        Returns (action, ev) tuple.
        HOLD is default unless improvement exceeds hysteresis threshold.

        Args:
            order_id: order ID
            reservation_price: our reservation price (max buy / min sell)
            target_price: fair value / target price
            depletion_rate: queue depletion rate (shares/sec)
            inventory_skew: current inventory skew in USDC
            tick_size: price tick size
            reward_rate: expected rewards per second from scoring
            rebate_rate: rebate rate for maker orders
            tox_cost: toxicity cost per share
            book_depth: order book depth at each price level

        Returns:
            (action, ev) tuple
        """
        # 1. Get order from state machine
        order = self.sm.get_order(order_id)
        if not order:
            return (PersistenceAction.CANCEL, 0.0)

        # 2. Check if EXIT state → return CANCEL immediately
        if order.state == OrderPersistenceState.EXIT:
            return (PersistenceAction.CANCEL, 0.0)

        # 3. Compute warmup loss (for reset cost calculation)
        warmup_loss = self.warmup.warmup_loss(order, reward_rate)

        # 4. Enumerate all action EVs
        action_evs = self.calc.enumerate_and_score(
            order=order,
            reservation_price=reservation_price,
            target_price=target_price,
            depletion_rate=depletion_rate,
            tick_size=tick_size,
            reward_rate=reward_rate,
            rebate_rate=rebate_rate,
            tox_cost=tox_cost,
            book_depth_at_prices=book_depth,
            warmup_loss=warmup_loss,
        )

        # 5. Find best action
        hold_ev = action_evs[PersistenceAction.HOLD]
        best_action = max(action_evs, key=action_evs.get)  # type: ignore
        best_action_ev = action_evs[best_action]

        # 6. Apply hysteresis gate
        if best_action != PersistenceAction.HOLD:
            should_act = self.gate.should_act(
                order=order,
                hold_ev=hold_ev,
                best_action_ev=best_action_ev,
                inventory_skew=inventory_skew,
            )
            if not should_act:
                # Improvement not big enough, default to HOLD
                best_action = PersistenceAction.HOLD
                best_action_ev = hold_ev

        # 7. Update order's cached EV values
        order.hold_ev = hold_ev
        order.best_action = best_action
        order.best_action_ev = best_action_ev

        # 8. Return (action, ev)
        return (best_action, best_action_ev)

    def decide_all(
        self,
        live_orders: list[str],
        reservation_prices: dict[str, float],
        target_prices: dict[str, float],
        depletion_rates: dict[str, float],
        inventory_skews: dict[str, float],
        book_depths: dict[str, dict[float, float]] | None = None,
        tick_sizes: dict[str, float] | None = None,
        **kwargs: Any,
    ) -> dict[str, tuple[PersistenceAction, float]]:
        """Batch decide for all live orders.

        Args:
            live_orders: list of order IDs
            reservation_prices: {order_id: reservation_price}
            target_prices: {order_id: target_price}
            depletion_rates: {order_id: depletion_rate}
            inventory_skews: {order_id: inventory_skew}
            **kwargs: additional kwargs passed to decide()

        Returns:
            {order_id: (action, ev)} dict
        """
        decisions: dict[str, tuple[PersistenceAction, float]] = {}
        book_depths = book_depths or {}
        tick_sizes = tick_sizes or {}
        kwargs = dict(kwargs)
        default_tick_size: float = float(kwargs.pop("tick_size", 0.01))

        for order_id in live_orders:
            reservation = reservation_prices.get(order_id, 0.5)
            target = target_prices.get(order_id, 0.5)
            depletion = depletion_rates.get(order_id, 1.0)
            skew = inventory_skews.get(order_id, 0.0)

            decision = self.decide(
                order_id=order_id,
                reservation_price=reservation,
                target_price=target,
                depletion_rate=depletion,
                inventory_skew=skew,
                book_depth=book_depths.get(order_id),
                tick_size=tick_sizes.get(order_id, default_tick_size),
                **kwargs,
            )
            decisions[order_id] = decision

        return decisions

"""Queue position estimator for tracking order queue dynamics."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from pmm2.queue.hazard import FillHazard
from pmm2.queue.state import QueueState

if TYPE_CHECKING:
    from pmm1.storage.database import Database

logger = structlog.get_logger(__name__)


class QueueEstimator:
    """Estimates queue position and fill probability for active orders."""

    def __init__(self, beta: float = 0.5, chi: float = 0.1) -> None:
        """Initialize queue estimator.

        Args:
            beta: initialization factor (fraction of own order to subtract from visible size)
            chi: uncertainty penalty factor for queue_uncertainty calculation
        """
        self.beta = beta
        self.chi = chi
        self.states: dict[str, QueueState] = {}  # keyed by order_id
        self.hazard = FillHazard()

    def initialize_order(
        self,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        visible_size_at_price: float,
        condition_id: str = "",
    ) -> None:
        """Initialize queue state for a new order.

        Args:
            order_id: unique order identifier
            token_id: token identifier
            side: "BUY" or "SELL"
            price: order price
            size: order size
            visible_size_at_price: total visible size at this price level
            condition_id: optional condition identifier
        """
        # A_init = max(visible_size_at_price - beta * size, 0)
        a_init = max(visible_size_at_price - self.beta * size, 0.0)

        # Create QueueState with low/mid/high estimates
        state = QueueState(
            order_id=order_id,
            token_id=token_id,
            condition_id=condition_id,
            side=side,
            price=price,
            size_open=size,
            est_ahead_low=a_init * 0.7,
            est_ahead_mid=a_init,
            est_ahead_high=a_init * 1.3,
        )

        # Compute initial metrics
        self._update_metrics(state)

        self.states[order_id] = state
        logger.info(
            "queue_order_initialized",
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            est_ahead=state.est_ahead,
        )

    def update_from_book(
        self, token_id: str, price: float, old_size: float, new_size: float
    ) -> None:
        """Update queue estimates based on book depth changes.

        Args:
            token_id: token identifier
            price: price level that changed
            old_size: previous total size at this price
            new_size: new total size at this price
        """
        delta = old_size - new_size

        # Find all orders at this price/token
        matching_orders = [
            state
            for state in self.states.values()
            if state.token_id == token_id and abs(state.price - price) < 1e-6
        ]

        if not matching_orders:
            return

        if delta > 0:
            # Size decreased (queue consumed)
            for state in matching_orders:
                # Reduce est_ahead by delta, clamp to 0
                state.est_ahead_low = max(state.est_ahead_low - delta, 0.0)
                state.est_ahead_mid = max(state.est_ahead_mid - delta, 0.0)
                state.est_ahead_high = max(state.est_ahead_high - delta, 0.0)
                state.last_update = time.time()
                self._update_metrics(state)

            logger.debug(
                "queue_consumed",
                token_id=token_id,
                price=price,
                delta=delta,
                affected_orders=len(matching_orders),
            )
        elif delta < 0:
            # Size increased (new orders behind us or ahead of us)
            # Conservative: assume some might have jumped ahead
            for state in matching_orders:
                # Increase est_ahead_high to account for possible queue-jumping
                state.est_ahead_high = state.est_ahead_high + abs(delta) * 0.2
                state.last_update = time.time()
                self._update_metrics(state)

            logger.debug(
                "queue_expanded",
                token_id=token_id,
                price=price,
                delta=abs(delta),
                affected_orders=len(matching_orders),
            )

    def update_from_fill(self, order_id: str, fill_size: float) -> None:
        """Update queue state after a fill event.

        Args:
            order_id: order that was filled
            fill_size: size of the fill
        """
        state = self.states.get(order_id)
        if not state:
            logger.warning("queue_fill_unknown_order", order_id=order_id)
            return

        # Reduce size_open
        state.size_open -= fill_size
        state.size_open = max(state.size_open, 0.0)

        # We got filled, so nothing ahead now
        state.est_ahead_low = 0.0
        state.est_ahead_mid = 0.0
        state.est_ahead_high = 0.0
        state.last_update = time.time()

        self._update_metrics(state)

        logger.info(
            "queue_fill_updated",
            order_id=order_id,
            fill_size=fill_size,
            size_remaining=state.size_open,
        )

        # Remove if fully filled
        if state.size_open <= 0:
            self.remove_order(order_id)

    def remove_order(self, order_id: str) -> None:
        """Remove order from queue tracking.

        Args:
            order_id: order to remove
        """
        if order_id in self.states:
            del self.states[order_id]
            logger.info("queue_order_removed", order_id=order_id)

    def recompute_metrics(self) -> None:
        """Recompute fill probability, ETA, and uncertainty for all orders.

        Should be called on the 250ms fast loop.
        """
        for state in self.states.values():
            self._update_metrics(state)

    def _update_metrics(self, state: QueueState) -> None:
        """Update derived metrics for a queue state.

        Args:
            state: queue state to update
        """
        depletion_rate = self.hazard.get_depletion_rate(state.token_id)

        # Fill probability over 30 second horizon
        state.fill_prob_30s = self.hazard.fill_probability(
            queue_ahead=state.est_ahead_mid,
            order_size=state.size_open,
            horizon_sec=30.0,
            depletion_rate=depletion_rate,
        )

        # ETA to fill
        state.eta_sec = self.hazard.eta(
            queue_ahead=state.est_ahead_mid,
            order_size=state.size_open,
            depletion_rate=depletion_rate,
        )

        # Queue uncertainty
        state.queue_uncertainty = self.chi * (state.est_ahead_high - state.est_ahead_low)

    async def persist(self, db: Database) -> None:
        """Persist all queue states to database.

        Args:
            db: database connection
        """
        if not self.states:
            return

        # Build insert values
        rows = []
        for state in self.states.values():
            rows.append(
                (
                    state.order_id,
                    state.token_id,
                    state.condition_id,
                    state.side,
                    state.price,
                    state.size_open,
                    state.est_ahead_low,
                    state.est_ahead_mid,
                    state.est_ahead_high,
                    state.eta_sec,
                    state.fill_prob_30s,
                    state.queue_uncertainty,
                    int(state.is_scoring),
                    state.entry_time,
                    state.last_update,
                )
            )

        # Insert/replace into queue_state table
        await db.executemany(
            """
            INSERT OR REPLACE INTO queue_state (
                order_id, token_id, condition_id, side, price, size_open,
                est_ahead_low, est_ahead_mid, est_ahead_high,
                eta_sec, fill_prob_30s, queue_uncertainty,
                is_scoring, entry_time, last_update
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

        logger.debug("queue_states_persisted", count=len(rows))

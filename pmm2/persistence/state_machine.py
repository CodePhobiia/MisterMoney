"""Order persistence state machine.

Tracks the lifecycle of live orders through states:
NEW → WARMING → SCORING → ENTRENCHED → STALE → EXIT
"""

from __future__ import annotations

import time
from enum import StrEnum

from pydantic import BaseModel, Field


class OrderPersistenceState(StrEnum):
    """Order lifecycle states."""

    NEW = "NEW"  # just posted
    WARMING = "WARMING"  # live, approaching scoring eligibility
    SCORING = "SCORING"  # confirmed scoring via /order-scoring
    ENTRENCHED = "ENTRENCHED"  # low queue ahead + strong hold EV
    STALE = "STALE"  # fair value drifted or toxicity spiked
    EXIT = "EXIT"  # cancel or cross immediately


class PersistenceOrder(BaseModel):
    """Tracks persistence state for a live order."""

    order_id: str
    condition_id: str = ""
    token_id: str = ""
    side: str = ""  # BUY or SELL
    price: float = 0.0
    size: float = 0.0

    state: OrderPersistenceState = OrderPersistenceState.NEW
    state_entered_at: float = Field(default_factory=time.time)

    # Queue info (synced from QueueEstimator)
    est_ahead: float = 0.0
    eta_sec: float = 0.0
    fill_prob_30s: float = 0.0

    # Scoring
    is_scoring: bool = False
    scoring_checked_at: float = 0.0

    # EV
    hold_ev: float = 0.0
    best_action: str = "HOLD"
    best_action_ev: float = 0.0

    @property
    def age_sec(self) -> float:
        """Time since entering current state."""
        return time.time() - self.state_entered_at


class StateMachine:
    """Manages persistence state transitions."""

    def __init__(self) -> None:
        self.orders: dict[str, PersistenceOrder] = {}

    def add_order(
        self,
        order_id: str,
        condition_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> PersistenceOrder:
        """Register a new order in NEW state."""
        order = PersistenceOrder(
            order_id=order_id,
            condition_id=condition_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            state=OrderPersistenceState.NEW,
        )
        self.orders[order_id] = order
        return order

    def remove_order(self, order_id: str) -> PersistenceOrder | None:
        """Remove order (filled or canceled)."""
        return self.orders.pop(order_id, None)

    def get_order(self, order_id: str) -> PersistenceOrder | None:
        """Get order by ID."""
        return self.orders.get(order_id)

    def update_scoring(self, order_id: str, is_scoring: bool) -> None:
        """Update scoring status from /order-scoring endpoint.

        Transitions:
        - NEW/WARMING → SCORING if is_scoring
        - SCORING → WARMING if not is_scoring (lost scoring)
        """
        order = self.orders.get(order_id)
        if not order:
            return

        order.is_scoring = is_scoring
        order.scoring_checked_at = time.time()

        # Transition to SCORING if we just started scoring
        if is_scoring and order.state in (
            OrderPersistenceState.NEW,
            OrderPersistenceState.WARMING,
        ):
            self.transition(order_id, OrderPersistenceState.SCORING)

        # Drop back to WARMING if we lost scoring
        elif not is_scoring and order.state == OrderPersistenceState.SCORING:
            self.transition(order_id, OrderPersistenceState.WARMING)

    def update_queue(
        self, order_id: str, est_ahead: float, eta_sec: float, fill_prob: float
    ) -> None:
        """Sync queue info from QueueEstimator.

        Transitions:
        - SCORING → ENTRENCHED if eta < 15s and fill_prob > 0.5
        """
        order = self.orders.get(order_id)
        if not order:
            return

        order.est_ahead = est_ahead
        order.eta_sec = eta_sec
        order.fill_prob_30s = fill_prob

        # Become entrenched if we're scoring AND close to filling
        if (
            order.state == OrderPersistenceState.SCORING
            and eta_sec < 15.0
            and fill_prob > 0.5
        ):
            self.transition(order_id, OrderPersistenceState.ENTRENCHED)

    def check_staleness(
        self,
        order_id: str,
        target_price: float,
        tick_size: float = 0.01,
        stale_ticks: int = 2,
        force_cancel_ticks: int = 4,
    ) -> None:
        """Check if fair value has drifted enough to mark STALE/EXIT.

        Drift = |current_price - target_price| / tick_size

        Transitions:
        - ANY → STALE if drift >= stale_ticks
        - STALE → EXIT if drift >= force_cancel_ticks
        """
        order = self.orders.get(order_id)
        if not order:
            return

        drift_ticks = abs(order.price - target_price) / tick_size

        # Force cancel if drift is extreme
        if drift_ticks >= force_cancel_ticks:
            if order.state != OrderPersistenceState.EXIT:
                self.transition(order_id, OrderPersistenceState.EXIT)

        # Mark stale if drift is significant
        elif drift_ticks >= stale_ticks:
            if order.state not in (OrderPersistenceState.STALE, OrderPersistenceState.EXIT):
                self.transition(order_id, OrderPersistenceState.STALE)

    def transition(self, order_id: str, new_state: OrderPersistenceState) -> None:
        """Force a state transition (resets state_entered_at)."""
        order = self.orders.get(order_id)
        if not order:
            return

        order.state = new_state
        order.state_entered_at = time.time()

"""Queue state data model for tracking order queue positions."""

from __future__ import annotations

import time

from pydantic import BaseModel, Field


class QueueState(BaseModel):
    """Per-order queue position estimate."""

    order_id: str
    condition_id: str = ""
    token_id: str = ""
    side: str = ""  # BUY or SELL
    price: float = 0.0
    size_open: float = 0.0

    # Queue position estimates
    est_ahead_low: float = 0.0  # optimistic (less queue ahead)
    est_ahead_mid: float = 0.0  # midpoint estimate
    est_ahead_high: float = 0.0  # pessimistic (more queue ahead)

    # Derived metrics
    eta_sec: float = 0.0  # estimated time to fill
    fill_prob_30s: float = 0.0  # P(fill) in next 30 seconds
    queue_uncertainty: float = 0.0  # chi * (high - low)

    # Status
    is_scoring: bool = False
    entry_time: float = Field(default_factory=time.time)
    last_update: float = Field(default_factory=time.time)

    @property
    def age_sec(self) -> float:
        """Age of the order in seconds since entry."""
        return time.time() - self.entry_time

    @property
    def est_ahead(self) -> float:
        """Midpoint estimate of queue ahead."""
        return self.est_ahead_mid

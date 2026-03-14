"""Records fill outcomes when markets resolve -- not at fill time.

Paper 2 S5: Edge validation requires ACTUAL binary outcomes
(did YES happen?), not trade direction (did we buy?).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pmm1.analytics.edge_tracker import EdgeTracker
    from pmm1.analytics.fv_calibrator import FairValueCalibrator

logger = structlog.get_logger(__name__)


@dataclass
class PendingFill:
    """A fill awaiting market resolution for edge tracking."""

    condition_id: str
    predicted_p: float  # mid at fill time
    market_p: float  # fill price
    pnl: float
    side: str
    timestamp: float = field(default_factory=time.time)


class ResolutionRecorder:
    """Bridges fill-time predictions to resolution-time edge tracking.

    Problem (F01): The fill handler was passing ``outcome=1.0 if side ==
    "BUY" else 0.0``, which encodes the *trade direction* instead of
    the actual market resolution.  This corrupts every downstream metric
    that depends on ``outcome``: SPRT decisions, Brier scores, and
    ``edge_confidence``.

    Fix: fills are buffered here at fill time.  When a market resolves
    (via ``on_market_resolved``), all pending fills for that market are
    flushed to the edge tracker / FV calibrator with the **true**
    binary outcome.
    """

    def __init__(
        self,
        edge_tracker: EdgeTracker | None,
        fv_calibrator: FairValueCalibrator | None,
    ) -> None:
        self._edge_tracker = edge_tracker
        self._fv_calibrator = fv_calibrator
        self._pending: dict[str, list[PendingFill]] = {}

    def record_fill(
        self,
        condition_id: str,
        predicted_p: float,
        market_p: float,
        pnl: float,
        side: str,
    ) -> None:
        """Store a fill for later resolution recording."""
        fill = PendingFill(
            condition_id=condition_id,
            predicted_p=predicted_p,
            market_p=market_p,
            pnl=pnl,
            side=side,
        )
        self._pending.setdefault(condition_id, []).append(fill)

    def on_market_resolved(self, condition_id: str, outcome: float) -> None:
        """Called when a market resolves.  Records all pending fills with actual outcome."""
        fills = self._pending.pop(condition_id, [])
        for fill in fills:
            if self._edge_tracker is not None:
                self._edge_tracker.record_trade(
                    predicted_p=fill.predicted_p,
                    market_p=fill.market_p,
                    outcome=outcome,  # ACTUAL resolution, not trade side
                    pnl=fill.pnl,
                    side=fill.side,
                    condition_id=fill.condition_id,
                )
            if self._fv_calibrator is not None:
                self._fv_calibrator.record_sample(
                    predicted_p=fill.predicted_p,
                    market_p=fill.market_p,
                    outcome=outcome,
                )
        if fills:
            logger.info(
                "resolution_recorded",
                condition_id=condition_id[:16],
                outcome=outcome,
                fills_recorded=len(fills),
            )

    @property
    def pending_count(self) -> int:
        """Total number of fills awaiting resolution."""
        return sum(len(v) for v in self._pending.values())

    @property
    def pending_markets(self) -> int:
        """Number of distinct markets with pending fills."""
        return len(self._pending)

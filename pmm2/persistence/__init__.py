"""Persistence optimizer for live order management.

The persistence optimizer decides whether to keep orders as-is (HOLD) or take action
(IMPROVE, WIDEN, CANCEL, CROSS) based on EV calculations with hysteresis.

Key components:
- StateMachine: Tracks order lifecycle (NEW → WARMING → SCORING → ENTRENCHED → STALE → EXIT)
- ActionEVCalculator: Computes expected value for 7 possible actions
- HysteresisGate: Prevents unnecessary moves unless improvement exceeds threshold
- WarmupEstimator: Calculates cost of losing scoring progress
- PersistenceOptimizer: Main decision engine

Philosophy: HOLD is the default. Moving orders is expensive (queue loss + warmup reset).
Only move when the math is CLEARLY better.
"""

from pmm2.persistence.action_ev import ActionEVCalculator, PersistenceAction
from pmm2.persistence.hysteresis import HysteresisConfig, HysteresisGate
from pmm2.persistence.optimizer import PersistenceOptimizer
from pmm2.persistence.state_machine import (
    OrderPersistenceState,
    PersistenceOrder,
    StateMachine,
)
from pmm2.persistence.warmup import WarmupEstimator

__all__ = [
    "ActionEVCalculator",
    "HysteresisConfig",
    "HysteresisGate",
    "OrderPersistenceState",
    "PersistenceAction",
    "PersistenceOptimizer",
    "PersistenceOrder",
    "StateMachine",
    "WarmupEstimator",
]

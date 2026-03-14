"""Capital allocator — discrete greedy bundle selection with constraints + hysteresis.

S6: Discrete Capital Allocator
- Adjusted scoring with penalties (corr, churn, queue, inventory)
- Constraint checking (capital, slots, per-market, per-event)
- Greedy allocation (sorted by adjusted return)
- Reallocation hysteresis (prevent thrashing)
- Circuit breaker (toxic market detection)
"""

from pmm2.allocator.allocator import CapitalAllocator as CapitalAllocator
from pmm2.allocator.circuit_breaker import CircuitBreaker, MarketCircuitState
from pmm2.allocator.constraints import AllocationConstraints, ConstraintChecker
from pmm2.allocator.greedy import AllocationPlan as AllocationPlan
from pmm2.allocator.greedy import GreedyAllocator
from pmm2.allocator.hysteresis import MarketAllocationState, ReallocationHysteresis
from pmm2.allocator.scoring import AdjustedScore, AdjustedScorer

__all__ = [
    "AdjustedScore",
    "AdjustedScorer",
    "AllocationConstraints",
    "ConstraintChecker",
    "AllocationPlan",
    "GreedyAllocator",
    "MarketAllocationState",
    "ReallocationHysteresis",
    "MarketCircuitState",
    "CircuitBreaker",
    "CapitalAllocator",
]

# Sprint 5 Summary: Persistence Optimizer

**Status:** ✅ Complete  
**Committed:** 766c36e (bundled with Sprint 4)  
**Date:** 2026-03-07

## Deliverables

All 5 modules implemented in `pmm2/persistence/`:

### S5-1: State Machine (`state_machine.py`)
- **OrderPersistenceState** enum: NEW → WARMING → SCORING → ENTRENCHED → STALE → EXIT
- **PersistenceOrder** model tracking queue position, scoring status, and cached EV
- **StateMachine** class managing order lifecycle and state transitions
- Transitions driven by scoring checks, queue updates, and staleness checks

### S5-2: Action EV Calculator (`action_ev.py`)
- **PersistenceAction** enum: HOLD, IMPROVE1/2, WIDEN1/2, CANCEL, CROSS
- **ActionEVCalculator** computes expected value for each action
- EV model: `EV = P^fill * Edge * Q + E^liq + E^reb - C^tox - ResetCost`
- `enumerate_and_score()` returns {action: EV} dict for all 7 actions
- Accounts for queue position, warmup loss, and reset costs

### S5-3: Hysteresis Layer (`hysteresis.py`)
- **HysteresisConfig** with tunable threshold components
- **HysteresisGate** prevents unnecessary moves unless improvement exceeds threshold
- Dynamic threshold: `ξ = ξ₀ + ξ₁·𝟙(scoring) + ξ₂·𝟙(ETA<15s) + ξ₃·|skew|`
- Higher bar for scoring orders (avoid warmup loss) and near-fill orders (avoid queue loss)

### S5-4: Warmup Estimator (`warmup.py`)
- **WarmupEstimator** tracks progress toward scoring eligibility
- Computes opportunity cost of resetting orders that are near-scoring
- Loss scales with progress: resetting a 90%-warmed order >> 10%-warmed order
- Warmup period defaults to 60 seconds (configurable)

### S5-5: Persistence Optimizer (`optimizer.py`)
- **PersistenceOptimizer** main decision engine
- `decide(order_id, ...)` returns `(action, ev)` tuple
- `decide_all(live_orders, ...)` batch processes multiple orders
- HOLD is default unless improvement clears hysteresis threshold
- Updates order's cached EV values for monitoring/logging

## Philosophy

**HOLD is the overwhelming default.** Moving orders is expensive:
- **Queue loss:** Lose time priority, go to back of line
- **Warmup reset:** Lose progress toward scoring eligibility
- **Transaction costs:** Cancel fees, API overhead

The hysteresis layer ensures we **only move when the math is CLEARLY better**, not just marginally better.

## Integration

- ✅ Imports from `pmm2.queue.state` (QueueState) and `pmm2.queue.hazard` (FillHazard)
- ✅ Can import `pmm2.scorer.bundles.QuoteBundle` for types (but doesn't depend on scorer logic)
- ✅ All modules tested with integration test
- ✅ No modifications to `pmm1/` (clean separation)

## Testing

```python
from pmm2.persistence import PersistenceOptimizer, StateMachine, ActionEVCalculator, HysteresisGate, WarmupEstimator
from pmm2.queue.hazard import FillHazard

# Initialize components
fill_hazard = FillHazard()
state_machine = StateMachine()
action_calculator = ActionEVCalculator(fill_hazard)
hysteresis = HysteresisGate()
warmup = WarmupEstimator()

# Create optimizer
optimizer = PersistenceOptimizer(state_machine, action_calculator, hysteresis, warmup)

# Add order
order = state_machine.add_order("order_1", "condition_123", "token_yes", "BUY", 0.48, 100.0)

# Make decision
action, ev = optimizer.decide(
    order_id="order_1",
    reservation_price=0.52,
    target_price=0.50,
    depletion_rate=2.0,
)
print(f"Decision: {action} with EV=${ev:.4f}")
```

## Next Steps

Sprint 5 is complete. The persistence optimizer is ready for integration into the live trading loop.

**Sprint 6** (next): Market selection and capital allocation — decides which markets to quote and how much capital to deploy.

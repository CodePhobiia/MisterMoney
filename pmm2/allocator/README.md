# Sprint 6: Discrete Capital Allocator

Complete implementation of discrete capital allocation system for MisterMoney Polymarket bot.

## Overview

The allocator implements greedy bundle selection with:
- **Adjusted scoring** with penalties (correlation, churn, queue, inventory)
- **Multi-constraint checking** (capital, slots, per-market, per-event)
- **Reallocation hysteresis** to prevent thrashing
- **Circuit breaker** for toxic market detection

## Scale-Aware Design

Bot NAV: **$104**

Constraints:
- **Active cap**: $31.20 (30% of NAV)
- **Per-market cap**: $8.00 (max of 3% NAV or $8 floor)
- **Per-event cap**: $10.40 (10% of NAV)
- **Total slots**: 48 (16 markets × 3 bundle levels)
- **Hysteresis min**: $5.00 (prevents $500 threshold degeneracy)

Expected allocation: **3-4 markets** at full scale.

## Modules

### `scoring.py` — Adjusted Score Computation

Applies penalties to raw marginal returns:

```python
R̃ = R - λ·CorrPenalty - φ·ChurnPenalty - ψ·QueueUncertainty - μ·InventoryPenalty
```

**Penalty coefficients** (in bps):
- `corr_lambda`: 20 bps per correlated event position
- `churn_phi`: 15 bps for entering/exiting a market
- `queue_psi`: 10 bps per unit queue uncertainty
- `inventory_mu`: 5 bps per unit directional exposure

### `constraints.py` — Constraint Checker

Validates allocation decisions against:
1. **Total capital**: current + bundle ≤ active_cap_frac × NAV (30%)
2. **Total slots**: current + bundle ≤ total_slots (48)
3. **Per-market**: market_capital + bundle ≤ max(NAV × 3%, $8)
4. **Per-event**: event_capital + bundle ≤ NAV × 10%
5. **Nested rule**: B2 only if B1 funded, B3 only if B2 funded

### `greedy.py` — Greedy Allocator

Discrete bundle selection:
1. Filter to positive adjusted return > 6 bps
2. Sort by adjusted_return descending
3. Greedily assign bundles while respecting constraints
4. Track skipped bundles with reasons

### `hysteresis.py` — Reallocation Hysteresis

Prevents allocator thrashing:
- **Delta threshold**: |ΔCap| > max(10% × Cap, $5)
- **Rank persistence**: must persist for 3 cycles
- **Override exceptions**: inventory breach, reward change, resolved, arb

### `circuit_breaker.py` — Circuit Breaker

Fast-path toxic market detection:
- Trip if `markout_1s > 3× historical average`
- **Cooldown**: 5 minutes before re-entering
- Signals persistence optimizer to EXIT

### `allocator.py` — Top-Level Orchestrator

Ties everything together:
```python
allocator = CapitalAllocator(nav=104.0)

plan = await allocator.run_allocation_cycle(
    scored_bundles=bundles,
    current_markets=current_positions,
    event_clusters=event_map,
)

# plan.funded_bundles → list of QuoteBundle
# plan.total_capital_used → float
# plan.markets_funded → int
```

## Demo

Run the demo:
```bash
python3 pmm2/allocator/demo.py
```

Expected output:
```
✅ Allocation complete!
  - Funded bundles: 3
  - Markets funded: 3
  - Capital used: $24.00
  - Slots used: 6 / 48
  - Capital utilization: 76.9%

📋 Funded bundles:
  1. market_1 B1: $8.00 @ 200.0 bps
  2. market_3 B1: $8.00 @ 90.0 bps
  3. market_4 B1: $8.00 @ 50.0 bps
```

## Integration

The allocator integrates with existing PMM-2 modules:

**Input**: `MarketEVScorer.score_market()` → `list[QuoteBundle]`
**Output**: `AllocationPlan` with funded bundles
**Persistence**: `allocator.persist_decisions(db, plan)`

## Testing

Import test:
```bash
python3 -c "from pmm2.allocator import CapitalAllocator; print('✓')"
```

All imports successful. Scale-aware defaults verified.

## Design Decisions

1. **Penalties in bps**: All penalty coefficients use basis points (0.0015 = 15 bps) to match marginal_return units
2. **Per-event cap 10%**: Increased from 6% to allow $8 floor to work at $104 NAV
3. **Hysteresis new entry**: Allow initial market entry without history requirement
4. **Nested bundle rule**: Enforced in constraint checker (B1 → B2 → B3)
5. **Min positive return**: 6 bps threshold after penalties

## Future Work

- [ ] Dynamic penalty tuning based on market conditions
- [ ] Portfolio optimization (mean-variance)
- [ ] Event correlation matrix from historical data
- [ ] Adaptive hysteresis thresholds
- [ ] Multi-objective optimization (Sharpe, Sortino, max drawdown)

---

**Status**: ✅ Complete
**Commit**: `3909ba9` (fix allocator scale-aware defaults)
**Lines**: 1,353 lines across 7 modules
**Test**: All imports successful, demo verified

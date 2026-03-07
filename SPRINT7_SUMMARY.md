# Sprint 7 Summary: Quote Planner + Runtime Integration

**Status:** ✅ Complete  
**Commit:** 67d77d6  
**Date:** 2026-03-07

## What Was Built

### 1. Configuration (`pmm2/config.py`)
- `PMM2Config` — Pydantic model with all PMM-2 parameters
- `load_pmm2_config()` — Load from YAML `pmm2:` section
- Default: `enabled: false`, `shadow_mode: true`
- Parameters:
  - Loop cadences (allocator 60s, medium 10s, fast 250ms, slow 5min)
  - Limits (max 12 markets, 48 slots)
  - Allocator scoring (min return 6bps, correlation penalty 0.20, churn 0.15)
  - Persistence (hysteresis $0.25 base, max 2 reprices/min)
  - Risk (3% per market, 6% per event, 30% total active)

### 2. Quote Planner (`pmm2/planner/`)

#### `quote_planner.py`
- `QuoteLadderRung` — Single order (token, side, price, size, intent)
- `TargetQuotePlan` — Complete ladder for a market
- `QuotePlanner` — Convert QuoteBundles → concrete quote plans
  - Each bundle → 2 rungs (bid + ask)
  - B1 at inside, B2 one tick deeper, B3 two ticks deeper
  - Handles neg-risk token routing
  - Rate-limits repricing (default 3/minute)

#### `diff_engine.py`
- `OrderMutation` — Single order change (add/cancel/amend)
- `DiffEngine` — Compare target vs live orders
  - Match orders within tick tolerance
  - Respect persistence optimizer decisions (HOLD/ENTRENCHED)
  - Generate minimal mutation set
  - Never cancel SCORING orders unless approved

### 3. Runtime Bridge (`pmm2/runtime/`)

#### `v1_bridge.py`
- `V1Bridge` — Execute mutations through V1 order manager
  - Shadow mode: log only (default)
  - Live mode: delegate to V1's order manager
  - NEVER bypasses V1 risk limits or heartbeat
  - Mutation log for auditing
  - Placeholder methods for actual V1 integration

#### `loops.py`
- `PMM2Runtime` — Main PMM-2 brain with 5 concurrent loops:

**Event-driven (WS callbacks):**
- `on_book_delta()` → queue estimator update
- `on_fill()` → queue + circuit breaker
- `on_order_live()` → initialize queue + persistence state
- `on_order_canceled()` → cleanup

**Fast loop (250ms):**
- Recompute queue states, ETAs, fill probabilities
- Sync to persistence state machine

**Medium loop (10s):**
- Refresh market EV components
- Update bundle values
- Persist queue states

**Allocator loop (60s):**
- Score all markets
- Run capital allocator
- Generate quote plans
- Diff vs live orders
- Execute mutations (rate-limited)
- Persist decisions

**Slow loop (5min):**
- Refresh enriched universe
- Update NAV
- Calibrate depletion rates (TODO)

#### `integration.py`
- `maybe_init_pmm2()` — Initialize if enabled in config
- `pmm2_on_book_delta()` — Forward WS events
- `pmm2_on_fill()` — Forward fills
- `pmm2_on_order_live()` — Forward order confirmations
- `pmm2_on_order_canceled()` — Forward cancels

### 4. Config Update (`config/default.yaml`)
Added `pmm2:` section with all parameters (disabled by default).

### 5. Tests (`test_sprint7.py`)
Unit tests for all components:
- Config loading
- Quote planner (ladder generation, rate limiting)
- Diff engine (mutation generation)
- V1 bridge (shadow mode execution)
- Runtime initialization

**All tests pass ✓**

## Integration Points (Not Implemented Yet)

Main.py will need to:

```python
from pmm2.runtime import (
    maybe_init_pmm2,
    pmm2_on_book_delta,
    pmm2_on_fill,
    pmm2_on_order_live,
    pmm2_on_order_canceled,
)

# At startup:
pmm2_runtime = await maybe_init_pmm2(settings, db, bot_state)

# In book WS handler:
pmm2_on_book_delta(pmm2_runtime, token_id, price, old_size, new_size)

# In fill WS handler:
pmm2_on_fill(pmm2_runtime, order_id, fill_size, fill_price)

# When order goes live:
pmm2_on_order_live(pmm2_runtime, order_id, token_id, side, price, size, book_depth)

# When order canceled:
pmm2_on_order_canceled(pmm2_runtime, order_id)
```

## Key Features

### Safety First
- **Shadow mode by default** — logs all decisions, executes nothing
- **NEVER bypasses V1** — delegates to V1's order manager
- **Rate limiting** — max 2-3 reprices per minute per market
- **Resilient loops** — catch all exceptions, continue running

### Smart Execution
- **Minimal churn** — diff engine matches within tick tolerance
- **Persistence-aware** — respects HOLD/ENTRENCHED/SCORING states
- **Event-driven** — real-time queue updates from WS deltas
- **Multi-cadence** — fast queue updates, medium EV refresh, slow universe

### Production Ready
- Structured logging (structlog)
- Type-safe (pydantic, type hints)
- Async throughout
- Database persistence
- Mutation audit log

## What's NOT Done

1. **Main.py integration** — caller needs to add the 5 function calls
2. **V1 order manager bridge** — placeholder methods need real implementation
3. **Depletion rate calibration** — TODO in slow loop
4. **Circuit breaker fill recording** — needs markout calculation
5. **Rebate reconciliation** — Sprint 8 (daily loop)

## How to Enable

1. Edit `config/default.yaml`:
   ```yaml
   pmm2:
     enabled: true
     shadow_mode: true  # Keep true for testing
     live_capital_pct: 0.0  # Increase to go live (0.1 = 10%)
   ```

2. Add integration calls to `pmm1/main.py` (see above)

3. Start bot — PMM-2 will run in shadow mode

4. Check logs for `pmm2_shadow_mutation` entries

5. When confident, increase `live_capital_pct` (start with 0.1 = 10%)

## Testing

```bash
# Run unit tests
python3 test_sprint7.py

# Test imports
python3 -c "from pmm2.runtime.loops import PMM2Runtime; from pmm2.planner import QuotePlanner; print('OK')"
```

## Next Steps (Sprint 8)

1. Integrate with main.py (add the 5 function calls)
2. Implement real V1 order manager bridge
3. Add rebate reconciliation (daily loop)
4. Shadow-mode testing with live bot
5. Gradual rollout (10% → 50% → 100%)

---

**Sprint 7 Complete ✓**  
All PMM-2 modules built, tested, and ready for integration.

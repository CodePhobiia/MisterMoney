# V3 Sprint 6 Report: Calibration & Signal Serving

**Date:** 2026-03-07  
**Status:** ✅ Complete  
**Commit:** 6290d41

## Summary

Built the calibration layer and signal serving infrastructure for MisterMoney V3. This includes route-specific calibrators with learnable weights, signal decay functions, and publisher/consumer interfaces for V2 integration.

## Deliverables

### ✅ S6-T1: Route Calibrator Models
**Files:** `v3/calibration/route_models.py`, `v3/calibration/__init__.py`  
**Lines:** 407

- **RouteCalibrator class** — Per-route calibration with:
  - 7-feature vector: logit(raw_p), logit(market_mid), uncertainty, evidence_count, source_reliability_avg, hours_to_resolution, volume_24h_log
  - Cold start strategy: beta = [0, 1, 0, 0, 0, 0, 0] (pass through market prior)
  - Logistic regression training via gradient descent
  - Conformal prediction intervals (wide in cold start: ±0.20)
  - Save/load to JSON

- **CalibrationManager class** — Manages all 4 route calibrators:
  - Auto-loads calibrators from disk on init
  - `retrain_all()` — queries resolved markets from DB and retrains each route
  - Returns per-route training stats (beta weights, log loss, mean residual)

**Cold start behavior:** With <50 resolved markets, calibrator passes through market prior conservatively. Gradually transitions to learned weights as data accumulates.

### ✅ S6-T2: Signal Decay
**File:** `v3/calibration/decay.py`  
**Lines:** 92

- **decay_signal()** — Exponential decay toward market consensus:
  ```
  p_live = λ(age) * p_raw + (1 - λ(age)) * market_mid
  λ = exp(-age / half_life) * exp(-staleness / (2 * half_life))
  ```
  
  Route-specific half-lives:
  - `numeric`: 60s (fast-moving data)
  - `simple`: 900s (15 min)
  - `rule`: 1800s (30 min)
  - `dossier`: 7200s (2 hours)

- **is_signal_expired()** — Returns True when decay factor < 0.05 (≈3x half-life)

### ✅ S6-T3: Cold Start Strategy
**Integrated into:** `v3/calibration/route_models.py`

When `resolved_market_count < 50`:
- Beta = [0, 1, 0, 0, 0, 0, 0] — passes through market prior only
- Conformal intervals are wide (±0.20)
- Conservative stance: `hurdle_met` rarely True
- Gradual transition: At 50+ markets, switches to learned weights via gradient descent

### ✅ S6-T4: Signal Publisher
**Files:** `v3/serving/publisher.py`, `v3/serving/__init__.py`  
**Lines:** 216

- **SignalPublisher class** — Dual-writes to Postgres + Redis:
  - `publish()` — INSERT to `fair_value_signals` table, SET in Redis with route-specific TTL
  - `get_latest()` — Fast path: Redis lookup, fallback to DB query
  - `get_cached_or_neutral()` — Returns cached signal or neutral (p=0.5, hurdle_met=False)
  
  Redis TTLs per route:
  - `numeric`: 300s (5 min)
  - `simple`: 1800s (30 min)
  - `rule`: 3600s (1 hour)
  - `dossier`: 14400s (4 hours)

### ✅ S6-T5: V2 Consumer Integration
**File:** `v3/serving/consumer.py`  
**Lines:** 128

- **V3Consumer class** — V2 scorer interface:
  - `get_fair_value()` — Returns `p_calibrated` if:
    1. Signal exists and not expired
    2. `hurdle_met == True`
    3. `uncertainty < 0.30`
    
    Otherwise returns `None` (V2 uses book midpoint as before)
    
  - `get_signal_detail()` — Full signal metadata for dashboard/logging

## Testing

**File:** `v3/calibration/test_calibration.py` (360 lines)

All 7 integration tests passed:

1. ✅ **RouteCalibrator cold start calibration** — Verified beta=[0,1,0,0,0,0,0] passes through market_mid
2. ✅ **Conformal intervals (cold start)** — Verified ±0.20 wide intervals
3. ✅ **Signal decay** — Verified exponential decay math (t=0, t=half_life, t=5*half_life)
4. ✅ **Signal expiration** — Verified each route's expiry threshold
   - numeric: 198s (3.3 min)
   - simple: 2966s (49.4 min)
   - rule: 5932s (98.9 min)
   - dossier: 23726s (395.4 min)
5. ✅ **SignalPublisher write/read** — Verified Postgres + Redis roundtrip
6. ✅ **V3Consumer scenarios** — Tested valid, no_hurdle, high_uncertainty, expired signals
7. ✅ **CalibrationManager retrain** — Verified cold start (30 markets) and trained (100 markets) behavior

## Metrics

- **Total lines of code:** 1,203 (excluding tests: 843)
- **Files created:** 7
  - `v3/calibration/__init__.py`
  - `v3/calibration/route_models.py`
  - `v3/calibration/decay.py`
  - `v3/calibration/test_calibration.py`
  - `v3/serving/__init__.py`
  - `v3/serving/publisher.py`
  - `v3/serving/consumer.py`

## Dependencies

- ✅ `redis` package installed (redis.asyncio)
- ✅ Uses existing `v3.evidence.db.Database`
- ✅ Uses existing `v3.evidence.entities.FairValueSignal`
- ✅ Postgres table `fair_value_signals` already exists in schema
- ✅ Redis 7.0.15 running on localhost:6379

## Integration Points

**For V2 integration:**
```python
from v3.serving import SignalPublisher, V3Consumer
from v3.evidence.db import Database

db = Database("postgresql://mmbot:mmbot_v3_2026@localhost/mistermoney_v3")
await db.connect()

publisher = SignalPublisher(db)
await publisher.connect()

consumer = V3Consumer(publisher)

# In V2 scorer:
fair_value = await consumer.get_fair_value(condition_id)
if fair_value is not None:
    # Use V3 signal
    ...
else:
    # Fall back to book midpoint
    ...
```

## Next Steps (Sprint 7+)

1. **Resolved markets table** — Create schema for storing resolved market outcomes (needed for `CalibrationManager.retrain_all()`)
2. **Retraining cron** — Schedule periodic calibrator retraining (e.g., daily)
3. **V2 scorer integration** — Wire up V3Consumer in existing V2 codebase
4. **Calibration dashboard** — Visualize beta weights, calibration stats, signal freshness
5. **Source reliability scoring** — Compute `source_reliability_avg` from evidence metadata

## Notes

- Cold start strategy is intentionally conservative — requires 50+ resolved markets before learning from data
- Gradient descent is simple (no regularization) but sufficient for 7 features
- Signal decay is route-aware — numeric signals decay much faster than dossier (60s vs 2 hours)
- Redis acts as fast cache; Postgres is source of truth
- All tests use live Postgres/Redis (not mocked) for realistic integration testing

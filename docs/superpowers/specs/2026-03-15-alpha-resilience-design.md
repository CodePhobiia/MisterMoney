# PMM-1 Alpha + Resilience Improvements

**Date:** 2026-03-15
**Status:** Approved
**Scope:** 4 targeted improvements to trading performance and operational resilience

## Goal

Increase per-trade profitability by 5-15 bps and eliminate false-positive shutdown events, through 4 self-contained changes to existing modules. No new modules, no new wiring paths, no architectural changes.

## Architecture Principle

Each change is a self-contained addition that improves an existing module. Data flow stays identical — fills feed analytics, analytics feed pricing, pricing feeds orders.

| Change | Where | What |
|--------|-------|------|
| Staged recovery | risk/kill_switch.py + main.py | 4-stage mismatch ladder replaces binary kill |
| Adaptive gamma | analytics/spread_optimizer.py + strategy/quote_engine.py | Gamma buckets added to Thompson sampling |
| Toxicity pause | settings.py + main.py | VPIN > threshold suppresses quotes per-market |
| Dynamic exits | strategy/exit_manager.py + math/kelly.py | Kelly growth rate replaces fixed TP/SL thresholds |

---

## 1. Staged Recovery Protocol

### Problem

3 reconciliation mismatches → kill switch → FLATTEN_ONLY → stuck until manual restart. A 15-minute exchange hiccup kills the whole session.

### Design

The kill switch gets a per-market `mismatch_stage` (0-3) instead of a binary trigger:

- **Stage 0** (1st mismatch): Log warning + immediate re-reconcile. Keep quoting all markets.
- **Stage 1** (2nd mismatch): Skip the problem market for 30s. Quote other markets normally.
- **Stage 2** (3rd mismatch): Problem market enters read-only (no orders). Others quote normally.
- **Stage 3** (4th+ mismatch): FLATTEN_ONLY — but auto-retry every 5 minutes. If retry succeeds, drop back to Stage 0. If 3 retries fail, emit CRITICAL alert and stay flat.

### Rules

- Stages track per-market, not globally. Market A at Stage 2 doesn't affect Market B.
- Stage escalation resets after 10 minutes of clean reconciliation.
- Stage 3 auto-recovery caps at 3 attempts (15 minutes), then requires manual intervention.
- Existing kill switch triggers (stale feed, heartbeat, auth failures) are unchanged — they still go straight to FLATTEN_ONLY with no auto-recovery.

### Files

- `pmm1/risk/kill_switch.py` — add `MismatchTracker` class with per-market stage tracking
- `pmm1/main.py` — replace binary mismatch counter with staged tracker calls

---

## 2. Adaptive Gamma via Extended SpreadOptimizer

### Problem

`gamma=0.015` is hardcoded for all markets. A sports market with high informed flow needs gamma=0.05+; a stable political market needs gamma=0.005. Fixed gamma means too tight on toxic markets (losing to adverse selection) and too wide on clean markets (losing fills to competitors).

### Design

SpreadOptimizer already maintains per-market `BucketStats` for spread buckets. Add a parallel set of gamma buckets:

```
Spread buckets (existing): [0.005, 0.010, 0.015, 0.020, 0.025, 0.030]
Gamma buckets (new):        [0.005, 0.010, 0.015, 0.025, 0.040, 0.060]
```

Each bucket is a `BucketStats` with its own Gaussian posterior. On each fill:

1. `record_fill()` already receives `spread_capture` and `adverse_selection_5s`
2. Existing: reward for spread bucket = `spread_capture - adverse_selection_5s`
3. New: reward for gamma bucket = `spread_capture - 2 * adverse_selection_5s` (2x multiplier penalizes AS more heavily because gamma's job is inventory risk protection)

On each quote cycle:

1. Existing: `get_optimal_base_spread(condition_id)` Thompson-samples the best spread
2. New: `get_optimal_gamma(condition_id)` Thompson-samples the best gamma
3. Quote engine uses sampled gamma instead of `self.config.inventory_skew_gamma`

### Blending

Same 70/30 blend pattern as spread: `effective_gamma = 0.7 * config_gamma + 0.3 * sampled_gamma`. Prevents wild values from insufficient data.

### Convergence

With 6 buckets and decay=0.95, meaningful differentiation after ~30 fills per market. Markets with <10 fills stay on config default.

### Files

- `pmm1/analytics/spread_optimizer.py` — add `_gamma_buckets` dict, `get_optimal_gamma()`, extend save/load
- `pmm1/strategy/quote_engine.py` — `compute_half_spread()` accepts `optimal_gamma` parameter
- `pmm1/main.py` — pass sampled gamma through quote path (same pattern as `optimal_base_spread`)

---

## 3. Toxicity-Based Quoting Pause

### Problem

When VPIN > 0.3, the code widens spreads. But when VPIN > 0.55, you shouldn't be quoting at all — informed traders are actively moving the market, and any fill is likely adverse.

### Design

A per-market cooldown in the quote loop:

```python
if features.vpin > toxicity_pause_threshold:  # default 0.55
    _toxicity_mute_until[md.condition_id] = time.time() + toxicity_pause_seconds  # default 30

if time.time() < _toxicity_mute_until.get(md.condition_id, 0):
    suppress("toxicity_pause")
    continue
```

### Rules

- Threshold configurable via `PricingConfig.toxicity_pause_vpin` (default 0.55)
- Duration configurable via `PricingConfig.toxicity_pause_seconds` (default 30)
- Per-market — toxic flow in Market A doesn't pause Market B
- Existing VPIN spread widening (0.3-0.55) unchanged — this is a separate, more aggressive gate
- If VPIN drops below threshold, quoting resumes at next cycle

### Why 0.55 not 0.3

VPIN 0.3-0.55 = elevated but uncertain toxicity → widen spreads. VPIN > 0.55 = high-confidence informed flow → step aside entirely.

### Files

- `pmm1/settings.py` — add `toxicity_pause_vpin` and `toxicity_pause_seconds` to `PricingConfig`
- `pmm1/main.py` — add per-market mute dict and check before `compute_quote`

---

## 4. Kelly-Rational Dynamic Exit Thresholds

### Problem

Take-profit at +5%/+20% and stop-loss at -10%/-30% are fixed numbers that ignore market context. A -10% loss at hour 3 is noise; at hour 0.5 it's signal. A +5% gain with 6 hours left should be held; with 1 hour left should be taken.

### Design

Replace fixed thresholds with Kelly growth rate comparison:

```python
def should_exit(self, p_fair, p_market, cost_to_exit, time_remaining_hours) -> bool:
    growth_if_hold = kelly_growth_rate(p_fair, p_market)
    exit_urgency = cost_to_exit / max(0.1, time_remaining_hours)
    return growth_if_hold < exit_urgency
```

Where:
- `kelly_growth_rate(p, q) = p * log(p/q) + (1-p) * log((1-p)/(1-q))` — binary KL divergence (expected log-wealth growth)
- `cost_to_exit` = half-spread from `compute_half_spread()`
- `time_remaining_hours` from features

### How this replaces fixed thresholds

- **Stop-loss:** When `p_fair` converges toward `p_market` (edge disappears), growth → 0, falls below exit cost → exit.
- **Take-profit:** When `p_market` moves toward `p_fair` (market agrees with you), growth → 0 → exit.
- **Time decay:** As `time_remaining` shrinks, `exit_urgency` grows, making the bar higher.
- **Vol awareness:** High-vol markets have larger `cost_to_exit` (wider spread), so you hold longer.

### Safety backstop

Existing hard stop (-30% or `max_loss_per_trade_usd`) is kept as-is. Kelly-rational exits handle common cases; hard stops handle catastrophic ones.

### Files

- `pmm1/strategy/exit_manager.py` — add `_check_kelly_rational_exit()`, wire into `evaluate_position()` as highest-priority (after flatten, before stop-loss)
- `pmm1/math/kelly.py` — add `kelly_growth_rate(p, q)` function

---

## Testing Strategy

| Change | Tests | What they prove |
|--------|-------|----------------|
| Staged recovery | Stage escalation 0→1→2→3, auto-recovery to 0, per-market isolation, 3-retry cap | Recovery works and safety backstop holds |
| Adaptive gamma | Thompson converges to higher gamma for toxic markets, lower for clean; save/load; blend | Learning works and is backward-compatible |
| Toxicity pause | VPIN > 0.55 suppresses, < 0.55 resumes, per-market isolation, configurable | Pause engages and disengages correctly |
| Dynamic exits | Growth rate comparison, time decay urgency, hard stop preserved, edge=0 triggers exit | Kelly-rational exits beat fixed thresholds |

## Implementation Order

1. **Toxicity pause** (smallest, most self-contained, immediate AS cost reduction)
2. **Staged recovery** (operational safety, no pricing logic involved)
3. **Adaptive gamma** (extends existing SpreadOptimizer, needs fill data flowing)
4. **Dynamic exits** (most complex, builds on Kelly math already in codebase)

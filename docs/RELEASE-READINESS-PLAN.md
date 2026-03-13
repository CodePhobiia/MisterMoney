# MisterMoney — Production Readiness Plan

## Definition of Production Ready

MisterMoney is only production ready when all of the following are true:

- **V1** can run live for 24h+ without drift, crash, or silent bad state
- **order lifecycle truth** is reliable
- **NAV / positions / fills / PnL** reconcile cleanly
- **PMM2 shadow** is measuring reality, not garbage proxies
- **PMM2 canary** can safely control a small slice of capital
- alerts are trustworthy
- rollback is instant

The plan is:

1. **stabilize truth**
2. **stabilize behavior**
3. **stabilize PMM2 shadow**
4. **run canary**
5. **promote carefully**

---

## Phase 0 — Freeze the target

### Goal
Stop changing random things and define the exact release target.

### Tasks
- Branch a release target from current working state
- Stop adding new features unrelated to:
  - execution correctness
  - reconciliation
  - PMM2 shadow truthfulness
  - ops / alerting
- Write a short release checklist doc:
  - what must pass before canary
  - what must pass before PMM2 live
- Lock config values that affect risk until verification is complete

### Exit criteria
- Single branch designated as release candidate
- No open ambiguity on what production-ready means

---

## Phase 1 — Make V1 accounting true

This is the highest priority. If V1 truth is wrong, everything above it is fake.

### 1.1 Order lifecycle audit

#### Problem
Logs show lots of `order_created`, but quote-cycle summaries often show `submitted: 0`, `canceled: 0`.

#### Tasks
- Trace every path that can create/cancel/amend orders:
  - quote loop
  - taker bootstrap
  - reconciliation path
  - retry / restart paths
- Create one canonical order event stream:
  - `intent_created`
  - `submitted`
  - `live`
  - `matched`
  - `partial`
  - `filled`
  - `canceled`
  - `expired`
  - `failed`
- Ensure summary metrics derive from canonical events, not ad hoc counters
- Add invariant checks:
  - if `order_created` happened in cycle, summary cannot show zero submitted unless explicitly excluded and labeled

#### Exit criteria
- For a sampled hour, order summary matches raw order lifecycle logs exactly
- No unexplained discrepancy between `order_created` and cycle summaries

---

### 1.2 Fill truth + position truth

#### Problem
Must be fully trustworthy.

#### Tasks
- Verify every matched/fill event maps to:
  - tracked order
  - position update
  - PnL attribution record
  - fill recorder row
- Re-check `position_zeroed_by_exchange` behavior:
  - ensure stale local positions are pruned only via reconciliation truth
- Add reconciliation invariants:
  - local positions == exchange positions after reconciliation
  - if not, emit high-priority alert with diff payload

#### Exit criteria
- No orphan fills
- No unknown-order fills
- Position snapshots reconcile cleanly across at least 24h of runtime

---

### 1.3 NAV / PnL / drawdown truth

#### Problem
Startup drawdown bug is fixed, but NAV must be formally reliable.

#### Tasks
- Make one canonical NAV function used everywhere:
  - main loop
  - drawdown governor
  - PMM2 runtime
  - dashboards / alerts
- Add explicit source tagging to NAV logs:
  - cash
  - marked positions
  - total
- Add sanity checks:
  - NAV cannot jump due only to reconciliation unless backed by position/cash delta
- Write a replay test for:
  - startup
  - reconcile
  - drawdown initialization
  - no false flatten-only

#### Exit criteria
- Same NAV number across all subsystems
- No synthetic/default NAV fallbacks in live mode
- Drawdown transitions only occur on real mark-to-market loss

---

## Phase 2 — Make V1 execution behavior sane

### 2.1 Quote churn / replacement audit

#### Problem
PMM1 appears to be creating many live orders repeatedly.

#### Tasks
- Measure:
  - cancels per hour
  - new orders per hour
  - average live duration
  - average reprices per market
- Identify why repeated orders occur:
  - TTL expiry
  - reprice threshold too sensitive
  - exchange state lag
  - order tracker mismatch
  - reconciliation forcing resubmits
- Add per-market lifecycle telemetry:
  - `order_replaced_reason`
  - `reprice_trigger`
  - `ttl_expired`
  - `state_mismatch_reconcile`
- Tune:
  - TTL
  - price move threshold
  - size change threshold
  - reconciliation aggressiveness

#### Exit criteria
- Order churn materially reduced
- Replacements are explainable by logged reasons
- No unexplained order spam loops

---

### 2.2 Strategy sanity pass

#### Tasks
- Verify live quoted markets make sense under V1 rules:
  - not stale
  - not near resolution unless intended
  - not violating directional concentration
- Audit current active market set for:
  - reward capture
  - spread quality
  - liquidity
  - event diversification
- Add structured “market rejected because…” logs for V1 universe selection

#### Exit criteria
- Every active V1 market has an explainable reason for being active
- No mystery markets in the live set

---

## Phase 3 — Make PMM2 shadow scientifically valid

### 3.1 Fix shadow comparison semantics

#### Tasks
- Ensure shadow compares against:
  - actual V1 live markets
  - actual V1 live capital
  - actual recent V1 cancel rate
- Remove remaining bogus proxies where possible
- Persist per-cycle comparison rows with:
  - V1 market set
  - PMM2 market set
  - overlap %
  - reward market counts
  - churn rates
  - EV delta
  - top reasons for divergence

#### Exit criteria
- Every shadow metric is derived from auditable inputs
- No fake self-referential metrics left

---

### 3.2 Fix PMM2 objective function

Current root causes already identified:
- fill model was over-optimistic
- event_id was missing
- reward EV likely still underweighted
- reward improvement remains zero

#### Tasks
- Recalibrate spread EV, especially for low-price markets
- Penalize ultra-low-price / extreme-odds markets harder where appropriate
- Rework reward EV so it reflects actual scoring competition, not just crude depth heuristics
- Introduce explicit selection bias for reward-core markets where spec intends it
- Make allocator constraints reflect intended strategy:
  - not just max-return greedy
  - but reward / rebate / time-in-book economics

#### Exit criteria
- PMM2 no longer prefers obviously inferior non-reward markets without good reason
- Shadow logs can explain why PMM2 beats or loses to V1 in concrete terms

---

### 3.3 Replace weak launch gates

Current gates are too rough.

#### New gate set
- **Gate 1:** positive EV delta over rolling window
- **Gate 2:** reward capture improvement over rolling window
- **Gate 3:** churn rate lower or equal to V1 at matched capital
- **Gate 4:** no safety regressions
- **Gate 5:** enough cycles + enough fills + enough market variety

#### Tasks
- Rewrite launch gate logic around:
  - rolling windows
  - minimum sample sizes
  - actual rates, not raw counts
- Persist gate diagnostics every cycle

#### Exit criteria
- `ready_for_live` means something real and defensible

---

## Phase 4 — Production operations hardening

### 4.1 Alerting

#### Tasks
- Send real alerts for:
  - service down
  - reconciliation mismatch
  - drawdown tier change
  - WS stale / reconnect storm
  - no active quotes for X minutes
  - excessive churn
  - fill recorder failure
  - DB write failure
- Separate severity:
  - info
  - warning
  - critical

#### Exit criteria
- Important failures page immediately
- Non-important noise does not

---

### 4.2 Runbook

#### Tasks
- Update `docs/RUNBOOK.md` with:
  - normal behavior examples
  - known-good log signatures
  - bad log signatures
  - restart steps
  - rollback steps
  - what to inspect first for each alert type
- Add one-command health checks:
  - service status
  - last fills
  - active orders
  - NAV
  - recent errors
  - PMM2 gate summary

#### Exit criteria
- Someone can operate the bot at 3 AM without guessing

---

### 4.3 Config discipline

#### Tasks
- Separate:
  - safe defaults
  - production overrides
  - experiment flags
- Ensure dangerous toggles are explicit:
  - PMM2 live enable
  - taker bootstrap
  - flatten flags
- Add config validation for invalid combinations

#### Exit criteria
- No accidental unsafe config state after restarts

---

## Phase 5 — Canary launch

### 5.1 PMM2 canary

#### Tasks
- Keep V1 as safety / execution substrate
- Let PMM2 control only a tiny live slice:
  - `live_capital_pct = 0.05` or `0.10`
- Restrict canary to:
  - reward-eligible markets only
  - low ambiguity
  - healthy liquidity
- Log PMM2-controlled orders separately

#### Exit criteria
- 24–48h canary with:
  - no safety incidents
  - acceptable churn
  - no reconciliation drift
  - positive or at least non-worse realized behavior vs V1 baseline

---

### 5.2 Promote gradually

#### Steps
- 5% → 10% → 25% → 50% → 100%
- Only if each stage passes its own observation window

#### Automatic rollback triggers
- drawdown threshold breach
- reconciliation mismatch
- cancel storm
- shadow/live divergence anomaly
- PMM2 orders not scoring when expected

---

## Concrete implementation order

### Sprint A — Truth
1. Fix order summary metrics vs real order lifecycle
2. Reconcile fills / positions / NAV into one truthful model
3. Add invariant tests

### Sprint B — Behavior
4. Diagnose live order churn
5. Tune reprice / TTL / reconcile behavior
6. Add per-market replacement reasons

### Sprint C — PMM2 objective
7. Rework reward EV / reward-core prioritization
8. Further calibrate fill model + extreme-price penalties
9. Finalize event / correlation-aware allocator behavior

### Sprint D — Shadow quality
10. Replace launch gates with rolling, real metrics
11. Persist shadow diagnostics + gate state
12. Build comparison dashboard / summary command

### Sprint E — Ops
13. Alerting
14. Runbook
15. Rollback / canary controls

### Sprint F — Canary
16. 5–10% PMM2 live capital
17. Observe
18. Promote only if data supports it

---

## Hard acceptance checklist

Do not call the system production-ready until all of these are green:

- Existing test suite passing
- Added tests for new fixes passing
- 24h no crash
- 24h no reconciliation mismatch
- 24h no false drawdown state
- no parquet / DB write errors
- order summary matches raw lifecycle logs
- PMM2 shadow metrics are auditable
- launch gates use real measurements
- canary succeeds before full promotion

---

## Current state assessment

MisterMoney is currently **post-firefighting, pre-production**.

It has moved from:
- “limping and partly lying”

to:
- “much more stable and much more truthful”

But there is still a real gap between:
- **stable software**
- and
- **production trading system**

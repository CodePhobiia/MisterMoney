# MisterMoney Master Fix Plan

**Created**: 2026-03-08
**Sources**: Architecture Review (Audit A) + 6-Auditor Quantitative Review (Audit B, 130 findings)
**Current State**: Live at ~$97 NAV ($23.86 liquid + $73.59 positions), bot reports $13.80 (broken NAV)
**Verdict**: Operational but structurally unsound. Fix before scaling.

---

## Principles

1. **No new features until Tier 0 is closed.** V4 is frozen. V3 stays shadow-only. PMM-2 stays shadow-only.
2. **Fix order: safety → truth → calibration → integration → features.**
3. **Each tier has an exit gate.** Don't start the next tier until the gate passes.
4. **Bot stays live during fixes** at current NAV (~$100). Kill switch improvements go in first.
5. **Every fix gets a test.** No untested money-handling code.

---

## Tier 0 — Stop the Bleeding (before any NAV increase)

*Estimated: 2–3 days focused work. Bot can stay live during fixes with rolling restarts.*

### T0-01: Cancel failure fall-through → double exposure
- **Findings**: E-01
- **Risk**: Double exposure on transient cancel failure. Up to $720 unintended.
- **File**: `pmm1/execution/order_manager.py:265-268`
- **Fix**: Add `return results` after generic exception handler in `diff_and_apply()`
- **Test**: Unit test — mock cancel raising Exception, assert no orders submitted after
- **Effort**: 15 min
- **Priority**: P0 — money safety

### T0-02: Drawdown governor uses day-start, not high-water mark
- **Findings**: R-03
- **Risk**: 9% intraday drawdown from peak reads as 0%. Governor is blind.
- **File**: `pmm1/risk/drawdown.py:143-149`
- **Fix**: Replace `day_start_nav` with `daily_high_watermark` in drawdown_pct calculation
- **Test**: Unit test — NAV 100→110→101, assert drawdown = 8.2% not 1%
- **Effort**: 30 min
- **Priority**: P0 — last line of defense is broken

### T0-03: NAV uses cost basis, not mark-to-market
- **Findings**: R-05, Audit A §3
- **Risk**: NAV shows $13.80 when actual is ~$97. Drawdown governor receives garbage.
- **Files**: `pmm1/state/inventory.py:223-241`, `pmm1/main.py:870-872`
- **Fix**: Pass a price oracle (book midpoints) into `get_total_nav_estimate()`. Use `book_manager.get_book(token_id).midpoint` as fallback mark price instead of `yes_avg_price`.
- **Test**: Unit test — position at cost $0.50, market at $0.30, assert NAV reflects $0.30
- **Effort**: 2 hrs
- **Priority**: P0 — everything downstream depends on NAV truth

### T0-04: Order state machine accepts terminal→active transitions
- **Findings**: E-03
- **Risk**: Ghost orders corrupt diff calculations. Markets go unquoted.
- **File**: `pmm1/state/orders.py:155-192`
- **Fix**: Hard-reject transitions FROM terminal states (FILLED/CANCELED/EXPIRED/FAILED). Allow active-to-active with warnings.
- **Test**: Unit test — CANCELED order receives LIVE transition, assert rejected
- **Effort**: 30 min
- **Priority**: P0 — state corruption

### T0-05: Top-of-book clamp defeats inventory skew
- **Findings**: R-07, Audit A §4
- **Risk**: A-S model is decorative. Bot joins best bid regardless of inventory, accumulating more when it should step back.
- **File**: `pmm1/main.py:1146-1167`
- **Fix**: Only clamp toward top-of-book (don't improve beyond best). Do NOT clamp bid upward when skew pushes it lower (that's the skew working). Same logic inverted for asks.
- **Test**: Unit test — long inventory + skew pushes bid below best_bid, assert bid stays below
- **Effort**: 1 hr
- **Priority**: P0 — core market-making logic is nullified

### T0-06: Populate event_id from Gamma API
- **Findings**: A-08, Audit A §2
- **Risk**: event_id="" means per-event cluster limits are unenforced. Correlated positions unlimited.
- **Files**: `pmm1/main.py:216+`, universe construction
- **Fix**: After fetching from /markets, do a second enrichment pass: batch-fetch event_ids from Gamma /events or /markets?id= endpoints. If event_id still missing, exclude from cluster-aware allocation.
- **Test**: Integration test — assert all funded markets have non-empty event_id
- **Effort**: 1.5 hrs
- **Priority**: P0 — correlation risk

### T0-07: Taker bootstrap bypasses all risk limits
- **Findings**: R-11
- **Risk**: Fill escalation submits a FAK buy bypassing circuit breaker, risk limits, drawdown governor. 5% NAV in an uncontrolled trade.
- **File**: `pmm1/main.py:1527-1598`
- **Fix**: Route taker order through risk limit checker before submission. Check `is_kill_switch_active()`, `check_per_market_limit()`, `drawdown_governor.tier`.
- **Test**: Unit test — drawdown Tier1 active, assert taker bootstrap blocked
- **Effort**: 1 hr
- **Priority**: P0 — risk bypass

### T0-08: Kill switch auto-clear timing (exchange restart)
- **Findings**: A-05
- **Risk**: Stale feed auto-clears after 30s. Exchange restart takes 90s. Post-clear, bot quotes on stale state. Up to $360 excess exposure.
- **File**: `pmm1/risk/kill_switch.py:97-107, 113-121`
- **Fix**: Increase `auto_clear_s` to 120s. Better: require both WS connected AND successful reconciliation before clearing.
- **Test**: Unit test — one WS up + one down, assert kill switch stays active
- **Effort**: 30 min
- **Priority**: P0 — weekly occurrence (Tuesday restarts)

### T0-09: Disable neg-risk arb (non-atomic, no on-chain step)
- **Findings**: E-02, A-02
- **Risk**: Non-atomic multi-leg execution with no rollback. No on-chain conversion step exists.
- **File**: `config/default.yaml`
- **Fix**: Set `neg_risk_arb.enabled: false`. Revisit after implementing two-phase commit with rollback.
- **Test**: Config validation — assert neg_risk_arb disabled in prod profile
- **Effort**: 5 min
- **Priority**: P0 — structural incompleteness

### T0-10: Parquet flush blocks async event loop
- **Findings**: S-04
- **Risk**: Synchronous disk I/O blocks event loop. No WS, no heartbeats during flush. Kill switch fires.
- **File**: `pmm1/storage/parquet.py:73-118`
- **Fix**: Wrap `flush()` in `asyncio.to_thread(self._sync_flush)`
- **Test**: Verify heartbeat interval during flush (mock slow disk)
- **Effort**: 15 min
- **Priority**: P0 — liveness

### T0-11: Fire-and-forget tasks swallow exceptions
- **Findings**: S-03
- **Risk**: `asyncio.create_task()` without done callbacks. Failed fill records silently lost.
- **Files**: `pmm1/main.py:586-600, 629, 1516`
- **Fix**: Add `_task_exception_handler` done callback to all fire-and-forget tasks.
- **Test**: Unit test — task that raises, assert error logged
- **Effort**: 30 min
- **Priority**: P0 — silent data corruption

### T0-12: Fill dedup set clears entirely at 500
- **Findings**: E-06, R-06, A-12
- **Risk**: After 500 unique fills, ALL dedup history lost. WS replays cause double position tracking.
- **File**: `pmm1/main.py:536-541`
- **Fix**: Replace `set` + `.clear()` with LRU dedup (OrderedDict, maxsize=2000). Evict oldest one-at-a-time.
- **Test**: Unit test — insert 2001 entries, assert oldest evicted, recent still present
- **Effort**: 30 min
- **Priority**: P0 — position corruption

### T0-13: PMM-2 bridge guard (prevent accidental live deployment)
- **Findings**: S-01, Audit A §1
- **Risk**: V1Bridge methods are stubs returning `True`. If shadow_mode=false, PMM-2 "executes" orders that don't exist.
- **File**: `pmm2/runtime/v1_bridge.py:174-270`
- **Fix**: Add `raise NotImplementedError("V1 bridge live execution not implemented")` guard when shadow_mode=false. 
- **Test**: Unit test — shadow_mode=false, assert NotImplementedError raised
- **Effort**: 15 min
- **Priority**: P0 — prevents catastrophic misconfiguration

### T0-14: V1 state snapshot not atomic
- **Findings**: S-02
- **Risk**: Counterfactual comparisons built on inconsistent snapshots. Could pass launch gates on noise.
- **File**: `pmm2/shadow/v1_snapshot.py:59-142`
- **Fix**: Use dict.copy() on tracker internals at a single point. Add asyncio.Lock for snapshot capture window.
- **Test**: Concurrent modification test — snapshot during simulated order churn, assert internal consistency
- **Effort**: 1 hr
- **Priority**: P0 — gate reliability

### T0-15: Config — tighten risk limits to justified values
- **Findings**: R-01, A-03, G-08, Audit A §4
- **Risk**: Prod config is 3-6x looser than spec hard caps. 60% directional at $100 NAV.
- **Files**: `config/default.yaml`, new `config/prod.yaml`
- **Fix**: 
  - Create `config/prod.yaml` with spec-aligned limits
  - `per_market_gross_nav: 0.08` (keep — justified at $100 NAV where 2% = $2 = unfundable)
  - `per_event_cluster_nav: 0.15` (keep until event_id fixed, then tighten to 0.10)
  - `total_directional_nav: 0.30` (halve from 0.60 — compromise between spec's 0.10 and operational need)
  - `total_arb_gross_nav: 0.25` (back to spec)
  - `allow_sports: true` (keep — justified: sports are high-volume, good for MM at small NAV)
  - `require_clear_rules: true` (back to spec)
  - `min_time_to_end_hours: 12` (compromise between 6 and 24)
  - Add startup log printing any override looser than spec defaults
  - Add validation: `num_markets * per_market_gross_nav <= 0.80`
- **Test**: Startup validation test — assert overcommit rejected
- **Effort**: 1 hr
- **Priority**: P0 — risk posture

### T0-16: Exit manager only processes YES side
- **Findings**: R-12
- **Risk**: NO-side positions have no stop-loss protection.
- **File**: `pmm1/strategy/exit_manager.py:92-103`
- **Fix**: Replace `elif no_size > 0` with separate `if no_size > 0` block. Process both sides.
- **Test**: Unit test — position with both YES and NO, assert both evaluated
- **Effort**: 15 min
- **Priority**: P0 — loss protection gap

---

### Tier 0 Exit Gate
- [ ] All 16 fixes committed and deployed
- [ ] All unit tests passing
- [ ] Bot restarted with new config
- [ ] NAV reports correctly (matches on-chain within $1)
- [ ] Drawdown governor triggers correctly on simulated intraday drop
- [ ] 24 hours clean operation post-deploy
- [ ] Review checkpoint with Theyab

---

## Tier 1 — Make Truth Honest (before scaling past $200 NAV)

*Estimated: 1 week. Can run in parallel with live trading on Tier 0 fixes.*

### T1-01: Wire QueueEstimator into PMM-2 scorer
- **Findings**: Q-02
- **Risk**: queue_ahead=0 makes all EV calculations fictional.
- **Files**: `pmm2/scorer/combined.py:79-82`, `pmm2/queue/estimator.py`
- **Fix**: Compute queue_ahead from book snapshot (sum visible liquidity at our price level). Replace hardcoded 0.0.
- **Effort**: 2 hrs

### T1-02: Floor toxicity at zero
- **Findings**: Q-03
- **Risk**: Negative toxicity inflates EV. Turns structural cost into phantom revenue.
- **File**: `pmm2/scorer/toxicity.py:70-86`
- **Fix**: `return max(0.0, raw_tox)`
- **Effort**: 15 min

### T1-03: Fix fill calibrator placeholder
- **Findings**: Q-07
- **Risk**: predicted_rate hardcoded to 0.5. Lambda correction actively degrades model accuracy.
- **File**: `pmm2/calibration/fill_calibrator.py:148`
- **Fix**: Compute predicted_rate from actual fill hazard model for the same window.
- **Effort**: 2 hrs

### T1-04: Cross-market correlation grouping
- **Findings**: R-02
- **Risk**: Bot can go 8% each on 5 correlated markets (40% on a single theme).
- **Files**: `pmm1/risk/limits.py`, new `pmm1/risk/correlation.py`
- **Fix**: Thematic grouping layer with keyword matching. Add `per_theme_nav` limit (15%).
- **Effort**: 1 day

### T1-05: Reconciliation → kill switch integration
- **Findings**: E-11
- **Risk**: Reconciliation mismatches never escalate. Bot quotes on corrupt state.
- **Files**: `pmm1/state/reconciler.py`, `pmm1/risk/kill_switch.py`
- **Fix**: Call `kill_switch.report_reconciliation_mismatch()` when mismatch_count > 3.
- **Effort**: 30 min

### T1-06: PMM-2 shadow NAV — remove 100.0 fallback
- **Findings**: S-02, Audit A §3
- **Risk**: Shadow gates pass on synthetic NAV.
- **Files**: `pmm2/shadow/v1_snapshot.py`, `pmm2/runtime/loops.py`
- **Fix**: Mark cycle invalid if NAV unknown. No fallback.
- **Effort**: 1 hr

### T1-07: WS reconnect — rebuild books from REST
- **Findings**: S-08, E-08
- **Risk**: After reconnect, local books are stale until WS sends new snapshots. Quotes on stale data.
- **File**: `pmm1/ws/market_ws.py:278-303`, `pmm1/main.py`
- **Fix**: After reconnect, immediately fetch books via REST for all subscribed assets. Add RECONNECTING gate to quote loop.
- **Effort**: 2 hrs

### T1-08: WS resubscribe — don't clear set before success
- **Findings**: E-15
- **Risk**: Failed resubscribe + cleared set = no book updates, quoting on empty books.
- **File**: `pmm1/ws/market_ws.py:288-291`
- **Fix**: Don't clear `_subscribed_assets` until after successful resubscribe.
- **Effort**: 15 min

### T1-09: Fill callback — pass actual fees
- **Findings**: E-13
- **Risk**: fee=0.0 on every fill. PnL systematically overestimated.
- **File**: `pmm1/main.py:567-571`
- **Fix**: Compute fee from `price * size * fee_rate` using market's actual fee rate.
- **Effort**: 30 min

### T1-10: Exit orders — use round_ask() for SELL
- **Findings**: E-16
- **Risk**: Rounding down on sells leaves money on the table every exit.
- **File**: `pmm1/execution/order_manager.py:394`
- **Fix**: `round_ask()` for normal exits, `round_bid()` for urgent exits.
- **Effort**: 15 min

### T1-11: Wire PnL tracker into live loop
- **Findings**: R-14, G-17
- **Risk**: PnL decomposition is dead code. Flying blind on profitability attribution.
- **Files**: `pmm1/main.py`, `pmm1/analytics/pnl.py`
- **Fix**: Call `record_fill()` in fill callback. Call `compute_snapshot()` every 5 min. Log results.
- **Effort**: 1 hr

### T1-12: Production alerting — Telegram alerts for critical events
- **Findings**: G-04
- **Risk**: Kill switch fires at 3 AM, nobody knows until morning.
- **File**: `pmm1/notifications.py`
- **Fix**: Add alerts for: kill switch activation, drawdown tier changes, reconciliation mismatches >3, position limit breaches, gas < 0.5 POL.
- **Effort**: 2 hrs

### T1-13: Move all hardcoded credentials to env vars
- **Findings**: S-12, S-25, G-21
- **Risk**: Google OAuth secret in source code. DB DSN with password committed. Chat IDs hardcoded.
- **Files**: `v3/providers/google_adapter.py`, `v3/shadow/main.py`, `pmm1/notifications.py`
- **Fix**: All to `.env` or `os.getenv()`. Pre-commit scan for credential patterns.
- **Effort**: 2 hrs

### T1-14: Basic pytest suite for execution core
- **Findings**: G-01, G-02, G-03
- **Risk**: 10,400 lines of money-handling code with zero tests.
- **Files**: `tests/unit/test_tick_rounding.py`, `test_kill_switch.py`, `test_order_state.py`, `test_heartbeat.py`, `test_risk_limits.py`
- **Fix**: Minimum 30 tests covering tick rounding, kill switch transitions, order state machine, heartbeat timing, risk limit enforcement.
- **Effort**: 2-3 days

### T1-15: Runbook
- **Findings**: G-05
- **Risk**: Incident response is tribal knowledge.
- **File**: `docs/RUNBOOK.md`
- **Fix**: Cover: emergency stop, restart checklist, exchange restart handling, drawdown investigation, gas refill, position reconciliation mismatch.
- **Effort**: 4 hrs

---

### Tier 1 Exit Gate
- [ ] All 15 fixes committed and deployed
- [ ] pytest suite with 30+ tests, all passing
- [ ] PMM-2 shadow shows non-fictional EV (queue_ahead > 0)
- [ ] V3 shadow running with valid auth tokens (both Anthropic + Codex)
- [ ] Runbook reviewed by Theyab
- [ ] PnL tracker live for 7+ days with real data
- [ ] NAV > $150 and drawdown governor proven (survived an intraday dip)
- [ ] 7 days clean operation post-Tier-1

---

## Tier 2 — Calibrate and Harden (before scaling past $500 NAV)

*Estimated: 2-3 weeks. Runs alongside live trading and data collection.*

### T2-01: Fair value model — replace identity function
- **Findings**: Q-01
- **Fix**: Implement microprice (volume-weighted mid) as immediate improvement. Build offline fitting pipeline for logistic model using book snapshots + fill outcomes when data accumulates.
- **Effort**: 2 hrs (microprice), 1 week (fitting pipeline)

### T2-02: PMM-2 bridge — real execution wiring
- **Findings**: S-01, Audit A §1
- **Fix**: Attach order_manager, heartbeat, reconciler to state before PMM-2 init. Replace V1Bridge stubs with actual V1 calls. Wire real book deltas.
- **Effort**: 3-5 days

### T2-03: Nightly calibration loop
- **Findings**: Q-05, Q-14, Audit A §5
- **Fix**: Add daily calibration jobs from fill_record, book_snapshot, scoring_history. Split by market regime. Fail closed if stale.
- **Effort**: 2-3 days

### T2-04: V3 provider stabilization
- **Findings**: G-07, Audit A §7
- **Fix**: Fix OpenAI adapter (official API), implement Google OAuth refresh, Redis-backed rate limiting, cost tracking, provider SLOs.
- **Effort**: 3-5 days

### T2-05: V3 Redis format alignment
- **Findings**: S-17
- **Fix**: Publisher uses SET, integrator uses HGETALL. Align to same format. Currently canary pipeline is DOA.
- **Effort**: 1 hr

### T2-06: V3 canary integration into V1 quote engine
- **Findings**: Audit A §6
- **Fix**: Wire V3Integrator into quote_engine.py. Cap at 1¢ influence. Log counterfactual vs midpoint.
- **Effort**: 2-3 days

### T2-07: Parallel order submission
- **Findings**: E-05
- **Fix**: `asyncio.gather()` with semaphore for concurrent order submission. Sign all first, then submit.
- **Effort**: 2 hrs

### T2-08: Neg-risk arb — two-phase commit with rollback
- **Findings**: E-02, A-02
- **Fix**: If re-enabling arb: implement phase 1 (buy all NO, rollback on partial), phase 2 (on-chain conversion), phase 3 (sell YES).
- **Effort**: 2-3 days

### T2-09: Config hot-reload
- **Findings**: S-10
- **Fix**: SIGHUP handler or file watcher. Atomic swap for critical fields.
- **Effort**: 1 day

### T2-10: Adopt positions at midpoint, not zero
- **Findings**: R-10
- **Fix**: Set adopted positions' avg_price to current midpoint. Flag as estimated.
- **Effort**: 30 min

### T2-11: USDC balance heuristic fix
- **Findings**: E-19
- **Fix**: Use consistent decimal conversion for USDC balance. Check contract decimals.
- **Effort**: 30 min

### T2-12: CI/CD pipeline
- **Findings**: G-20
- **Fix**: GitHub Actions: ruff lint, mypy, pytest. Gate merges on CI.
- **Effort**: 1 day

---

### Tier 2 Exit Gate
- [ ] Fair value model beats midpoint on backtested data
- [ ] PMM-2 bridge proven in canary (fake exchange → real V1 orders)
- [ ] V3 all providers healthy, canary producing counterfactual data
- [ ] Nightly calibration running for 14+ days
- [ ] CI pipeline green, all PRs gated
- [ ] 14 days clean operation at $500 NAV

---

## Tier 3 — Scale (before $5,000+ NAV)

*Estimated: 1-2 months. Requires accumulated data.*

### T3-01: Full quantitative stack rebuild
- Fit fair value model from data (betas via MLE)
- Validate reward proxy against actual payouts (50+ epochs)
- Calibrate fill hazard from queue position data
- Validate toxicity model against realized PnL

### T3-02: PMM-2 live deployment
- Canary with real capital (1% → 5% → 25% → 100%)
- Proven markout accuracy
- Counterfactual EV vs V1 demonstrated

### T3-03: V3 production promotion
- 14+ days shadow with validated Brier scores
- Canary ramp: 1¢ → 2¢ → 5¢
- Proven uplift over midpoint

### T3-04: Infrastructure hardening
- Postgres migration (V1 off SQLite)
- Proper monitoring (Prometheus + Grafana)
- Automated backup/DR
- Wallet security (hardware wallet or smart contract with spending limits)

### T3-05: V4 unfreeze
- Only after 30+ days V2/V3 shadow data
- Factor model and VOI scheduler
- Cross-market portfolio optimization

---

## Deduplication Notes

The two audits overlap significantly. Here's how findings map:

| Audit A Item | Audit B Equivalent | Status |
|---|---|---|
| A§1: PMM-2 not live-wired | S-01, S-02 | T0-13, T0-14, T2-02 |
| A§2: event_id blank | A-08 | T0-06 |
| A§3: Shadow NAV synthetic | R-05, S-02 | T0-03, T1-06 |
| A§4: Config drift | R-01, A-03, G-08 | T0-15 |
| A§5: Prototype calibration | Q-02, Q-05, Q-07 | T1-01, T1-03, T2-03 |
| A§6: V3 canary not wired | S-17 | T2-05, T2-06 |
| A§7: V3 providers broken | G-07 | T2-04 |
| A§8: Hardcoded env assumptions | S-12, S-13, S-25, G-21 | T1-13 |

## Findings NOT addressed (accepted risk at current NAV)

| Finding | Reason |
|---|---|
| A-09: Key compromise = 100% drain | At $100 NAV, HSM/multisig is over-engineering. IP whitelist is sufficient. Revisit at T3. |
| Q-16: Vol from trade prices (bid-ask bounce) | Benign direction (more conservative). Fix when fitting vol model. |
| Q-17: 7-day market lifetime assumption | Feature unused (beta_6=0). Fix when fitting model. |
| Q-18: ToxicityFitter regresses markouts on markouts | Fix in T2-03 calibration rebuild. |
| Q-19: Allocator reward market count = markets funded | Monitoring metric, no financial impact. |
| Q-20: B1 capital price-invariant | Minor. Fix when scaling past 20 markets. |
| G-24: ToS compliance review | Research task, not code. Theyab to review. |
| G-29: KYC/wallet identity | Research task. Theyab to review. |

---

## Summary

| Tier | Fixes | Est. Effort | NAV Gate | Key Outcome |
|------|-------|-------------|----------|-------------|
| **T0** | 16 | 2-3 days | Current ($100) | Bot is safe to run |
| **T1** | 15 | 1 week | $200 | Bot tells the truth |
| **T2** | 12 | 2-3 weeks | $500 | Bot has real edge |
| **T3** | 5 | 1-2 months | $5,000 | Bot is production-grade |

**Total: 48 actionable fixes derived from 130 findings across 2 independent audits.**

The 82 findings not explicitly listed are either: (a) deduplicated into a listed fix, (b) accepted risk at current scale, or (c) automatically resolved by a higher-priority fix (e.g., fixing NAV resolves all downstream NAV-dependent bugs).

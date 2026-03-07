# PMM-2 Engineering Work Breakdown

*Draft: 2026-03-07 — Butters*

## Principles

1. **V1 keeps running.** PMM-2 is built alongside, not instead of. V1 earns while we build.
2. **Data before logic.** Every scorer/estimator needs calibration data. Instrument first, optimize second.
3. **Shadow before live.** Stage 0 (replay) and Stage 1 (shadow) run the full decision pipeline with zero order mutations.
4. **Scale-aware defaults.** Current NAV is ~$104. Config defaults must work at this scale. The architecture should scale to $10K+ but never assume it.
5. **Each sprint ships a testable artifact.** No multi-sprint dark periods.

---

## Dependency Graph

```
Sprint 0 (V1 quick wins)
    ↓
Sprint 1 (Storage + Data Collection)
    ↓
    ├── Sprint 2 (Universe + Metadata)
    │       ↓
    │   Sprint 4 (Market EV Scorer)
    │       ↓
    │   Sprint 6 (Allocator)
    │       ↓
    │   Sprint 8 (Quote Planner)
    │
    └── Sprint 3 (Queue Estimator)
            ↓
        Sprint 5 (Persistence Optimizer)
            ↓
        Sprint 7 (Integration)
            ↓
        Sprint 9 (Shadow Mode)
            ↓
        Sprint 10 (Staged Live Rollout)
```

Sprints 2-3 can run in parallel. Sprints 4-5 can run in parallel.

---

## Sprint 0 — V1 Quick Wins (backport from V2 insights)

**Goal:** Immediate ROI improvements to V1 using V2's venue knowledge. No new architecture.

### Tickets

**S0-1: Reward-eligible market filter**
- Add `getSamplingSimplifiedMarkets()` call to universe selection
- Intersect with current eligible set — only quote markets that can earn rewards
- This alone could be the highest-ROI change: stop wasting capital on non-reward markets
- File: `pmm1/strategy/universe.py`
- Endpoint: `GET /sampling-simplified-markets` (CLOB API)

**S0-2: Order scoring probe**
- Poll `GET /order-scoring?order_id=X` every 30s for each live order
- Log results to structured log (`event=order_scoring_check`)
- No action yet — just visibility into whether our orders are actually scoring
- File: new `pmm1/api/scoring.py`

**S0-3: Rebate visibility**
- Poll `GET /rebates/current` once per hour
- Log realized rebates per market
- File: new `pmm1/api/rebates.py`

**S0-4: Fee-enabled market awareness**
- Parse `feesEnabled` field from market metadata
- Log which markets have maker rebates available
- No behavioral change yet — just data

**S0-5: GTD default for new orders**
- Switch from GTC to GTD with `now + 60 + 3600` expiration
- Reduces stale order risk without changing pricing
- Config: `order_type: GTD`, `gtd_duration_sec: 3600`

**Deliverable:** V1 only quotes reward-eligible markets, logs scoring status and rebates. Pure visibility gain.

**Estimate:** 1-2 days

---

## Sprint 1 — Storage + Data Collection

**Goal:** Build the durable data layer that all V2 components consume.

### Tickets

**S1-1: Database setup**
- SQLite for v1 (upgrade to Postgres later if needed)
- Create 5 tables from spec Section 11: `market_score`, `queue_state`, `allocation_decision`, `reward_actual`, `rebate_actual`
- Add 2 tables: `fill_record` (all fills with timestamps, markouts), `book_snapshot` (periodic book state for replay)
- File: new `pmm1/storage/database.py`, `pmm1/storage/schema.sql`

**S1-2: Fill recorder with markout tracking**
- On every confirmed fill, record: timestamp, condition_id, token_id, side, price, size, book_state_at_fill
- After fill, snapshot book at +1s, +5s, +30s to compute markouts
- Store in `fill_record` table
- This is the ground truth for toxicity cost (Section 5.5)
- File: new `pmm1/recorder/fill_recorder.py`

**S1-3: Book snapshot recorder**
- Every 10s, snapshot top-5 levels of book for each active market
- Store in `book_snapshot` table
- Used for queue estimation replay and fill probability calibration
- File: new `pmm1/recorder/book_recorder.py`

**S1-4: Scoring + rebate poller**
- Promote S0-2 and S0-3 from log-only to database-persisted
- Store scoring results with timestamp per order
- Store rebate snapshots daily
- File: extend `pmm1/api/scoring.py`, `pmm1/api/rebates.py`

**S1-5: Data export CLI**
- `python -m pmm1.tools.export --table fill_record --since 24h --format csv`
- For manual analysis and Stage 0 replay input

**Deliverable:** All V2 calibration data flowing to SQLite. Fill markouts computing. Scoring and rebate truth labels persisting.

**Estimate:** 2-3 days

---

## Sprint 2 — Universe + Metadata Layer

**Goal:** Rich market metadata beyond what V1's simple filter provides.

### Tickets

**S2-1: Reward-eligible surface**
- `getSamplingMarkets()` / `getSamplingSimplifiedMarkets()` intersection
- Cache with 5-minute TTL
- Track which markets enter/exit reward eligibility
- File: new `pmm2/universe/reward_surface.py`

**S2-2: Fee-enabled surface**
- Parse `feesEnabled`, `feeRate`, `expM` from market metadata
- Identify rebate-eligible markets
- File: new `pmm2/universe/fee_surface.py`

**S2-3: Market metadata enrichment**
- For each candidate market, compute and cache:
  - `hoursToResolution` (from end_date)
  - `is_neg_risk` + named vs placeholder outcomes
  - `ambiguity_score` (heuristic: title length, keyword flags like "approximately", "around")
  - `event_cluster_id` (group correlated markets)
  - `tick_size` (current)
  - `reward_pool_share` (from sampling markets data)
- File: new `pmm2/universe/metadata.py`

**S2-4: Universe scorer**
- Combine V1 eligibility + reward surface + fee surface + metadata
- Output: ranked list of candidate markets with metadata bundles
- File: new `pmm2/universe/scorer.py`

**Deliverable:** PMM-2 universe selection that knows which markets earn rewards, which have rebates, and which are risky.

**Estimate:** 2 days

---

## Sprint 3 — Queue Estimator

**Goal:** Track queue position for every live order. Core of the persistence half.

### Tickets

**S3-1: QueueState data model**
- Per-order state: `entry_time`, `price`, `size_open`, `est_ahead_low`, `est_ahead_mid`, `est_ahead_high`, `scoring`, `eta_sec`
- Pydantic model with serialization
- File: new `pmm2/queue/state.py`

**S3-2: Queue initialization**
- On order LIVE ack: `A_o^init = VisibleSizeAtPrice - β Q_o` (β=0.5 default)
- Read current book depth at order price from WS or REST
- File: new `pmm2/queue/estimator.py`

**S3-3: Queue update from book deltas**
- Subscribe to market WS book deltas
- Price-level decreases → consume `A_o` first
- New size after our arrival → assumed behind us
- Maintain `[A_low, A_high]` bounds
- File: extend `pmm2/queue/estimator.py`

**S3-4: Queue update from fills**
- Our fills reduce `size_open`
- Other fills at our price level reduce `A_o`
- File: extend `pmm2/queue/estimator.py`

**S3-5: Fill hazard function**
- `P^fill(H_Q) = 1 - exp(-λ · H_Q / (1 + κ A_o / Q_o))`
- `λ` estimated from observed queue depletion intensity (from book_snapshot data)
- ETA computation: `(A_o + ρ Q_o) / d̂`
- File: new `pmm2/queue/hazard.py`

**S3-6: Queue uncertainty penalty**
- `QueueUncertainty_o = χ (A_high - A_low)`
- Feeds into allocator adjusted score
- File: extend `pmm2/queue/state.py`

**Deliverable:** Real-time queue position estimates for all live orders. Fill probability and ETA per order.

**Estimate:** 3-4 days (most technically challenging sprint)

---

## Sprint 4 — Market EV Scorer

**Goal:** Compute V_{m,j} for each market-bundle pair per spec Section 5.

### Tickets

**S4-1: Bundle generator**
- For each market, generate B1/B2/B3 bundles with `(Cap, Slots, sizes, prices)`
- B1: min viable reward size, two-sided at inside
- B2: additional depth within reward spread band
- B3: edge extension (only if spread EV > 0 before rewards)
- Nested rule enforced: B3 ≤ B2 ≤ B1
- Scale-aware: at $104 NAV, most markets only get B1
- File: new `pmm2/scorer/bundles.py`

**S4-2: Spread EV**
- `E^spread = Σ P^fill · Edge · Q` per bundle
- Uses reservation price from V1, fill probability from Sprint 3
- File: new `pmm2/scorer/spread_ev.py`

**S4-3: Arbitrage EV**
- Inherit V1's parity + neg-risk conversion signals
- Filter: exclude placeholder/Other outcomes from neg-risk
- File: new `pmm2/scorer/arb_ev.py`

**S4-4: Liquidity reward EV**
- Implement `g(s,v)` scoring proxy
- Side-combination logic with c=3.0
- Competitor mass estimation from book depth
- Pool share from reward surface data
- Calibration against `/order-scoring` truth labels
- File: new `pmm2/scorer/reward_ev.py`

**S4-5: Maker rebate EV**
- `feeEq` calculation for fee-enabled markets
- Market share estimation from book depth
- Concentration preference: dominate one market > mediocre in six
- File: new `pmm2/scorer/rebate_ev.py`

**S4-6: Toxicity cost**
- Weighted markout: `0.5 M_1s + 0.3 M_5s + 0.2 M_30s`
- Reads from `fill_record` markout data (Sprint 1)
- Initial weights are priors; fitted after 7+ days of fill data
- File: new `pmm2/scorer/toxicity.py`

**S4-7: Resolution cost**
- `α₁ A_m + α₂/max(hours,6) + α₃ D_m + α₄ N_m`
- Ambiguity from metadata heuristics (Sprint 2)
- Dispute risk from historical patterns (initially manual flags)
- File: new `pmm2/scorer/resolution.py`

**S4-8: Combined scorer**
- `V_{m,j} = spread + arb + liq + reb - tox - res - carry`
- `R_{m,j} = V / Cap`
- Persist to `market_score` table
- File: new `pmm2/scorer/combined.py`

**Deliverable:** Full market value model. Each market-bundle pair has a dollar EV estimate.

**Estimate:** 3-4 days

---

## Sprint 5 — Persistence Optimizer

**Goal:** Decide HOLD/IMPROVE/WIDEN/CANCEL/CROSS for each live order.

### Tickets

**S5-1: Order persistence state machine**
- States: NEW → WARMING → SCORING → ENTRENCHED → STALE → EXIT
- Transition rules per spec Section 8.1
- File: new `pmm2/persistence/state_machine.py`

**S5-2: Action EV calculator**
- For each action in {HOLD, IMPROVE1, IMPROVE2, WIDEN1, WIDEN2, CANCEL, CROSS}:
  - Compute `P^fill · Edge · Q + E^liq + E^reb - C^tox - ResetCost`
- ResetCost = QueueValue + WarmupLoss + CancelCost
- File: new `pmm2/persistence/action_ev.py`

**S5-3: Hysteresis layer**
- `ξ = ξ₀ + ξ₁·𝟙(scoring) + ξ₂·𝟙(ETA<15s) + ξ₃·|inventorySkew|`
- Only move if `EV_best > EV_HOLD + ξ`
- Entrenched/scoring orders require much bigger reason to move
- File: new `pmm2/persistence/hysteresis.py`

**S5-4: Queue value computation**
- `QV = (P^fill(A) - P^fill(A')) · (Edge + r^liq + r^reb) · Q`
- A' = expected queue after cancel/repost (back of queue)
- This is the "cost of moving" — makes persistence the default
- File: extend `pmm2/persistence/action_ev.py`

**S5-5: Warmup loss estimator**
- Time since order placed vs. scoring warmup threshold
- If order is 80% through warmup, moving it wastes that progress
- File: new `pmm2/persistence/warmup.py`

**Deliverable:** Every live order has a recommended action with EV justification. HOLD is the default unless the math says otherwise.

**Estimate:** 2-3 days

---

## Sprint 6 — Discrete Allocator

**Goal:** Greedy bundle allocation with constraints per spec Section 6.

### Tickets

**S6-1: Adjusted score computation**
- `R̃ = R - λ·CorrPenalty - φ·ChurnPenalty - ψ·QueueUncertainty`
- CorrPenalty: penalize correlated markets (same event cluster)
- ChurnPenalty: penalize markets we'd be entering/exiting (hysteresis)
- QueueUncertainty: from Sprint 3
- **Add: InventoryPenalty = μ · |NetExposure| / Cap** (not in spec, recommended)
- File: new `pmm2/allocator/scoring.py`

**S6-2: Constraint checker**
- Capital: `Σ x·Cap ≤ Cap_total`
- Slots: `Σ x·Slots ≤ Slots_total`
- Per-market: `Σ_j x·Cap ≤ Cap^market`
- Per-event: `Σ_{event} x·Cap ≤ Cap^event`
- Nested: `x_3 ≤ x_2 ≤ x_1`
- Scale-aware: at $104 NAV, `per_market_cap = max($3.12, $8)` — override floor
- File: new `pmm2/allocator/constraints.py`

**S6-3: Greedy allocator**
- Sort feasible positive bundles by R̃ descending
- Greedily assign until capital/slot budgets exhausted
- Respect nested rule: can't fund B2 without B1
- File: new `pmm2/allocator/greedy.py`

**S6-4: Reallocation hysteresis**
- `|ΔCap| > max(0.1 · Cap, $5)` (scaled down from spec's $500)
- Rank changes must persist 3 cycles (3 minutes) before moving capital
- Override exceptions: inventory breach, reward eligibility change, resolution, arb appearance
- File: new `pmm2/allocator/hysteresis.py`

**S6-5: Fast-path circuit breaker** (addition to spec)
- If markout_1s > 3x average for a market, immediately flag STALE
- Persistence optimizer can force EXIT without waiting for allocator cycle
- File: new `pmm2/allocator/circuit_breaker.py`

**Deliverable:** Capital allocation decisions. Which markets get how much capital, at what bundle depth.

**Estimate:** 2-3 days

---

## Sprint 7 — Quote Planner + Integration

**Goal:** Convert allocator output into target quote plans. Wire everything together.

### Tickets

**S7-1: Quote planner**
- Input: allocator target capital + bundle selection per market
- Output: target ladder (prices, sizes, intents) per market
- `max_reprices_per_minute` enforcement
- File: new `pmm2/planner/quote_planner.py`

**S7-2: Diff engine**
- Compare target plan vs. current live orders
- Generate minimal mutation set: {add, cancel, amend}
- Respect persistence optimizer decisions (don't move ENTRENCHED orders)
- File: new `pmm2/planner/diff_engine.py`

**S7-3: Runtime loop integration**
- Wire the 5 cadences from spec Section 12:
  - Event-driven: WS deltas → queue update
  - Fast (250ms): queue states, ETA, action candidates
  - Medium (10s): market EV refresh, bundle values
  - Allocator (60s): greedy allocation, target plan deltas
  - Slow (5min): universe refresh, metadata, scoring checks
  - Daily: rebate reconciliation, model recalibration
- File: new `pmm2/runtime/loops.py`

**S7-4: V1 execution bridge**
- PMM-2 outputs order mutations → V1's order manager executes them
- V1's heartbeat, risk engine, reconciliation remain authoritative
- PMM-2 never bypasses V1 safety layers
- File: new `pmm2/runtime/v1_bridge.py`

**S7-5: Telemetry + dashboard**
- Extend existing dashboard with PMM-2 panels:
  - Bundle allocation heatmap
  - Queue position estimates per order
  - Scoring uptime percentage
  - EV breakdown per market
  - Persistence action log
- File: extend `tools/dashboard.py`

**Deliverable:** Complete PMM-2 pipeline from universe → scorer → allocator → planner → V1 execution. All loops running.

**Estimate:** 3-4 days

---

## Sprint 8 — Calibration + Attribution

**Goal:** Close the feedback loop. Compare estimates vs. actuals.

### Tickets

**S8-1: Reward capture efficiency**
- `realized_liq_reward / estimated_liq_reward` per market per epoch
- Pull from `/rebates/current` + internal estimates
- Store in `reward_actual` table
- Alert if efficiency < 50% for 3 consecutive epochs

**S8-2: Rebate capture efficiency**
- Same structure for maker rebates
- Store in `rebate_actual` table

**S8-3: Fill probability calibration**
- Compare predicted `P^fill(H_Q)` vs actual fill rate per price level
- Adjust λ, κ parameters via exponential moving average
- Half-life: 7 days for reward model, 14 days for rebate model

**S8-4: Toxicity weight fitting**
- After 7+ days of fill markout data, fit `(w_1s, w_5s, w_30s)` weights
- Use simple OLS: adverse PnL ~ w₁·M_1s + w₂·M_5s + w₃·M_30s

**S8-5: Attribution report**
- Daily PnL decomposition: spread + arb + rewards + rebates - toxicity - resolution - gas
- Telegram summary at midnight UTC
- File: new `pmm2/calibration/attribution.py`

**Deliverable:** Self-correcting models. PMM-2 gets better over time from its own data.

**Estimate:** 2-3 days

---

## Sprint 9 — Shadow Mode (Stage 1)

**Goal:** Full pipeline running live, zero order mutations. Compare V1 actuals vs PMM-2 counterfactuals.

### Tickets

**S9-1: Shadow execution mode**
- PMM-2 pipeline runs all loops but `v1_bridge` is mocked
- Logs all intended mutations without executing
- File: config flag `pmm2.shadow_mode: true`

**S9-2: Counterfactual comparison engine**
- For each allocator cycle, compare:
  - V1's actual market selection vs PMM-2's recommended selection
  - V1's actual orders vs PMM-2's target plan
  - V1's actual fills vs PMM-2's predicted fills
- Log divergences with EV impact estimates

**S9-3: 10-day evaluation criteria**
- PMM-2 must show:
  - Better market selection (more reward-eligible markets)
  - Lower churn (fewer cancels per live minute)
  - Better scoring uptime estimates
  - Positive EV delta vs V1 in at least 70% of cycles
- Document results before proceeding to Stage 2

**Deliverable:** 10 days of shadow data proving PMM-2 decisions are better than V1.

**Estimate:** 10 days calendar (1-2 days dev + 10 days observation)

---

## Sprint 10 — Staged Live Rollout

**Goal:** Gradual handover from V1 to PMM-2 control.

### Tickets

**S10-1: Stage 2 — 10% capital**
- PMM-2 controls 10% of NAV (~$10 initially)
- V1 controls remaining 90%
- Only reward-eligible binary markets with clean rules
- Kill switch: `pmm2.live_capital_pct: 0.10`

**S10-2: Stage 3 — 25% capital**
- Add fee-enabled rebate markets if rebate estimator is calibrated
- `pmm2.live_capital_pct: 0.25`

**S10-3: Stage 4 — Full production**
- PMM-2 allocator controls all market budgets
- V1 execution + safety layers remain authoritative
- `pmm2.live_capital_pct: 1.00`

**S10-4: V1 sunset path**
- V1's universe selection, pricing, and quote loop become PMM-2's subordinates
- V1's order manager, heartbeat, risk engine, reconciliation remain
- Document what's V1 vs V2 in architecture diagram

**Deliverable:** PMM-2 in full production. V1 is the execution substrate, V2 is the brain.

**Estimate:** 2-4 weeks (gated by performance data, not dev time)

---

## Timeline Summary

| Sprint | Name | Estimate | Dependencies | Parallelizable |
|--------|------|----------|-------------|----------------|
| S0 | V1 Quick Wins | 1-2 days | None | — |
| S1 | Storage + Data Collection | 2-3 days | S0 | — |
| S2 | Universe + Metadata | 2 days | S1 | ✅ with S3 |
| S3 | Queue Estimator | 3-4 days | S1 | ✅ with S2 |
| S4 | Market EV Scorer | 3-4 days | S2 | ✅ with S5 |
| S5 | Persistence Optimizer | 2-3 days | S3 | ✅ with S4 |
| S6 | Discrete Allocator | 2-3 days | S4 | — |
| S7 | Integration | 3-4 days | S5, S6 | — |
| S8 | Calibration | 2-3 days | S7 | — |
| S9 | Shadow Mode | 10 days | S8 | — |
| S10 | Live Rollout | 2-4 weeks | S9 | — |

**Critical path:** S0 → S1 → S2 → S4 → S6 → S7 → S8 → S9 → S10

**Total dev time:** ~22-33 days (excluding shadow observation and staged rollout)

**Calendar time with parallelization:** ~18-25 dev days + 10 days shadow + 2-4 weeks staged = **~2 months to full production**

---

## File Structure

```
pmm2/
├── __init__.py
├── universe/
│   ├── reward_surface.py
│   ├── fee_surface.py
│   ├── metadata.py
│   └── scorer.py
├── scorer/
│   ├── bundles.py
│   ├── spread_ev.py
│   ├── arb_ev.py
│   ├── reward_ev.py
│   ├── rebate_ev.py
│   ├── toxicity.py
│   ├── resolution.py
│   └── combined.py
├── queue/
│   ├── state.py
│   ├── estimator.py
│   └── hazard.py
├── persistence/
│   ├── state_machine.py
│   ├── action_ev.py
│   ├── hysteresis.py
│   └── warmup.py
├── allocator/
│   ├── scoring.py
│   ├── constraints.py
│   ├── greedy.py
│   ├── hysteresis.py
│   └── circuit_breaker.py
├── planner/
│   ├── quote_planner.py
│   └── diff_engine.py
├── calibration/
│   └── attribution.py
├── runtime/
│   ├── loops.py
│   └── v1_bridge.py
└── config.py

pmm1/
├── storage/
│   ├── database.py
│   └── schema.sql
├── recorder/
│   ├── fill_recorder.py
│   └── book_recorder.py
└── api/
    ├── scoring.py (new)
    └── rebates.py (new)
```

---

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| Queue estimation is wildly inaccurate on sparse Polymarket books | Persistence optimizer makes bad HOLD/MOVE decisions | Start with wide uncertainty bounds; only tighten after calibration data. Default to HOLD when uncertain. |
| Reward scoring formula changes without notice | Liquidity reward EV estimates become stale | `/order-scoring` truth label catches drift within 30s. Model auto-recalibrates. |
| $104 NAV too small for meaningful bundle allocation | Only B1 funded everywhere, B2/B3 never activate | Accept this. B1-only is still better than V1's blind allocation. Architecture scales when capital does. |
| Fill probability λ needs weeks of data to calibrate | Stage 0 replay has low-confidence inputs | Use conservative priors (underestimate fill prob). Better to miss fills than overtrade on bad estimates. |
| Polymarket weekly 425 restart causes state corruption | Queue estimates reset, scoring flags stale | Freeze all loops during 425 window. Full state reconstruction on restart. |
| Shadow mode diverges from V1 enough to be meaningless | Counterfactual comparison unreliable | Shadow runs on identical inputs. Divergence IS the signal — it shows what PMM-2 would do differently. |

---

## Open Questions for Theyab

1. **Storage backend**: SQLite enough for now, or go straight to Postgres? SQLite is simpler but concurrent writes from multiple loops could bottleneck.
2. **`getSamplingMarkets()` authentication**: Does this endpoint require API key auth or is it public? Need to verify before Sprint 0.
3. **Queue estimator β calibration**: The spec says β=0.5 default. Do we have any data on whether Polymarket book snapshots include our own orders? If β=1.0 (always included), the initialization formula simplifies.
4. **Ambiguity scoring**: Manual flags to start, or attempt NLP classification of market rules? Manual is faster but doesn't scale.
5. **Sub-agent build**: Want me to spawn this as a sub-agent build like V1, or build incrementally sprint-by-sprint with review between each?

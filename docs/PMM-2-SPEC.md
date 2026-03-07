# PMM-2: Capital Allocation & Queue Persistence Layer — Complete Spec

*Received from Theyab, 2026-03-07*

PMM-2 is the capital-allocation and queue-persistence layer that sits on top of the V1 execution core. It exists because Polymarket's current economics reward where we quote and how long we stay there, not just whether we can submit orders quickly: liquidity rewards favor passive, balanced quoting near midpoint, maker rebates are paid daily in USDC and calculated per market, and there is an order-scoring endpoint that tells us whether a live order is actually scoring. That makes allocation and queue retention higher-ROI than adding a bigger prediction model right now.

---

## 1. Scope and design stance

PMM-2 does not replace V1. It uses V1's order manager, inventory manager, risk engine, heartbeats, reconciliation, and WS handlers. PMM-2 adds five new layers:
1. Universe + metadata layer
2. Market EV scorer
3. Discrete capital allocator
4. Queue estimator + persistence optimizer
5. Calibration + attribution layer

Pushback: we should not start with a fancy MIQP or reinforcement-learning allocator. Our inputs are noisy, partially latent, and regime-dependent. The robust v2 is a discrete, greedy, constraint-aware bundle allocator with a hard hysteresis layer. We can add an MIQP later behind a feature flag if the bundle valuations become stable.

## 2. Official venue hooks PMM-2 must use

For universe discovery, we should keep using Gamma's events endpoint with `active=true&closed=false`, because Polymarket explicitly documents that as the most efficient way to fetch all active markets. Reward-eligible markets should be intersected from `getSamplingMarkets()` / `getSamplingSimplifiedMarkets()`, which are documented as the reward/sampling market surfaces.

For live state, PMM-2 should consume the public market WebSocket as the source of truth for the book. The market WS provides orderbook snapshots, price-level deltas, last-trade updates, and `tick_size_change`; with `custom_feature_enabled: true`, it also provides `best_bid_ask`, `new_market`, and `market_resolved`. The authenticated user WS is server-side only and gives our real-time trade/order lifecycle, including statuses like `MATCHED`, `MINED`, `CONFIRMED`, `RETRYING`, and `FAILED`.

For reward and rebate truth, PMM-2 should use two official feedback endpoints. `GET /order-scoring` tells us whether a specific live order is currently scoring for maker rewards. `GET /rebates/current` returns current daily rebated fees for a maker by market and does not require authentication. Those two endpoints are critical because they let us calibrate our internal reward and rebate estimates against venue truth instead of guessing.

Execution constraints still come from V1. All orders are limit orders under the hood; post-only is only valid with GTC/GTD; GTD has a one-minute security buffer; prices must conform to current tick size; the heartbeat endpoint cancels all open orders if a valid heartbeat is not received within 10 seconds plus a 5-second buffer; and weekly matching-engine restarts return HTTP 425 on Tuesdays at 7:00 AM ET. PMM-2 must respect all of that.

## 3. Strategy objective

PMM-2 optimizes marginal next-hour net EV per dollar of capital and per order slot.

We define two horizons:
- **Allocator horizon**: H_A = 60 minutes
- **Order horizon**: H_Q = 30 seconds

The allocator decides which markets deserve more capital and quote depth over H_A. The persistence optimizer decides whether an existing order should be held, improved, widened, canceled, or crossed over H_Q.

The core objective is:

```
max Σ_{m,j} x_{m,j} V_{m,j}
    - (λ/2) w^T Σ w
    - φ · Churn(w, Δw)
```

subject to capital, event, inventory, and slot constraints, where `x_{m,j}` is whether we fund bundle j in market m, `V_{m,j}` is bundle value, and `w` is the resulting market-weight vector.

## 4. What a "bundle" is

We should not allocate directly at the market level. We should allocate in nested quote bundles.

For each market m, generate three bundles:
- **B1: reward core** — Two-sided inside quote at the minimum viable size to score well or at our base size, whichever is larger.
- **B2: reward depth** — Additional two-sided size within the reward-valid spread band.
- **B3: edge extension** — Extra depth or arb reserve that only exists if spread EV stays positive before rewards.

Each bundle has:

```
Bundle_{m,j} = (Cap_{m,j}, Slots_{m,j}, V_{m,j}, RiskVec_{m,j})
```

Nested rule:

```
x_{m,3} ≤ x_{m,2} ≤ x_{m,1}
```

So we never fund deeper size before funding the inner book.

## 5. Market value model

For each market m and bundle j:

```
V_{m,j} = E^spread_{m,j} + E^arb_{m,j} + E^liq_{m,j} + E^reb_{m,j} - C^tox_{m,j} - C^res_{m,j} - C^carry_{m,j}
```

and the marginal return ratio is:

```
R_{m,j} = V_{m,j} / Cap_{m,j}
```

### 5.1 Spread EV

Let `r_m` be the reservation price from V1 after inventory skew.

For each order o in bundle j:

```
Edge_o = r_m - p_o   (if bid)
Edge_o = p_o - r_m   (if ask)
```

```
E^spread_{m,j} = Σ_{o ∈ j} P^fill_o(H_Q) · Edge_o · Q_o
```

where `Q_o` is order size and `P^fill_o(H_Q)` is queue-adjusted fill probability.

### 5.2 Arbitrage EV

This is inherited from V1:

```
E^arb_{m,j} = E^{binary parity}_{m,j} + E^{neg risk conversion}_{m,j}
```

If a market has no structural arb, this term is zero. If it does, arb reserve can justify B3 even when passive quote EV is mediocre.

For augmented negative-risk events, PMM-2 should only treat named outcomes as tradable inventory candidates and should exclude placeholder/"Other" outcomes from allocator funding. Polymarket explicitly warns that unnamed/placeholder outcomes should be ignored until named, and "Other" should be avoided directly.

### 5.3 Liquidity reward EV

This is where PMM-2 differs most from V1.

Polymarket's published liquidity-reward methodology is based on a quadratic scoring logic around the midpoint, max-spread and min-size thresholds, a two-sided boost, and a single-sided discount with scaling factor c=3.0. In midrange markets, one-sided quotes can still score at a discount; in extreme-price markets, liquidity must be double-sided to score. The published methodology also sums normalized scores across 10,080 samples in an epoch, which means time-in-book matters and churn is expensive.

We should not try to reproduce the venue formula exactly on day one. Our internal proxy should be:

```
g(s, v) = (1 - s/v)²₊
```

where s is spread from the adjusted midpoint and v is the market's max incentive spread.

For each side-pair:

```
Q̂^one_m = Σ_{o ∈ side 1} g(s_o, v_m) Q_o
Q̂^two_m = Σ_{o ∈ side 2} g(s_o, v_m) Q_o
```

Then apply the official side-combination logic:

```
Q̂^pair_m = max(min(Q̂^one, Q̂^two), max(Q̂^one/c, Q̂^two/c))   if mid ∈ [0.10, 0.90]
Q̂^pair_m = min(Q̂^one, Q̂^two)                                   otherwise
```

with c = 3.0.

Expected liquidity reward over allocator horizon:

```
E^liq_{m,j}(H_A) = Pool_m · (Q̂^pair_{m,j} / (Q̂^pair_{m,j} + Q̂^others_m)) · (H_A / T_epoch) · P(scoring_{m,j})
```

Where:
- `Pool_m` comes from reward allocation metadata
- `Q̂^others_m` is competitor score mass
- `P(scoring)` is calibrated using GET /order-scoring

Important implementation detail: if order-scoring returns false for an order that our proxy thought was high-value, the proxy must be downweighted immediately. That endpoint is our truth label. Polymarket documents that an order is scoring only if it is live on a rewards-eligible market, satisfies min size, sits within valid spread, and has been live for the required duration.

### 5.4 Maker rebate EV

Rebate EV only exists in fee-enabled markets. Polymarket documents that most markets are zero-fee, while taker fees apply in all crypto markets deployed on or after March 6, 2026 and in select sports markets; those markets expose `feesEnabled=true` on the market object. Maker rebates are funded by taker fees, paid daily in USDC, and calculated per market using fee-equivalent maker liquidity.

Official fee-equivalent:

```
feeEq = C · p · feeRate_m · (p(1-p))^{exp_m}
```

Expected rebate:

```
E^reb_{m,j} = ρ̂_m · Σ_{fills ∈ H_A} feeEq
```

where `ρ̂_m` is our expected share of the market's total fee-equivalent maker liquidity.

Because Polymarket calculates totals per market, the allocator should prefer dominating one good rebate market over being mediocre in six rebate markets. That is the entire point of PMM-2.

### 5.5 Toxicity cost

We model toxicity as weighted markout:

```
C^tox_{m,j} = Σ_{o ∈ j} Q_o (0.5 M_{1s}(o) + 0.3 M_{5s}(o) + 0.2 M_{30s}(o))
```

where `M_Δ(o)` is adverse post-fill price movement at horizon Δ, signed against us.

This is measured from our recorder, not guessed.

### 5.6 Resolution cost

Polymarket explicitly states that the market title is not the payout rule, clarifications can be issued in rare cases, and disputed markets can take 4–6 days total to resolve. PMM-2 should therefore charge a real penalty for ambiguous rules, near-resolution exposure, and dispute risk.

Use:

```
C^res_{m,j} = α₁ A_m + α₂ / max(hoursToResolution_m, 6) + α₃ D_m + α₄ N_m
```

Where:
- `A_m`: ambiguity score from our rule-review layer
- `D_m`: dispute/clarification risk
- `N_m`: neg-risk placeholder penalty

## 6. Discrete allocator

### 6.1 Why discrete, not continuous

Our quotes are discrete, our tick sizes are discrete, reward thresholds are discrete, and our order slots are discrete. A continuous optimizer is pretending the market is smoother than it is.

So PMM-2 should:
1. Generate nested bundles per market
2. Compute V_{m,j} and R_{m,j}
3. Apply penalties for correlation, churn, and queue uncertainty
4. Greedily select the best feasible positive bundles until capital or slot budget is exhausted

Adjusted score:

```
R̃_{m,j} = R_{m,j} - λ · CorrPenalty_{m,j} - φ · ChurnPenalty_{m,j} - ψ · QueueUncertainty_{m,j}
```

Constraints:

```
Σ_{m,j} x_{m,j} Cap_{m,j} ≤ Cap_total
Σ_{m,j} x_{m,j} Slots_{m,j} ≤ Slots_total
Σ_j x_{m,j} Cap_{m,j} ≤ Cap^market_m
Σ_{m ∈ event e} Σ_j x_{m,j} Cap_{m,j} ≤ Cap^event_e
x_{m,3} ≤ x_{m,2} ≤ x_{m,1}
```

### 6.2 Reallocation hysteresis

We do not want the allocator thrashing markets in and out.

Target changes must clear:

```
|ΔCap_m| > max(0.1 · Cap_m, $500)
```

and score rank changes must persist for at least 3 allocator cycles before capital is moved, unless:
- inventory breach
- rules risk spike
- reward eligibility changed
- market resolved / halted
- structural arb appeared

## 7. Queue estimator

This is the core of the second half of V2.

For each live order o, maintain:
- entry time
- current price
- current open size
- estimated queue ahead A_o
- estimated queue behind B_o
- scoring flag
- fill ETA
- hold EV
- move EVs

### 7.1 Initialization

When our order becomes live:

```
A_o^init = VisibleSizeAtPrice - β Q_o
```

where β ∈ [0,1] is whether the venue snapshot likely includes our own size already. Default β = 0.5 until calibrated.

### 7.2 Update rule

Using orderbook deltas and our own fill messages:
- price-level decreases consume A_o first until zero
- our reported fills reduce our own remaining size
- new displayed size after our arrival is assumed behind us unless contradictory evidence appears

We maintain a bounded estimate:

```
A_o ∈ [A_o^low, A_o^high]
```

and use the midpoint for decisions, with uncertainty penalty:

```
QueueUncertainty_o = χ (A_o^high - A_o^low)
```

### 7.3 Fill hazard

```
P^fill_o(H_Q) = 1 - exp(-λ_{m,p_o}(t) · H_Q / (1 + κ A_o / Q_o))
```

where `λ_{m,p_o}(t)` is observed queue depletion intensity at our price.

Expected time to fill:

```
ETA_o ≈ (A_o + ρ Q_o) / d̂_{m,p_o}
```

where `d̂` is estimated queue depletion per second.

## 8. Persistence optimizer

The action set is:

```
A = {HOLD, IMPROVE1, IMPROVE2, WIDEN1, WIDEN2, CANCEL, CROSS}
```

For each live order:

```
EV_a = P^fill_a(H_Q) · Edge_a · Q_a + E^liq_a(H_Q) + E^reb_a(H_Q) - C^tox_a(H_Q) - ResetCost_a
```

Where:

```
ResetCost_a = 𝟙_{a ≠ HOLD} (QV_o + WarmupLoss_o + CancelCost_o)
```

Queue value:

```
QV_o = (P^fill(A_o, H_Q) - P^fill(A'_o, H_Q)) · (Edge_o + r^liq_o + r^reb_o) Q_o
```

where `A'_o` is expected queue ahead after cancel/repost.

Warmup loss is the reward loss from resetting a quote that is currently scoring or near-scoring. The key point is venue-documented: scoring depends on being live, valid, and old enough. So stale "refresh-everything" behavior can destroy reward EV even when our displayed prices look disciplined.

Decision rule:

```
a* = argmax_{a ∈ A} EV_a
```

Take a* ≠ HOLD only if:

```
EV_{a*} > EV_{HOLD} + ξ
```

with hysteresis:

```
ξ = ξ₀ + ξ₁ 𝟙_{scoring_o} + ξ₂ 𝟙_{ETA_o < 15s} + ξ₃ |inventorySkew_m|
```

This means entrenched/scoring orders require a much bigger reason to move.

### 8.1 Order states

Each quote has a persistence state:
- **NEW**: just posted, no edge in moving unless target drifts hard
- **WARMING**: eligible area but not yet confirmed scoring
- **SCORING**: confirmed scoring via endpoint
- **ENTRENCHED**: low queue ahead and strong hold EV
- **STALE**: fair value drift or toxicity broke the case
- **EXIT**: cancel or cross immediately

Transition rules:
- NEW → WARMING after live ack
- WARMING → SCORING if scoring endpoint true
- SCORING → ENTRENCHED if ETA short and queue value high
- ANY → STALE if target drifts by >= 2 ticks, toxicity spikes, or scoring drops
- STALE → EXIT if all alternative actions are negative EV

## 9. Quote planner

Allocator output is not raw orders. It is a target quote plan.

For market m, planner outputs:
- target capital
- target order slots
- target ladder prices
- target sizes
- max allowed churn
- priority class

Example:

```yaml
condition_id: "0xabc..."
target_capital_usdc: 4200
target_slots: 4
priority: reward_core
ladder:
  - side: bid
    price: 0.48
    size: 1200
    intent: reward_core
  - side: ask
    price: 0.52
    size: 1200
    intent: reward_core
  - side: bid
    price: 0.47
    size: 800
    intent: reward_depth
  - side: ask
    price: 0.53
    size: 800
    intent: reward_depth
max_reprices_per_minute: 3
```

The persistence optimizer then decides whether current live orders should actually move to that plan.

## 10. Execution integration

PMM-2 still uses post-only GTC/GTD for passive quotes and FOK/FAK only for arb or forced rebalancing. Batch submission should use `postOrders()` and keep requests to 15 orders max per batch. GTD remains the default for catalyst-sensitive markets, with the documented `now + 60 + N` expiration rule.

On any `tick_size_change` event, PMM-2 must immediately re-round all new intended prices before submission. Best-bid/ask-driven queue decisions should only run when `custom_feature_enabled: true` is active on the market WS subscription, since that feature gate is documented.

Heartbeats remain every 5 seconds. If we miss two in a row, PMM-2 must stop allocating new capital and put execution into PAUSED, because Polymarket documents that open orders are canceled if a valid heartbeat is not received within the allowed window. During weekly 425 restart windows, allocator and persistence both freeze and execution falls back to reconcile-only mode.

## 11. Storage additions

Add five durable tables:

```sql
market_score (
  ts timestamptz,
  condition_id text,
  bundle text,
  spread_ev_bps double precision,
  arb_ev_bps double precision,
  liq_ev_bps double precision,
  rebate_ev_bps double precision,
  tox_cost_bps double precision,
  res_cost_bps double precision,
  carry_cost_bps double precision,
  marginal_return_bps double precision,
  target_capital_usdc double precision,
  allocator_rank int,
  primary key (ts, condition_id, bundle)
);

queue_state (
  ts timestamptz,
  order_id text,
  condition_id text,
  side text,
  price double precision,
  size_open double precision,
  est_ahead_low double precision,
  est_ahead_mid double precision,
  est_ahead_high double precision,
  eta_sec double precision,
  scoring boolean,
  hold_ev_usdc double precision,
  best_alt_action text,
  best_alt_ev_usdc double precision,
  chosen_action text,
  primary key (ts, order_id)
);

allocation_decision (
  ts timestamptz,
  condition_id text,
  current_capital_usdc double precision,
  target_capital_usdc double precision,
  delta_capital_usdc double precision,
  reason text,
  confidence double precision,
  primary key (ts, condition_id)
);

reward_actual (
  date date,
  condition_id text,
  realized_liq_reward_usdc double precision,
  est_liq_reward_usdc double precision,
  capture_efficiency double precision,
  primary key (date, condition_id)
);

rebate_actual (
  date date,
  condition_id text,
  realized_rebate_usdc double precision,
  est_rebate_usdc double precision,
  capture_efficiency double precision,
  primary key (date, condition_id)
);
```

## 12. Runtime cadences

Use four loops:

**Event-driven loop** — Triggered by every market WS delta and every user WS fill/update.

**Fast loop: 250 ms** — Recompute queue states, ETA, hold EV, and action candidates.

**Medium loop: 10 s** — Refresh market-level EV components and bundle values.

**Allocator loop: 60 s** — Rebuild top bundle set, run greedy allocation, issue target plan deltas.

**Slow loop: 5 min** — Refresh active universe, reward-eligible markets, market metadata, rules flags.

**Daily loop: after midnight UTC** — Pull realized rebates, reconcile reward estimates, recalibrate liquidity/rebate models.

## 13. Default config

```yaml
pmm2:
  allocator_horizon_min: 60
  order_horizon_sec: 30
  allocator_interval_sec: 60
  scoring_check_sec: 30
  universe_refresh_sec: 300

  max_markets_active: 12
  max_slots_total: 48

  bundle_sizes:
    reward_core_mult: 1.0
    reward_depth_mult: 1.0
    edge_extension_mult: 0.5

  allocator:
    min_positive_return_bps: 6
    market_hysteresis_usdc: 500
    market_hysteresis_frac: 0.10
    corr_penalty_lambda: 0.20
    churn_penalty_phi: 0.15
    queue_uncertainty_penalty_psi: 0.10

  persistence:
    hysteresis_base_usdc: 0.25
    scoring_extra_usdc: 0.40
    eta_extra_usdc: 0.30
    max_reprices_per_minute_per_order: 2
    entrench_eta_sec: 15
    stale_drift_ticks: 2
    force_cancel_drift_ticks: 4

  risk:
    per_market_cap_nav: 0.03
    per_event_cap_nav: 0.06
    total_active_cap_nav: 0.30
    rebate_market_cap_nav: 0.10
    reward_market_cap_nav: 0.20

  calibration:
    markout_horizons_sec: [1, 5, 30]
    reward_model_half_life_days: 7
    rebate_model_half_life_days: 14
```

## 14. Success metrics

PMM-2 is working only if these all improve versus V1:

```
Reward Capture Efficiency = realized_liq_reward / estimated_liq_reward
Rebate Capture Efficiency = realized_rebate / estimated_rebate
Quote Churn Ratio = (cancels + replaces) / live_order_minutes
Queue Reset Loss = Σ_{reprices} (EV_hold^counterfactual - EV_move^actual)
```

Hard launch gates:
- positive passive PnL before rewards/rebates
- reward capture efficiency above 70%
- rebate capture efficiency above 70%
- scoring uptime above 85% in funded reward markets
- fill-to-cancel ratio improves by at least 25% versus V1
- queue reset loss negative trend eliminated
- no heartbeat-induced mass cancels from our own bug

## 15. Rollout plan

**Stage 0 — replay only** — Run allocator and persistence decisions against recorded V1 sessions. No live action.

**Stage 1 — shadow mode** — Live inputs, zero live order changes. Compare V1 actuals vs PMM-2 counterfactuals for 10 days.

**Stage 2 — 10% capital** — Only reward-eligible binary markets with clean rules. No augmented neg-risk.

**Stage 3 — 25% capital** — Add fee-enabled rebate markets if rebate estimator is calibrated.

**Stage 4 — production V2** — Allocator controls all reward/rebate market budgets. V1 still handles execution and safety.

## 16. Main loop pseudocode

```python
async def allocator_loop(state):
    universe = build_universe(state)  # Gamma + sampling markets
    live = snapshot_live_state(state)  # books, fills, prices, queue states
    bundles = []

    for market in universe:
        features = score_features(market, live)
        bundles.extend(generate_nested_bundles(market, features))

    feasible = [b for b in bundles if b.value_usdc > 0]
    plan = greedy_allocate(
        bundles=feasible,
        capital_cap=state.capital_cap,
        slot_cap=state.slot_cap,
        market_caps=state.market_caps,
        event_caps=state.event_caps,
    )
    state.target_plan = plan


async def persistence_loop(state):
    for order in state.live_orders:
        actions = enumerate_actions(order, state.target_plan, state.live_books)
        scored = {a: action_ev(order, a, state) for a in actions}
        best = max(scored, key=scored.get)

        if scored[best] > scored["HOLD"] + hysteresis(order, state):
            await apply_action(order, best, state)
```

## 17. Final stance

This is the right V2. It gets us from "competent bot" to "capital-efficient bot."

It is still not ultimate full capacity. After PMM-2, the next real multiplier is V3 resolution intelligence, because Polymarket resolution depends on written rules rather than headlines, clarifications can matter, and disputed resolution can take multiple days.

The correct sequence is:

**V1 execution safety → V2 allocator + queue persistence → V3 resolution intelligence → V4 directional/event graph overlay**

If we skip that order, we will overcomplicate the bot before we have earned the right to. The next thing we should do is convert this spec into an engineering work breakdown with tickets, owners, and build order.

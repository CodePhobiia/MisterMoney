# PMM-1 — Production v1 Spec

> "The right v1 is not a giant 'AI prediction bot.' It is a disciplined execution system centered on structural arb and reward-aware passive quoting, with directional views as a later overlay."

---

## 1. Bot Definition

- **Name:** PMM-1
- **Architecture:** Modular monolith, Python 3.12, asyncio
- **Deployment:** One live execution service + one separate research/backtest pipeline
- **Wallet:** Dedicated EOA wallet for v1, server-side only
- **SDK:** Official Python CLOB client (not raw REST — manual order signing involved)
- **APIs:** Gamma (discovery), Data API (positions/trades/analytics), CLOB (prices/orderbooks/order management)
- **Why EOA over Safe:** Fastest route to ship. Keep enough POL for approvals, splits/merges, emergency onchain cancels. Safe = v2.

---

## 2. Official Constraints

- All orders are limit orders under the hood. GTC/GTD = resting; FOK/FAK = marketable.
- `postOnly` only works with GTC/GTD; rejected if it would cross.
- **GTD security threshold:** Set expiration to `now + 60 + N` for effective lifetime of N seconds.
- **Tick size:** Must conform. Changes dynamically near extremes. `tick_size_change` events are critical.
- **Negative-risk markets:** Require `negRisk: true` in order options. No→Yes conversion via Neg Risk adapter.
- **Market WebSocket:** Book snapshots, price deltas, last trades, tick-size changes, best_bid_ask (with `custom_feature_enabled: true`).
- **User WebSocket:** Authenticated, server-side only. Order/trade updates: MATCHED→MINED→CONFIRMED→RETRYING→FAILED.
- **REST Heartbeat:** Must receive within 10s (+5s buffer) or all orders canceled. Send every 5s, track `heartbeat_id`.
- **Batch orders:** POST /orders capped at 15 per request.
- **Matching engine restarts:** Weekly Tuesday 7:00 AM ET, ~90s, returns HTTP 425. 429 = rate limit. 503 = paused/cancel-only.
- **Geoblocking:** Orders from blocked regions rejected. Check lives on polymarket.com, not API hosts.

---

## 3. Product Scope

**What v1 trades:**
1. High-liquidity binary markets with clear rules
2. Negative-risk multi-outcome events with enough depth for conversion arb
3. Reward-eligible markets when reward EV is real

**What v1 does NOT trade:**
1. Sports (orders canceled at game start, 3s placement delay, shifting start times)
2. Intraday crypto
3. Ambiguous markets
4. Pure headline/news race strategies

---

## 4. Strategy Stack

### A. Binary Parity Arbitrage
```
A = ask_YES + ask_NO + costs
If A < 1 − ε → buy both sides

B = bid_YES + bid_NO − costs
If B > 1 + ε → split collateral into YES/NO, sell both
```
Pure structure. No prediction needed. Needs correct fees, slippage, and inventory plumbing.

### B. Negative-Risk Conversion Arbitrage
For negative-risk events, No on outcome k converts into Yes on all other outcomes.

Two canonical checks:
```
ask(No_k) + conversion_cost + exit_cost < Σ_{j≠k} bid(Yes_j)
Σ_{j≠k} ask(Yes_j) + entry_cost < bid(No_k) − conversion_cost
```
If either holds after safety buffer → execute.

> "Strongest non-predictive edge in multi-outcome markets — exploits documented token relationships, not opinion."

### C. Reward-Aware Passive Market Making (Main Engine)
Quote both sides around reservation price. Optimize for EV after:
- adverse selection
- fill probability
- inventory cost
- liquidity rewards
- maker rebates

Liquidity rewards: tight balanced quoting, two-sided boost, single-sided discount `c = 3.0`. At extremes, double-sided required.

Maker rebates: daily USDC, proportional to maker liquidity share.

Fee-equivalent formula:
```
fee = C × p × feeRate × (p(1−p))^exponent
```
Different fee-rate/exponent for sports vs crypto.

### D. Directional Overlay
**Disabled by default in v1.** When enabled: only crosses spread when edge survives fees + slippage + model haircut + resolution risk + correlation caps.

> "We will not ship a 'forecasting hero bot' first."

---

## 5. Universe Selection

**Discovery:** Gamma `events?active=true&closed=false`

**Intersect with:**
- `enableOrderBook = true`
- Reward-eligible from `getSamplingMarkets()` / `getSamplingSimplifiedMarkets()`
- Manual allow-list with clear resolution rules

### Eligibility Filter
```
eligible_i = active_i ∧ orderbook_i ∧ clearRules_i ∧ liquid_i ∧ safeTime_i
```

**Default thresholds:**
- `time_to_end > 24h` (unless pure parity/neg-risk arb)
- 24h volume above configurable threshold
- Top-of-book spread below configurable threshold
- Enough depth within 2–3¢ of mid
- No unresolved rule ambiguity
- No recent clarification or dispute escalation

> "The title is not the payout rule."

### Universe Score
```
U_i = w_1·log(1+vol_24h) + w_2·log(1+depth_2c) − w_3·spread_i − w_4·tox_i − w_5·resolutionRisk_i + w_6·rewardEV_i + w_7·arbEV_i
```
Trade top K only. **V1 default: K = 20.**

---

## 6. Pricing Model

### Core Features
- midpoint `m_t`
- microprice `μ_t = (ask·bidSize + bid·askSize) / (bidSize + askSize)`
- book imbalance `I_t = (bidSize − askSize) / (bidSize + askSize)`
- recent signed trade flow `F_t`
- short-horizon realized volatility `V_t`
- time-to-resolution `T_t`
- related-market residuals `R_t`
- external signal `E_t` (when available)

### Fair Value
```
x_t = β_0 + β_1·logit(m_t) + β_2·logit(μ_t) + β_3·I_t + β_4·F_t + β_5·R_t + β_6·E_t
p̂_t = σ(x_t)
```

**Model class:** Logistic regression + isotonic calibration first. Boosted trees only after live recorder + clean feature history.

### Model Haircut
```
h_t = h_0 + k_1·V_t + k_2·stale_t + k_3·resolutionRisk_t + k_4·modelError_t
```
No taking flow unless edge exceeds `h_t`.

---

## 7. Quote Engine

### Reservation Price
```
r_t = clip(p̂_t − γ·q_t − η·q_t^cluster, ε, 1−ε)
```
- `q_t` = signed market inventory
- `q_t^cluster` = correlated exposure across linked markets/events

### Fill Model (Queue-Aware)
```
P(fill | Δ, qpos, τ) = 1 − e^{−λ(Δ,qpos)·τ}
λ(Δ, qpos) = exp(θ_0 − θ_1·Δ − θ_2·qpos + θ_3·flow)
```
- `Δ` = distance from best price
- `qpos` = estimated queue position
- `flow` = recent trade/sweep intensity

### Quote Objective
```
EV = P_b·(r_t − bid − AS_b) + P_a·(ask − r_t − AS_a) + EV^liq + EV^rebate − InvPenalty
```

### Quote Widths
```
δ_t = max(tick/2, δ_0 + δ_t^tox + δ_t^lat + δ_t^vol − δ_t^reward)
bid_t = ⌊r_t − δ_t⌋_tick
ask_t = ⌈r_t + δ_t⌉_tick
```

### Size Model
```
size_t = min(s_max, (s_0 · conf_t · rewardBoost_t) / (1 + k|q_t|))
```
Smaller when: high volatility, high toxicity, imbalanced inventory, short time to catalyst.

### Crossing Rule
```
takeEV = (p̂_t − p_exec)·Q − fee − slippage − h_t
```
Only cross if `takeEV > take_threshold` AND no cluster/drawdown breach.

---

## 8. Reward Estimator

### Liquidity Rewards
```
EV^liq_share = (rewardShare_m · rewardPool_m) / expectedFilledShares_m
```
Score: quadratic in distance from mid, min-of-sides for two-sided boost, discount `c=3.0` for single-sided in [0.10, 0.90]. **Quote both sides always on reward markets.**

### Maker Rebates
```
EV^rebate = shareOfFeeEquivalent_m · rebatePool_m
```
Updated daily via `/rebates/current` endpoint reconciliation.

---

## 9. Execution Architecture

**Modular monolith. Not microservices.**

### Modules
1. **market_sync** — active markets, reward-eligible, tick size, neg-risk flags, fee rates, rules
2. **ws_market** — subscribe asset IDs, local books from snapshot+deltas, tick_size_change
3. **ws_user** — authenticated order/trade updates, fills, settlement tracking
4. **feature_engine** — microprice, imbalance, flow, volatility, event clocks
5. **arb_engine** — binary parity, neg-risk conversion detection
6. **mm_engine** — fair value, reservation price, quote width, size
7. **risk_engine** — limits, kill switches, market state transitions
8. **order_manager** — sign, batch, diff, cancel/replace, reconcile
9. **inventory_manager** — split/merge/redeem, event-level rebalancing
10. **persistence** — raw events, normalized state, PnL ledger, model features/decisions

### Storage
- **Redis:** Hot state, books, live orders, heartbeat
- **Postgres:** Durable events, orders, fills, positions, PnL
- **Parquet + DuckDB/Polars:** Research, replay, backtest

---

## 10. Startup Sequence

1. Geoblock check
2. Load secrets from KMS / environment
3. Create or derive L2 API creds
4. Sync server time
5. Fetch universe from Gamma + sampling markets
6. Fetch market metadata: tick size, neg-risk, fee rate, rules
7. Load current open orders and balances
8. Connect market WS
9. Connect user WS
10. Start REST heartbeat loop
11. Enter WARMUP → QUOTING

---

## 11. Runtime State Machines

### Market State
```
DISCOVERED → ELIGIBLE → QUOTING → PAUSED → FLATTEN_ONLY → RESOLVED
```
- ELIGIBLE→PAUSED: stale data, rule ambiguity, restart window, drawdown
- PAUSED→FLATTEN_ONLY: severe disconnect, heartbeat miss, cancel-only mode
- ANY→RESOLVED: market resolves

### Order State
```
INTENT → SIGNED → SUBMITTED → LIVE|MATCHED|DELAYED → PARTIAL → FILLED|CANCELED|EXPIRED|FAILED
```

---

## 12. Order Manager Rules

### Passive Quoting
- `postOnly=true` with GTC or short GTD
- TTL default: 20–45s effective
- Reprice on: price move ≥1 tick, size move ≥20%, age>TTL, tick size change, fill changes inventory

### Taker Execution
- FAK for partial immediate; FOK for all-or-nothing arb

### Batch Behavior
- Diff desired vs live order set
- Chunks of 15 max
- Per cycle: cancel stale → submit new → reconcile

### Heartbeats
- Every 5s, persist `heartbeat_id`
- 2 failures → PAUSED
- Market data stale >2s → cancel all, FLATTEN_ONLY

### Reconciliation
- Open orders every 30s
- Positions/trades every 60s
- After reconnect: full reconciliation before resuming

---

## 13. Inventory Engine

### Rules
Track per market: YES inventory, NO inventory, reserved in open orders, event-level net exposure.
```
freeInventory = balance − Σ(openOrderRemaining)
```

### Rebalancing Priority
1. Merge stale paired inventory
2. Split new collateral
3. Internal cross-event conversion (neg-risk)
4. External market hedge (last resort)

### Resolution Handling
- Stop quoting well before resolution
- Freeze inventory accumulation if dispute/clarification risk elevated
- Redeem only after confirmed resolution
- Disputes can escalate through challenge rounds + UMA voting (days)

---

## 14. Risk Engine

### Hard Caps (v1 defaults)
| Limit | Default |
|-------|---------|
| Per-market gross | 2% NAV |
| Per-event cluster | 5% NAV |
| Total directional net | 10% NAV |
| Total arb gross | 25% NAV |
| Max orders per market side | 3 |
| Max quoted markets | 20 |

### Dynamic Caps
Shrink on: rising volatility, rising model error, falling time-to-catalyst, deepening drawdown, falling reward EV.

### Drawdown Governor
| Trigger | Action |
|---------|--------|
| Daily DD > 1.5% NAV | Pause taker trades |
| Daily DD > 2.5% NAV | Quote wider, cut sizes 50% |
| Daily DD > 4% NAV | FLATTEN_ONLY |

### Kill Switches — Immediate `cancelAll()`
- Stale market feed
- Heartbeat failure
- Position breach
- Repeated 400/401 auth failures
- 503 exchange pause/cancel-only
- Unresolved reconciliation mismatch

---

## 15. Failure Handling

| Scenario | Response |
|----------|----------|
| **425 restart** | Pause, exponential backoff from 1–2s, reconcile before resume |
| **429 rate limit** | Exponential backoff, reduce REST, prefer WS |
| **503 cancel-only** | Cancel only, no new orders. If trading disabled, stop all + page ops |
| **WS disconnect** | Freeze quoting, cancel all, reconnect, resubscribe, rebuild books, reconcile, resume |

---

## 16. Backtesting & Evaluation

> "A serious Polymarket MM bot needs our own live recorder. No public historical L2 order-book replay endpoint exists."

### Backtest Layers
1. **Coarse historical:** prices, trades, metadata, outcomes
2. **Queue-aware simulator:** our recorded book snapshots/deltas, quote intents, queue-position model, fill simulation
3. **Shadow live / paper:** full live data, zero real orders, compare theoretical vs actual fills

### PnL Decomposition
- Spread capture
- Adverse selection (1s / 5s / 30s)
- Inventory carry PnL
- Arb locked-in PnL
- Maker rebates
- Liquidity rewards
- Slippage
- Reject/cancel costs

### Acceptance Criteria for Live Launch
- 30 consecutive paper days with positive net expectancy
- Quote uptime > 99%
- Order reject rate < 0.5% (excluding 425/503)
- Zero heartbeat mass-cancels from our bug
- Positive realized spread (with and without rewards)
- 5-second adverse selection < 60% of spread capture

---

## 17. Testing Plan

### Unit Tests
tick rounding, neg-risk conversion math, parity detector, inventory reservations, order diffing, heartbeat state, risk-limit transitions

### Integration Tests
auth flow, order submission/cancel, WS reconnect, 425 handling, 429 backoff, invalid tick-size rejection recovery. Use staging hosts where available.

### Chaos Tests
kill market WS mid-quote, kill user WS mid-fill, force heartbeat miss, force tick-size change, simulate 503 cancel-only, corrupt local book → verify self-pause

---

## 18. Default Config

```yaml
bot:
  name: PMM-1
  env: prod
  max_markets: 20
  quote_cycle_ms: 250
  reconcile_orders_s: 30
  reconcile_positions_s: 60

wallet:
  type: EOA
  chain_id: 137

market_filters:
  min_time_to_end_hours: 24
  min_volume_24h_usd: 50000
  max_top_spread_cents: 4
  min_depth_within_2c_shares: 2000
  allow_sports: false
  allow_crypto_intraday: false
  require_clear_rules: true

strategy:
  enable_binary_parity: true
  enable_neg_risk_arb: true
  enable_market_making: true
  enable_directional_overlay: false

pricing:
  base_half_spread_cents: 1.0
  inventory_skew_gamma: 0.015
  cluster_skew_eta: 0.02
  take_threshold_cents: 0.8
  reward_capture_weight: 0.7

risk:
  per_market_gross_nav: 0.02
  per_event_cluster_nav: 0.05
  total_directional_nav: 0.10
  total_arb_gross_nav: 0.25
  daily_pause_drawdown_nav: 0.015
  daily_flatten_drawdown_nav: 0.04

execution:
  post_only: true
  order_ttl_effective_s: 30
  heartbeat_s: 5
  ws_stale_kill_s: 2
  max_batch_orders: 15
  retry_backoff_initial_ms: 1000
  retry_backoff_max_ms: 30000
```

---

## 19. Repo Layout

```
pmm1/
├── pyproject.toml
├── README.md
├── .env.example
├── config/
│   ├── default.yaml
│   └── prod.yaml
├── pmm1/
│   ├── main.py
│   ├── settings.py
│   ├── logging.py
│   ├── api/
│   │   ├── gamma.py
│   │   ├── clob_public.py
│   │   ├── clob_private.py
│   │   ├── data_api.py
│   │   └── geoblock.py
│   ├── ws/
│   │   ├── market_ws.py
│   │   └── user_ws.py
│   ├── state/
│   │   ├── books.py
│   │   ├── orders.py
│   │   ├── positions.py
│   │   ├── inventory.py
│   │   └── heartbeats.py
│   ├── strategy/
│   │   ├── universe.py
│   │   ├── features.py
│   │   ├── fair_value.py
│   │   ├── quote_engine.py
│   │   ├── binary_parity.py
│   │   ├── neg_risk_arb.py
│   │   ├── directional.py
│   │   └── rewards.py
│   ├── risk/
│   │   ├── limits.py
│   │   ├── kill_switch.py
│   │   ├── drawdown.py
│   │   └── resolution.py
│   ├── execution/
│   │   ├── order_manager.py
│   │   ├── batcher.py
│   │   ├── reconciler.py
│   │   └── tick_rounding.py
│   ├── storage/
│   │   ├── postgres.py
│   │   ├── redis.py
│   │   └── parquet.py
│   ├── analytics/
│   │   ├── pnl.py
│   │   ├── metrics.py
│   │   └── attribution.py
│   └── backtest/
│       ├── recorder.py
│       ├── replay.py
│       ├── simulator.py
│       └── queue_model.py
└── tests/
    ├── unit/
    ├── integration/
    └── chaos/
```

---

## 20. Main Loop Pseudocode

```python
async def run():
    await geoblock_check()
    creds = await load_or_create_api_creds()
    server_time = await clob.get_server_time()

    universe = await build_universe()
    metadata = await load_market_metadata(universe)

    state = await bootstrap_state(metadata)
    await connect_market_ws(state)
    await connect_user_ws(state, creds)

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    reconcile_task = asyncio.create_task(reconcile_loop())
    market_task = asyncio.create_task(market_event_loop())
    user_task = asyncio.create_task(user_event_loop())

    while True:
        if state.kill_switch:
            await order_manager.cancel_all()
            state.mode = "FLATTEN_ONLY"
            await asyncio.sleep(1)
            continue

        for market in state.eligible_markets():
            features = feature_engine.compute(market, state)
            arb_orders = arb_engine.find(market, state, features)

            if arb_orders:
                await order_manager.execute_arb(arb_orders)
                continue

            quote_intent = mm_engine.quote(market, state, features)
            checked_intent = risk_engine.apply(quote_intent, state)

            await order_manager.diff_and_apply(checked_intent)

        await asyncio.sleep(0.25)
```

---

## 21. Final Stance

> "The right v1 is not a giant 'AI prediction bot.' It is a disciplined execution system centered on structural arb and reward-aware passive quoting, with directional views as a later overlay. The first thing we should build is the recorder/backtester and paper trader, then the live execution layer."

**Build order:**
1. Recorder + backtester + paper trader
2. Live execution layer

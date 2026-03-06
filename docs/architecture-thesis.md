# Polymarket Bot — Architecture Thesis (Theyab, 2026-03-06)

Source: Direct message from Theyab correcting initial LMSR-based approach.

## Core Correction
LMSR is the wrong venue model. Polymarket runs a **hybrid-decentralized CLOB on Polygon** with limit orders, real-time WebSocket feeds, tick sizes, negative-risk handling, and heartbeat-based order safety. NOT an AMM.

## Opportunity Stack (ranked by robustness)
1. **YES/NO parity and negative-risk arbitrage** — structural, not predictive
2. **Reward-aware market making** on clear, liquid markets
3. **Selective event-driven trading** with better/faster data
4. **Pure latency sniping** — lowest priority

## Seven Core Formulas

### 1) Fair Value — Market as Prior, Model as Evidence (Logit-Space Blending)
```
p̂_t = σ((1-λ)·logit(m_t) + λ·logit(p^ext_t) + β^T·z_t)
```
- m_t = current executable midpoint
- p^ext_t = external model probability
- z_t = related-market, news, order-book, time-to-resolution features
- Market IS the prior. Only move off it with actual evidence.

### 2) Inventory-Aware Reservation Price (Avellaneda-Stoikov adapted)
```
r_t = clip(p̂_t - γ·q_t·σ_t²·τ_t, ε, 1-ε)
```
- q_t = signed inventory in YES-equivalent units
- σ_t = short-horizon midpoint volatility
- τ_t = time to next quote refresh or catalyst
- γ = inventory aversion
- "This one formula is worth more than most fancy prediction models"

### 3) Quote Objective — EV per Quote, Not Tightest Spread
```
EV(b,a) = λ_b·(r_t - b - c^AS_b) + λ_a·(a - r_t - c^AS_a) + R^rebate(a,b) + R^liq(a,b) - C^inv(q_t)
```
Fill rate model: `λ(Δ) = A·e^(-κΔ)`

Simpler production approximation:
```
δ_t = max(tick/2, c_as + c_lat + c_res - c_rebate - c_reward)
bid = floor(r_t - δ_t, tick)
ask = ceil(r_t + δ_t, tick)
```

### 4) Structural Incentive Formulas
Fee-enabled rebate markets:
```
feeEquivalent = C × p × feeRate × (p(1-p))^exponent
rebate = (your_feeEquivalent / total_feeEquivalent) × rebatePool
```
Rebate value highest near mid-probability. Crypto has higher exponent.

Liquidity rewards proxy:
```
score^liq_i ∝ size_i · (1 - spread_i/v)² × twoSidedBonus
```

### 5) Take/Pass Rule
```
edge = ŵ - c_net   (c_net includes fees, slippage, model-risk haircut)
Only cross spread when: edge > h
```
h must cover: model calibration buffer + latency/adverse-selection buffer + resolution-risk buffer

### 6) Fractional Kelly (NEVER full Kelly)
```
f* = η · max(0, (ŵ - c_net)/(1 - c_net))
η ∈ [0.1, 0.25]
```
Plus hard caps: per-market caps, correlated-cluster caps, drawdown throttles.

### 7) Parity / Conversion Arbitrage
Binary:
```
ask_YES + ask_NO + costs < 1  →  buy both
bid_YES + bid_NO - costs > 1  →  split USDC into YES/NO, sell both
```
Multi-outcome negative-risk:
```
Σ_i ask_i + costs < 1  →  basket buy
```

## Architecture Decisions
- Use Gamma/CLOB APIs for discovery and metadata
- Subscribe to market AND user WebSocket channels
- Batch quote updates
- GTD orders around catalysts (auto-expire stale quotes)
- Decompose PnL: spread capture, rebates, liquidity rewards, directional alpha, slippage, adverse selection
- Filter markets aggressively: clear resolution rules, clean data sources, enough depth, favorable incentives
- Handle tick size correctly (changes near extremes)
- Continuous heartbeats, kill switch on model failure or position breach
- Check geoblocking endpoint before deployment

## What NOT To Do
- ❌ Build around LMSR (wrong venue model)
- ❌ Full Kelly (too fragile)
- ❌ Quote through catalysts without GTD/auto-cancel
- ❌ Trade ambiguous "vibes" markets
- ❌ Optimize only for liquidity rewards without toxicity model

## Recommendation
**Hybrid reward-aware market maker with parity/negative-risk arbitrage and Bayesian fair-value overlay.**

## Next Step
Concrete bot spec: market filters, data schema, quote loop, risk limits, backtest ledger measuring true edge.

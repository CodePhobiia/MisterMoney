# Sprint 4 Summary — Market EV Scorer

**Status:** ✅ Complete  
**Commit:** 766c36e  
**Date:** 2026-03-07

## What Was Built

Sprint 4 implemented the complete Market EV Scorer under `pmm2/scorer/`, following the value model:

```
V = E^spread + E^arb + E^liq + E^reb - C^tox - C^res - C^carry
```

### Files Created

1. **`pmm2/scorer/__init__.py`** — Package initialization
2. **`pmm2/scorer/bundles.py`** — QuoteBundle model and B1/B2/B3 bundle generator
3. **`pmm2/scorer/spread_ev.py`** — Spread capture EV computation
4. **`pmm2/scorer/arb_ev.py`** — Arbitrage opportunity detection
5. **`pmm2/scorer/reward_ev.py`** — Liquidity reward EV with proxy scoring function
6. **`pmm2/scorer/rebate_ev.py`** — Maker rebate EV from fee sharing
7. **`pmm2/scorer/toxicity.py`** — Adverse selection cost from fill markouts
8. **`pmm2/scorer/resolution.py`** — Market resolution risk cost
9. **`pmm2/scorer/combined.py`** — MarketEVScorer class integrating all components

## Architecture

### Bundle Types

- **B1 (Reward Core):** Minimum viable two-sided quote at inside spread
- **B2 (Reward Depth):** Additional depth within reward spread band (only if reward-eligible)
- **B3 (Edge Extension):** Extra depth for spread capture (only if spread is wide enough)

Bundles are nested: B3 ⊆ B2 ⊆ B1. At $104 NAV, most markets only get B1.

### Value Components

**Positive (Revenue):**
- `spread_ev` — Expected profit from bid-ask spread capture
- `arb_ev` — Binary parity arbitrage (stub for future multi-outcome data)
- `liq_ev` — Expected liquidity mining rewards from Polymarket
- `rebate_ev` — Expected maker fee rebates

**Negative (Costs):**
- `tox_cost` — Adverse selection from toxic flow (learned from fill markouts)
- `res_cost` — Market resolution risk (ambiguity, time, dispute risk, neg-risk placeholders)
- `carry_cost` — Capital carry cost (0.5% daily default)

### Scale Awareness

At $104 NAV:
- Per-market capital limit: `max(nav * 0.03, $8)` = **$8**
- Most markets get **B1 only**
- B2/B3 generated only when:
  - Market is reward-eligible (B2)
  - Spread is wide enough (B3)
  - Capital allows

### MarketEVScorer API

```python
from pmm2.scorer.combined import MarketEVScorer
from pmm1.storage.database import Database
from pmm2.queue.hazard import FillHazard
from pmm2.queue.estimator import QueueEstimator

scorer = MarketEVScorer(db, fill_hazard, queue_estimator)

# Score all bundles for a market
scored_bundles = await scorer.score_market(
    market=enriched_market,
    nav=104.0,
    reservation_price=None  # defaults to mid
)

# Bundles sorted by marginal_return desc
best = scored_bundles[0]
print(f"Best bundle: {best.bundle_type}, return: {best.marginal_return:.4f}")

# Persist to database
await scorer.persist_scores(scored_bundles)
```

## Implementation Details

### Spread EV (`spread_ev.py`)

- Bid edge: `reservation_price - bid_price`
- Ask edge: `ask_price - reservation_price`
- EV = `P(fill) * edge * size` for each side
- Fill probabilities from FillHazard with 30s horizon

### Reward EV (`reward_ev.py`)

Implements Polymarket's proxy scoring function:
```
g(s, v) = (1 - s/v)²₊
```

Side combination (c=3.0):
```
If mid ∈ [0.10, 0.90]: Q_pair = max(min(Q₁, Q₂), max(Q₁/c, Q₂/c))
Otherwise: Q_pair = min(Q₁, Q₂)
```

Expected reward:
```
E_liq = pool_daily_rate × (Q_pair / (Q_pair + Q_others)) × (H_A / T_epoch) × P(scoring)
```

Defaults:
- `Q_others` = 10% of market liquidity (min 50 shares)
- `P(scoring)` = 0.85
- `H_A / T_epoch` = 1/24 (1h allocator horizon in 24h epoch)

### Toxicity (`toxicity.py`)

Queries `fill_record` table for recent fills (24h lookback):
```
tox = 0.5 × avg(markout_1s) + 0.3 × avg(markout_5s) + 0.2 × avg(markout_30s)
```

Cold start default: 0.001 (1bp)  
Cached per market with 1h TTL

### Resolution Cost (`resolution.py`)

```
C_res = α₁ × ambiguity + α₂ / max(hours_to_resolution, 6) + α₃ × dispute_risk + α₄ × neg_risk_placeholder
```

Defaults:
- α₁ = 0.002 (ambiguity)
- α₂ = 0.001 (time)
- α₃ = 0.003 (dispute)
- α₄ = 0.002 (placeholder)

### Persistence

Scores written to `market_score` table with all components in basis points:
- `spread_ev_bps`
- `arb_ev_bps`
- `liq_ev_bps`
- `rebate_ev_bps`
- `tox_cost_bps`
- `res_cost_bps`
- `carry_cost_bps`
- `marginal_return_bps`
- `target_capital_usdc`
- `allocator_rank` (filled by allocator in Sprint 5)

## Testing

All modules tested and importable:
```bash
$ python3 -c "from pmm2.scorer.combined import MarketEVScorer; from pmm2.scorer.bundles import generate_bundles; print('OK')"
✅ All imports successful
```

## Next Steps (Sprint 5 — Capital Allocator)

1. Build `pmm2/allocator/` — optimal capital allocation across markets
2. Implement knapsack solver with marginal return sorting
3. Add rebalancing logic (capital add/remove decisions)
4. Integrate with persistence layer (Sprint 2)
5. Wire up to main bot loop

## Notes

- Arbitrage EV currently returns 0 (stub) — requires full multi-outcome book data
- Fill probabilities use FillHazard defaults (queue_ahead = 0) — will improve with real queue tracking
- Rebate and reward EVs use rough proxies for market share — can be refined with actual depth data
- Carry cost assumes 0.5% daily rate (adjustable parameter)

---

**Sprint 4 Complete.** All scorer modules built, tested, committed, and pushed.

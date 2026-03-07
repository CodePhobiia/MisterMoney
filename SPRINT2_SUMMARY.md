# Sprint 2 Summary — Universe + Metadata Layer

**Status:** ✅ Complete  
**Committed:** dd2bc9d  
**Date:** 2026-03-07

## What Was Built

Created complete `pmm2/universe/` layer with 5 core modules:

### S2-1: RewardSurface (`reward_surface.py`)
- Dual-indexed reward eligibility tracking
- Indexes by both `condition_id` AND `token_id` (critical for Gamma API quirk)
- 5-minute TTL cache
- `is_eligible()` method tries condition_id → token_id_yes → token_id_no
- `get_reward_info()` returns (eligible, daily_rate, min_size, max_spread)

**Why it matters:** Gamma `/markets` returns EMPTY `condition_id` fields. Token-based fallback is essential for reward matching.

### S2-2: FeeSurface (`fee_surface.py`)
- Tracks which markets have `fees_enabled=true`
- Stores fee rates when available
- 5-minute TTL cache
- `update_from_markets()` ingests raw Gamma data
- `is_fee_enabled()` and `get_fee_rate()` accessors

**Why it matters:** Fee-enabled markets provide maker rebates, improving profitability.

### S2-3: EnrichedMarket Model (`metadata.py`)
- Unified pydantic model with ALL scoring inputs:
  - Identifiers (condition_id, token_ids, event_id)
  - Book state (bid/ask/mid/spread)
  - Volume & liquidity
  - Reward parameters
  - Fee info
  - Risk metadata (time to resolution, ambiguity score, neg_risk)
  - Trading flags (active, accepting_orders)

- `compute_ambiguity_score()` heuristic:
  - Vague keywords ("approximately", "around", "roughly") → +0.2 each
  - Long title (>100 chars) → +0.1
  - Contains "or" → +0.1
  - Numbers/dates → -0.1 (specificity reduces ambiguity)
  - Clamped to [0, 1]

**Why it matters:** Single source of truth for all market metadata. Eliminates scattered data fetching.

### S2-4: UniverseScorer (`scorer.py`)
- Composite scoring formula:
  ```
  base = log(1 + volume_24h) * (1 / max(spread_cents, 0.5))
  reward_bonus = reward_daily_rate * 10 if reward_eligible
  fee_bonus = 2.0 if fees_enabled
  risk_penalty = ambiguity_score * 5 + (1 / max(hours_to_resolution, 6)) * 2
  extreme_penalty = 10 if mid < 0.05 or mid > 0.95
  score = base + reward_bonus + fee_bonus - risk_penalty - extreme_penalty
  ```

- `select_top()` scores all, sorts descending, returns top N
- Logs: total candidates, selected count, reward-eligible count, fee-enabled count, top score

**Why it matters:** Balances profitability (volume, spread, rewards, fees) against risk (ambiguity, resolution time, extreme prices).

### S2-5: Integration Function (`build.py`)
- `async def build_enriched_universe()` ties everything together
- Steps:
  1. Fetch top 200 markets from Gamma (sorted by volume_24hr)
  2. Refresh reward surface
  3. Build fee surface from Gamma data
  4. Enrich each market with reward/fee/metadata
  5. Return list of EnrichedMarket objects

- Does NOT do scoring/selection — that's UniverseScorer's job
- Pure data layer — builds enriched metadata for downstream use

**Why it matters:** Clean separation of concerns. Enrichment → Scoring → Selection.

## Code Quality

✅ **Style Consistency:**
- structlog logging throughout
- pydantic models with proper Field() declarations
- Type hints everywhere
- async/await pattern
- Proper docstrings

✅ **Testing:**
- `test_pmm2_universe.py` validates all components
- Tests EnrichedMarket model, ambiguity scoring, RewardSurface, FeeSurface, UniverseScorer
- All tests passing

✅ **Architecture:**
- Modular design — each file has single responsibility
- TTL caching on surfaces (5 min default)
- Fallback logic for missing data (token_id fallback, safe defaults)

## Key Design Decisions

### 1. Dual Indexing (RewardSurface)
**Problem:** Gamma `/markets` returns empty `condition_id` fields.  
**Solution:** Index rewards by both `condition_id` AND `token_id`. Fallback to token matching when condition_id is missing.

### 2. Separate Enrichment from Scoring
**Why:** Allows testing, debugging, and future strategies to use the same enriched data layer.  
**Implementation:** `build.py` creates EnrichedMarket objects. `scorer.py` scores them. Clean pipeline.

### 3. Ambiguity Scoring Heuristic
**Why:** Resolution risk is hard to quantify. Ambiguous questions → disputes → losses.  
**Heuristic:** Keyword matching + length + structure. Not perfect, but better than nothing.  
**Future:** ML model trained on historical dispute data.

### 4. Composite Scoring
**Why:** No single metric (volume, spread, rewards) is sufficient. Need multi-dimensional optimization.  
**Formula:** Base (volume/spread) + Bonuses (rewards, fees) - Penalties (risk, extremes).  
**Tunable:** Coefficients (10, 2.0, 5, 2) can be config-driven in future.

## What's NOT Done (By Design)

❌ **NOT integrated into `pmm1/main.py`** — Sprint 3 task  
❌ **NOT scoring V1 markets** — pmm2 is standalone layer  
❌ **NOT replacing universe.py** — parallel implementation for now  
❌ **NOT fetching event_id from Gamma** — left as TODO (needs event endpoint)  
❌ **NOT detecting placeholder outcomes** — left as TODO (low priority)

## File Structure

```
pmm2/
├── __init__.py
├── queue/                    # Sprint 1 artifacts (unchanged)
│   ├── __init__.py
│   ├── depletion.py
│   ├── estimator.py
│   ├── hazard.py
│   └── state.py
└── universe/                 # ← Sprint 2 deliverables
    ├── __init__.py
    ├── build.py              # S2-5: Integration
    ├── fee_surface.py        # S2-2: Fee tracking
    ├── metadata.py           # S2-3: EnrichedMarket model
    ├── reward_surface.py     # S2-1: Reward eligibility
    └── scorer.py             # S2-4: Scoring logic
```

## Next Steps (Sprint 3)

1. **Integrate into main.py:**
   - Replace V1 universe building with `build_enriched_universe()`
   - Use `UniverseScorer.select_top()` instead of `select_universe()`
   - Update config to expose scorer coefficients

2. **Add event_id extraction:**
   - Query Gamma `/events` endpoint
   - Map markets to events for cluster risk management

3. **Backtest scoring formula:**
   - Compare V1 vs V2 universe selection on historical data
   - Tune coefficients (reward_bonus, fee_bonus, risk_penalty)

4. **Add monitoring:**
   - Log enriched market stats to database
   - Track reward eligibility changes over time
   - Alert on scoring anomalies

## Performance Notes

- **TTL Caching:** 5-minute cache on RewardSurface and FeeSurface reduces API calls
- **Single Gamma Request:** Fetches 200 markets in one call (max_pages=1)
- **No N+1 Queries:** All enrichment done in single pass over market list
- **Memory Footprint:** ~200 EnrichedMarket objects = ~50KB (negligible)

## Testing Output

```
Testing pmm2 universe layer...

✓ EnrichedMarket model works
✓ Ambiguity scoring: vague=0.40, specific=0.00
✓ RewardSurface dual indexing works
✓ FeeSurface works
✓ UniverseScorer works: good=6.35, bad=-12.03

✅ All tests passed!
```

## Commit

```
commit dd2bc9d
Author: Ubuntu <ubuntu@ip-172-31-43-167.tail194b69.ts.net>
Date:   Sat Mar 7 14:09:41 2026 +0000

    feat: Sprint 2 — Universe + Metadata Layer (pmm2/universe)
    
    Add complete universe selection v2 layer with:
    - RewardSurface: dual-indexed reward eligibility (condition_id + token_id)
    - FeeSurface: track fee-enabled markets with TTL cache
    - EnrichedMarket: unified metadata model for scoring
    - UniverseScorer: composite scoring with reward/fee bonuses and risk penalties
    - build_enriched_universe(): integration function
    
    Key improvements over v1:
    - Handles empty condition_id from Gamma via token_id fallback
    - Ambiguity scoring heuristic for resolution risk
    - Separate concerns: enrichment vs scoring vs selection
    - 5-minute TTL caching on reward/fee surfaces
    
    Tested with test_pmm2_universe.py — all passing.
    Next: integrate into main.py (Sprint 3)
```

---

**Sprint 2 Status: ✅ COMPLETE**

All deliverables built, tested, committed, and pushed to `main` branch.

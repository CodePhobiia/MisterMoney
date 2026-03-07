# Sprint 9: Shadow Mode — Implementation Summary

## ✅ Completed

Sprint 9 successfully implemented shadow mode for PMM-2, enabling it to run the full decision pipeline alongside V1 without executing any orders. This provides data-driven validation before live capital deployment.

## 📦 Components Built

### S9-1: Shadow Logger (`pmm2/shadow/logger.py`)
**Purpose:** Records every PMM-2 allocation cycle to JSONL files for analysis.

**Features:**
- Daily rolling log files: `data/shadow/shadow_YYYY-MM-DD.jsonl`
- Compact JSONL format (one JSON object per line)
- Logs complete allocation cycles with:
  - V1 current state (markets, orders, positions)
  - PMM-2 intended state (target quotes, mutations)
  - Comparison metrics (EV delta, market overlap, etc.)
- Logs specific divergences (market selection, pricing, scoring)
- Automatic log rotation (new file per day)

**Key Methods:**
```python
log_allocation_cycle(cycle_data: dict)  # Log a full cycle
log_divergence(divergence_type: str, details: dict)  # Log specific divergences
read_cycles(date: str | None) -> list[dict]  # Read cycles from log file
```

---

### S9-2: Counterfactual Engine (`pmm2/shadow/counterfactual.py`)
**Purpose:** Compares V1 actual decisions vs PMM-2 counterfactual recommendations.

**Metrics Tracked:**
1. **Market Selection Divergence** — which markets differ
2. **Pricing Divergence** — how prices differ
3. **Predicted Fill Improvement** — better queue positioning
4. **Reward Capture Improvement** — more reward-eligible markets
5. **Overall EV Delta** — expected value improvement

**Launch Readiness Gates (from spec Section 14):**
- ✅ Gate 1: Positive EV delta in ≥70% of cycles
- ✅ Gate 2: Better market selection (more reward-eligible)
- ✅ Gate 3: Lower churn (fewer cancels per live minute)
- ✅ Gate 4: At least 100 cycles of data

**Key Methods:**
```python
compare_cycle(v1_state: dict, pmm2_plan: dict) -> dict  # Compare a single cycle
get_summary() -> dict  # Rolling summary of performance
is_ready_for_live() -> bool  # Check if all gates passed
get_gates_status() -> dict[str, bool]  # Detailed gate status
```

---

### S9-3: Shadow Dashboard (`pmm2/shadow/dashboard.py`)
**Purpose:** Generates human-readable Telegram status reports.

**Features:**
- Telegram-formatted status messages with emoji indicators
- Daily shadow reports (every 24 hours)
- Milestone reports (every 100 cycles)
- Launch readiness status with blocking gates highlighted

**Example Output:**
```
🔮 PMM-2 Shadow Mode

📊 150 cycles analyzed
✅ 72% positive EV (gate: 70%)
📈 Avg EV delta: +$0.005/cycle
✅ Reward improvement: +3.2 markets
✅ Churn: -15% vs V1
🎯 Market overlap: 65.0%

Launch readiness: ✅ READY (4/4 gates passed)
```

**Key Methods:**
```python
generate_status() -> str  # Generate Telegram-friendly status
send_daily_shadow_report(chat_id: str)  # Send daily report
send_milestone_report(milestone: int)  # Send cycle milestone report
get_detailed_metrics() -> dict  # Programmatic metrics access
```

---

### S9-4: V1 State Snapshot (`pmm2/shadow/v1_snapshot.py`)
**Purpose:** Captures V1 bot's current trading state for counterfactual comparison.

**Captured State:**
- Timestamp (ISO format)
- Markets being quoted (condition IDs)
- Live orders (price, size, side, scoring status)
- Positions (size, cost basis, unrealized P&L)
- Scoring count
- Reward-eligible count
- Total capital deployed
- NAV (net asset value)

**Key Methods:**
```python
@staticmethod
capture(bot_state) -> dict  # Capture current V1 state
@staticmethod
summarize(snapshot: dict) -> str  # Human-readable summary
```

---

### S9-5: Runtime Integration (`pmm2/runtime/loops.py`)
**Modifications:** Wired shadow mode into the PMM2Runtime allocator loop.

**Integration Points:**
1. **Start of cycle:** Capture V1 state via `V1StateSnapshot.capture()`
2. **After PMM-2 planning:** Build PMM-2 plan summary
3. **Counterfactual comparison:** Run `counterfactual_engine.compare_cycle()`
4. **Full cycle logging:** Log to shadow logger with all details
5. **Milestone checks:** Every 100 cycles, send milestone report
6. **Daily reports:** Every 24 hours, send daily shadow report

**Shadow Mode Behavior:**
- When `config.shadow_mode = True`:
  - PMM-2 runs full pipeline (scoring, allocation, planning, diffing)
  - Mutations are collected but NOT executed
  - V1Bridge logs mutations instead of executing them
  - Full counterfactual comparison runs
  - Results logged to JSONL files

---

### S9-6: Configuration Update (`config/default.yaml`)
**Change:** Enabled shadow mode for validation.

```yaml
pmm2:
  enabled: true          # PMM-2 now runs (shadow mode only)
  shadow_mode: true      # True = log decisions, don't execute
  live_capital_pct: 0.0  # 0 = shadow, 0.1 = 10%, 1.0 = full
```

---

## 🧪 Testing

Created comprehensive test suite: `test_sprint9_shadow.py`

**Tests:**
1. ✅ ShadowLogger — logs cycles and divergences
2. ✅ CounterfactualEngine — compares V1 vs PMM-2
3. ✅ ShadowDashboard — generates status reports
4. ✅ V1StateSnapshot — captures bot state
5. ✅ Integration — full shadow mode imports

**Test Results:**
```bash
$ python3 test_sprint9_shadow.py
============================================================
Sprint 9 — Shadow Mode Tests
============================================================

✅ ShadowLogger test passed
✅ CounterfactualEngine test passed
✅ ShadowDashboard test passed
✅ V1StateSnapshot test passed
✅ Integration test passed

============================================================
✅ All Sprint 9 tests passed!
============================================================
```

---

## 📁 File Structure

```
MisterMoney/
├── pmm2/
│   ├── shadow/
│   │   ├── __init__.py           # Module exports
│   │   ├── logger.py             # ShadowLogger (JSONL logging)
│   │   ├── counterfactual.py     # CounterfactualEngine (comparison)
│   │   ├── dashboard.py          # ShadowDashboard (Telegram reports)
│   │   └── v1_snapshot.py        # V1StateSnapshot (state capture)
│   └── runtime/
│       └── loops.py              # ← Modified: shadow integration
├── config/
│   └── default.yaml              # ← Modified: enabled shadow mode
├── data/
│   └── shadow/                   # Shadow log files (created at runtime)
│       └── shadow_YYYY-MM-DD.jsonl
└── test_sprint9_shadow.py        # Test suite
```

---

## 🚀 Next Steps

1. **Restart the bot** — PMM-2 will begin shadow logging alongside V1
2. **Monitor logs** — Check `data/shadow/shadow_*.jsonl` for cycle logs
3. **Watch Telegram** — Daily reports at chat_id `7916400037`
4. **Wait for 100+ cycles** — Needed for launch readiness gates
5. **Review gates** — Check if PMM-2 meets all 4 launch criteria
6. **Go live** — When ready, set `shadow_mode: false` and `live_capital_pct: 0.1`

---

## 🔍 How It Works

### Allocation Cycle Flow (Shadow Mode)

1. **V1 runs normally** — places/cancels/amends orders as usual
2. **V1 state captured** — PMM-2 captures V1's current state
3. **PMM-2 runs full pipeline:**
   - Score all markets (MarketEVScorer)
   - Run capital allocator (GreedyAllocator)
   - Generate quote plans (QuotePlanner)
   - Diff target vs live (DiffEngine)
   - Collect mutations (NOT executed)
4. **Counterfactual comparison:**
   - Market selection divergence
   - EV delta
   - Reward capture improvement
   - Churn reduction
5. **Log full cycle:**
   - V1 state
   - PMM-2 plan
   - Comparison metrics
   - All details in JSONL
6. **Report milestones:**
   - Every 100 cycles → Telegram milestone report
   - Every 24 hours → Daily shadow report

---

## 📊 Example JSONL Entry

```json
{
  "allocator_output": {
    "funded_bundles": 3,
    "total_capital_used": 15.5
  },
  "comparison": {
    "churn_reduction": 0.2,
    "cycle_num": 42,
    "ev_delta": 0.008,
    "market_overlap_pct": 0.67,
    "pmm2_ev": 0.025,
    "pmm2_only_markets": ["0x789..."],
    "reward_improvement": 2,
    "v1_estimated_ev": 0.017,
    "v1_only_markets": ["0xabc..."]
  },
  "ev_breakdown": [
    {"condition_id": "0x123...", "ev_bps": 15},
    {"condition_id": "0x456...", "ev_bps": 10}
  ],
  "pmm2_markets": ["0x123...", "0x456...", "0x789..."],
  "pmm2_mutations": [
    {"action": "add", "condition_id": "0x789...", "price": 0.52, "side": "BUY", "size": 10.0}
  ],
  "pmm2_plan": {...},
  "timestamp": "2026-03-07T15:30:00.000Z",
  "v1_markets": ["0x123...", "0x456...", "0xabc..."],
  "v1_orders": [...],
  "v1_state": {...}
}
```

---

## 🎯 Success Criteria

Shadow mode is considered successful when:

✅ PMM-2 runs without errors alongside V1
✅ Shadow logs are generated correctly
✅ Counterfactual comparisons are accurate
✅ Telegram reports are delivered on schedule
✅ At least 100 cycles logged
✅ All 4 launch gates pass (70% positive EV, better selection, lower churn, enough data)

Once these criteria are met, PMM-2 is ready for live capital deployment (Sprint 10).

---

## 📝 Notes

- **Log rotation:** New JSONL file created daily at midnight UTC
- **Performance impact:** Minimal (shadow mode is read-only, no order execution)
- **Storage:** ~1-5 KB per cycle, ~10-50 MB per month
- **Telegram chat ID:** `7916400037` (hardcoded in dashboard)
- **Launch gates:** Can be tuned in `counterfactual.py` if needed

---

## 🔗 Git Commit

```
commit fa3b09d
Sprint 9: Shadow Mode — PMM-2 counterfactual logging

✅ S9-1: ShadowLogger — logs allocation cycles and divergences to JSONL
✅ S9-2: CounterfactualEngine — compares V1 vs PMM-2 decisions
✅ S9-3: ShadowDashboard — generates Telegram status reports
✅ S9-4: V1StateSnapshot — captures V1's current trading state
✅ S9-5: Wired shadow mode into PMM2Runtime allocator loop
✅ S9-6: Enabled shadow mode in config (pmm2.enabled=true, shadow_mode=true)
```

---

**Sprint 9 Status: ✅ COMPLETE**

All components built, tested, committed, and pushed to `origin/main`.
Ready for bot restart to begin shadow logging.

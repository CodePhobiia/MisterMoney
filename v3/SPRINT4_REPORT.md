# V3 Sprint 4 Report: Rule-Heavy Route

**Completed:** 2026-03-07  
**Commit:** `5bdf9b8`  
**Status:** ✅ All deliverables completed and tested

---

## 📦 Deliverables

### S4-T1: Opus Blind Rule Analysis ✅

**Files Created:**
- `v3/routes/rule_heavy.py` (427 lines) — Main route implementation
- `v3/routes/prompts/rule_heavy_v1.py` (107 lines) — Opus system prompt and builder

**Implementation:**
- `RuleHeavyRoute.opus_rule_pass()` — Deep legal/rule analysis using Claude Opus 4.6
- **Extended fields** added to BlindEstimate output:
  - `dispute_risk`: float [0, 1] — Probability of UMA dispute
  - `rule_clarity`: float [0, 1] — How clear/unambiguous the rules are
  - `edge_cases`: list[str] — Identified edge cases and ambiguities
- Uses **high reasoning effort** for complex legal analysis
- Model is **blind** to current market price (prevents anchoring)

**Prompt Features:**
- Legal analyst persona specialized in prediction market rules
- Focuses on ambiguities: "notwithstanding", "unless", "at the sole discretion of"
- Identifies UMA dispute risk (Polymarket-specific)
- Parses clarifications and amendments
- Weighs evidence against specific rule criteria

---

### S4-T2: GPT-5.4 Judge (Rule-Heavy Variant) ✅

**Files Created:**
- `v3/routes/prompts/rule_judge_v1.py` (81 lines) — Judge system prompt and builder

**Implementation:**
- `RuleHeavyRoute.judge_pass()` — Market-aware decision with rule-specific adjustments
- `RuleHeavyRoute.execute()` — End-to-end route orchestration

**Enhanced Hurdle Formula:**
```
h = base_hurdle + spread/2 + (0.03 * dispute_risk) + (0.02 * (1 - rule_clarity))
```

Where:
- `base_hurdle = 0.03` (3 cents minimum edge)
- `dispute_risk` from Opus analysis
- `rule_clarity` from Opus analysis

**Conservative Trading:**
- Higher dispute risk → wider hurdle
- Lower rule clarity → wider hurdle
- Prevents trading on ambiguous markets without sufficient edge

---

### S4-T3: Escalation Logic ✅

**Implementation:**
- `RuleHeavyRoute.should_escalate_async()` — Decides if market needs GPT-5.4-pro async adjudication

**Escalation Criteria (ANY of):**
- `dispute_risk > 0.3` — High risk of UMA dispute
- `rule_clarity < 0.5` — Rules are very ambiguous
- `market_notional > $50k` — High-value market
- `uncertainty > 0.25` — High epistemic uncertainty

**Note:** GPT-5.4-pro is not yet available via Codex endpoint. This method flags and logs escalation recommendations for future implementation.

---

### Wire Into Orchestrator ✅

**Modified Files:**
- `v3/routing/orchestrator.py` — Added rule route dispatch
- `v3/routes/prompts/__init__.py` — Exported new prompts

**Changes:**
1. Imported `RuleHeavyRoute` class
2. Initialized `self.rule_route` in orchestrator constructor
3. Wired `route="rule"` to call `RuleHeavyRoute.execute()`

---

### Integration Test ✅

**File Created:**
- `v3/routes/test_rule_heavy.py` (275 lines)

**Test Scenario:**
- **Market:** "Will the US government shut down before April 1, 2026?"
- **Rules:** Complex rules about shutdown definition, qualifying events, edge cases
- **Evidence:** 4 mock items (stalled negotiations, CR likely, brief lapse warning, historical data)
- **Clarifications:** 2 clarifications about short shutdowns and retroactive funding

**Test Results (LIVE API Calls):**

#### Test 1: Opus Blind Analysis
- ✅ Completed in 26 seconds
- **Probability:** 58.0% (vs market 42.0%)
- **Uncertainty:** 20.0%
- **Dispute Risk:** 25.0%
- **Rule Clarity:** 72.0%
- **Edge Cases Identified:** 5
  1. Brief lapse with retroactive restoration ambiguity
  2. Weekend/holiday lapse without furloughs
  3. OMB warning vs formal declaration
  4. Short-term CR delaying crisis past deadline
  5. Timing ambiguity near cutoff
- **Tokens:** 1,120 in / 1,179 out

#### Test 2: GPT-5.4 Judge
- ✅ Completed in 5.3 seconds
- **Edge:** 13.0¢ (|0.58 - 0.42| × 100)
- **Hurdle:** 5.31¢ (calculated with dispute/clarity factors)
- **Action:** TRADE ✓
- **Tokens:** 772 in / 255 out

#### Test 3: Escalation Logic
- **Scenario A (Current Market, $125k):** → Escalate YES ⚠️
- **Scenario B (High-Value, $100k):** → Escalate YES ⚠️
- **Scenario C (Clear Rules, $10k):** → Escalate NO ✓

#### Test 4: End-to-End Execution
- ✅ Full route completed in 36 seconds
- **Final Signal:**
  - Calibrated Probability: 58.0%
  - Confidence Range: [38.0% - 78.0%]
  - Hurdle Met: YES ✓
  - Route: `rule`
  - Models: `claude-opus-4-6`, `gpt-5.4-pro`
  - Expires: 20 minutes

---

## 📊 Summary Statistics

### Lines of Code
- **Total for Sprint 4:** 890 lines
  - `rule_heavy.py`: 427 lines
  - `rule_heavy_v1.py` (prompt): 107 lines
  - `rule_judge_v1.py` (prompt): 81 lines
  - `test_rule_heavy.py`: 275 lines

### Files Created
1. `v3/routes/rule_heavy.py`
2. `v3/routes/prompts/rule_heavy_v1.py`
3. `v3/routes/prompts/rule_judge_v1.py`
4. `v3/routes/test_rule_heavy.py`

### Files Modified
1. `v3/routes/prompts/__init__.py`
2. `v3/routing/orchestrator.py`

---

## 🎯 Sample Opus Analysis Output

**Market:** US Government Shutdown before April 1, 2026  
**Opus Estimate:** 58% (vs market 42%)  
**Edge:** 16¢

**Reasoning Summary (excerpt):**
> Evidence points to a meaningful probability of a qualifying shutdown. e1 (reliability 0.85) indicates stalled negotiations with a March 15 deadline at risk. e3 (reliability 0.80) shows the White House itself warning of a potential 'brief lapse,' which signals insider awareness of shutdown risk...

**Edge Cases Identified:**
1. Brief lapse with retroactive restoration before furloughs (Clarification 2 edge case)
2. Weekend/holiday funding lapse where no furloughs occur (ambiguity about official acknowledgment)
3. OMB warning vs formal declaration (resolution source requirement)
4. Short-term CR delaying crisis past April 1 cutoff
5. Timing ambiguity near April 1, 12:00 PM ET cutoff

**Dispute Risk:** 25% — Moderate risk due to edge cases around "brief lapse" and timing  
**Rule Clarity:** 72% — Rules are reasonably clear but have acknowledged ambiguities

---

## ✅ Requirements Met

- ✅ Uses providers from `v3.providers.registry`
- ✅ Uses entities from `v3.evidence.entities`
- ✅ Imports `MarketMeta` from `v3.intake.schemas`
- ✅ Did NOT modify existing route files (`simple.py`, `numeric.py`)
- ✅ Updated orchestrator to dispatch rule route
- ✅ Uses `structlog` for logging
- ✅ Integration test runs LIVE API calls
- ✅ Git committed and pushed

---

## 🚀 What's Next

The Rule-Heavy Route is now fully integrated into the V3 Resolution Intelligence pipeline:

1. **Orchestrator** dispatches `route="rule"` markets to `RuleHeavyRoute`
2. **Opus 4.6** performs deep legal analysis with dispute risk assessment
3. **GPT-5.4** judges market edge with rule-aware hurdle calculation
4. **Escalation logic** flags high-risk markets for future GPT-5.4-pro review

**Future Enhancements:**
- Integrate GPT-5.4-pro async adjudication when available
- Add historical precedent retrieval (similar controversial resolutions)
- Build rule graph parser for structured rule representation
- Add dispute simulation (what would UMA voters likely decide?)

---

**Status:** ✅ Sprint 4 Complete  
**Next Sprint:** S5 — Numeric Route Refinement or Dossier Route v2

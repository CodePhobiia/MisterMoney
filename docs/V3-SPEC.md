# MisterMoney V3 — Resolution Intelligence Layer

*Spec v2.0 — Rewritten 2026-03-07 incorporating quant review*
*Original concept: Butters. Production architecture: Theyab's quant analyst. Final spec: Butters.*

---

## 0. Design Principles

1. **AI is a signal layer, not an execution layer.** V3 adjusts fair value and skew. It never overrides V1 risk limits, V2 allocation, or hard safety rails.
2. **Deterministic first, AI second.** If you can check the resolution source with an API call, don't burn $0.03 on an LLM to guess.
3. **Route, don't broadcast.** Different market types need different model combinations. No fixed committee runs on every market.
4. **Blind before market-aware.** Models produce probability estimates without seeing the current market price. Anchoring is the enemy.
5. **Evidence, not prose.** Every model returns structured schemas with cited evidence, falsifiers, and uncertainty. If it can't cite, it says "no edge."
6. **Escalate only when EV justifies cost.** Most markets don't deserve frontier-model spend most of the time.
7. **Calibrate on outcomes, not vibes.** The final probability comes from a learned stacker trained on resolved markets, not from a weighted median.
8. **LLMs complement quant models, they don't replace them.** Barrier probabilities, hazard models, and calibration are math. Rule parsing, evidence synthesis, and dispute detection are LLM territory.

---

## 1. Architecture Overview

```
                    ┌─────────────────────────────────┐
                    │         TIER 0: DETERMINISTIC    │
                    │                                  │
                    │  • Parse market metadata         │
                    │  • Fetch rule text/clarifications │
                    │  • Check resolution sources       │
                    │  • Microstructure features        │
                    │  • Change detection               │
                    │  • Market-type classification      │
                    └──────────────┬──────────────────┘
                                   │
                          anything changed?
                                   │
                         ┌─────────▼──────────┐
                         │  TIER 1: TRIAGE     │
                         │  Sonnet 4.6         │
                         │                     │
                         │  • News scan        │
                         │  • Entity extraction │
                         │  • Rule→schema      │
                         │  • Escalate? Y/N    │
                         └────────┬────────────┘
                                  │
                        EV > Cost + buffer?
                                  │
              ┌───────────────────┼───────────────────┐
              │                   │                    │
    ┌─────────▼────────┐ ┌───────▼────────┐ ┌────────▼────────┐
    │  TYPE 1: NUMERIC  │ │ TYPE 2: RULES  │ │ TYPE 3: DOSSIER │
    │                   │ │                │ │                  │
    │ Deterministic     │ │ Opus 4.6       │ │ Gemini 3.1 Pro  │
    │ barrier/hazard    │ │ rule lawyer    │ │ long-context     │
    │ + AI for regime   │ │ + GPT-5.4     │ │ + Opus challenger│
    │   shifts only     │ │   judge        │ │ + GPT-5.4 judge │
    └─────────┬─────────┘ └───────┬────────┘ └────────┬────────┘
              │                   │                    │
              └───────────────────┼────────────────────┘
                                  │
                    ┌─────────────▼──────────────┐
                    │   TIER 3: CALIBRATION       │
                    │                             │
                    │  Learned logit-space stacker │
                    │  + uncertainty estimation    │
                    │  + dynamic hurdle gate       │
                    │  + edge-after-uncertainty    │
                    │    skew formula              │
                    └─────────────┬───────────────┘
                                  │
                                  ▼
                        V2 Scorer (capped skew)
                                  ▼
                        V1 Execution (quotes)
```

---

## 2. Tier 0 — Deterministic Layer

Runs on **every market, every cycle** (no API cost). Pure Python/SQL.

### 2.1 Market Metadata Parser

```python
class MarketMetadata(BaseModel):
    condition_id: str
    question: str
    description: str
    resolution_source: str
    resolution_rules: str
    clarifications: list[str]
    end_date: datetime
    market_type: MarketType          # NUMERIC, RULE_HEAVY, DOCUMENT_RICH
    resolution_source_type: SourceType  # API_CHECKABLE, HUMAN_JUDGED, ORACLE_BASED
    tags: list[str]                  # politics, crypto, sports, science, etc.
```

### 2.2 Resolution Source Checker

For machine-readable markets, **check the actual source**:

```python
class SourceCheck(BaseModel):
    source_name: str           # "CoinGecko BTC/USD", "ESPN NBA scores"
    current_value: float | str
    threshold: float | str
    operator: str              # ">=", "<=", "==", "contains"
    distance_to_threshold: float
    last_checked: datetime
    checkable: bool            # False if source is human judgment
```

If BTC is at $99,500 and the threshold is $100,000, that's a deterministic signal worth more than any LLM opinion.

### 2.3 Change Detection

```python
class ChangeEvent(BaseModel):
    event_type: str   # "clarification_added", "source_crossed_band",
                      # "mid_stale_but_world_changed", "rule_updated",
                      # "new_evidence", "approaching_resolution"
    severity: float   # 0-1
    details: str
    timestamp: datetime
```

**Trigger condition for Tier 1:**
```
trigger_t = 𝟙[new_evidence ∨ clarification_changed ∨ source_crossed_band
             ∨ (|Δmid| < ε ∧ world_changed) ∨ high_EV_candidate]
```

If `trigger_t = 0`, Tier 0 emits the last cached signal. No API calls.

### 2.4 Market-Type Classification

```python
class MarketType(str, Enum):
    NUMERIC = "numeric"           # BTC>$100K, team scores, CPI prints
    RULE_HEAVY = "rule_heavy"     # policy, legal, approval, wording-sensitive
    DOCUMENT_RICH = "document_rich"  # court rulings, regulatory, multi-doc
    SIMPLE_BINARY = "simple_binary"  # straightforward yes/no, clear resolution
```

Classification rules (deterministic, no LLM needed):
- Has `resolution_source` matching a known API → NUMERIC
- Rule text > 500 words or > 2 clarifications → RULE_HEAVY or DOCUMENT_RICH
- Multiple related documents or PDFs referenced → DOCUMENT_RICH
- Short rules, clear source, no clarifications → SIMPLE_BINARY

### 2.5 Microstructure Features

Computed from V1/V2 data (no API cost):
```python
class MicrostructureFeatures(BaseModel):
    mid: float
    spread_cents: float
    depth_at_best: float
    volume_24h: float
    toxicity_score: float          # from V2 calibration
    our_inventory: float
    time_to_resolution_hours: float
    mid_velocity_1h: float         # how fast mid is moving
    volume_spike: bool             # volume > 3x 7-day average
```

---

## 3. Tier 1 — Triage (Sonnet 4.6)

**Model**: Claude Sonnet 4.6 with adaptive thinking
**When**: Only when Tier 0 fires a trigger
**Cost**: ~$0.001-0.005 per call (fast, cheap, high-volume)

### 3.1 Purpose

Sonnet answers ONE question: **"Does this market need specialist escalation right now?"**

It also extracts structured data for downstream use.

### 3.2 Input (blind — no market price)

```json
{
  "question": "Will Bitcoin reach $100,000 by June 30, 2026?",
  "rules": "This market resolves YES if...",
  "clarifications": ["..."],
  "change_events": [{"type": "source_crossed_band", "details": "BTC at $99,500"}],
  "source_check": {"current_value": 99500, "threshold": 100000, "distance": 500},
  "market_type": "NUMERIC",
  "time_to_resolution_hours": 2784
}
```

**Note: `current_mid` is NOT passed.** Blind by design.

### 3.3 Output Schema

```json
{
  "needs_escalation": true,
  "escalation_reason": "Source within 0.5% of threshold, high-EV regime shift",
  "market_type_override": null,
  "extracted_entities": {
    "resolution_source": "CoinGecko BTC/USD spot",
    "threshold": 100000,
    "window_end": "2026-06-30T23:59:59Z",
    "key_conditions": ["BTC/USD >= $100,000 at any point before deadline"]
  },
  "rule_schema": {
    "source": "CoinGecko BTC/USD",
    "operator": ">=",
    "threshold": 100000,
    "window_start": "2026-01-01T00:00:00Z",
    "window_end": "2026-06-30T23:59:59Z",
    "edge_cases": ["Does not specify which CoinGecko pair"]
  },
  "news_summary": "BTC crossed $99K for first time, $500 from target",
  "triage_score": 0.85,
  "estimated_specialist_value": 0.04
}
```

### 3.4 Escalation Gate

```
escalate = needs_escalation ∧ (EV_possible > Cost_API + Cost_latency + buffer)
```

Where:
- `EV_possible = estimated_specialist_value × our_capital_in_market × time_horizon`
- `Cost_API` = estimated token cost for the specialist path
- `Cost_latency` = opportunity cost of waiting for response
- `buffer` = configurable margin (default: $0.02)

Most markets most of the time: **no escalation, use cached signal.**

---

## 4. Tier 2 — Specialist Escalation

Called conditionally based on market type and triage output.

### 4.1 Type 1: Numeric Markets — Deterministic + Conditional AI

**Default**: No LLM. Use quantitative models:

```python
class NumericSignal(BaseModel):
    """Barrier/survival probability for numeric threshold markets."""
    p_barrier: float              # P(source crosses threshold before deadline)
    p_barrier_low: float
    p_barrier_high: float
    model_type: str               # "gbm_barrier", "hazard_rate", "empirical"
    current_distance_pct: float   # distance to threshold as % of current value
    implied_vol: float            # if available from options/historical
    time_remaining_fraction: float
```

For a BTC-at-$100K market with BTC at $94.5K:
- Distance: 5.8%
- Time: 4 months
- Historical BTC vol: ~60% annualized
- Barrier probability via GBM: ~72%

**AI escalation only when**:
- Regime shift detected (news of regulatory ban, ETF approval, etc.)
- Source behavior is anomalous (exchange outage, data feed divergence)
- Rule ambiguity discovered by Tier 0 or Tier 1

When escalated: Sonnet interprets the event → if material, GPT-5.4 judges impact on barrier probability.

### 4.2 Type 2: Rule-Heavy Markets — Opus Rule Lawyer + GPT Judge

**Pass A — Blind Rule Analysis (Opus 4.6, adaptive thinking)**

Input:
```json
{
  "rules": "Full resolution rules text...",
  "clarifications": ["..."],
  "evidence": [{"source": "Reuters", "claim": "...", "timestamp": "..."}],
  "extracted_entities": { /* from Sonnet triage */ }
}
```

**No market price. No mid. No spread. Blind.**

Output:
```json
{
  "p_blind": 0.67,
  "p_low": 0.58,
  "p_high": 0.75,
  "rule_clarity": 0.85,
  "dispute_risk": 0.05,
  "resolution_complexity": "conditional",
  "key_criteria": [
    "Senate must pass bill with majority vote",
    "President must sign within 10 business days"
  ],
  "ambiguities": [
    "Rules say 'pass' but don't specify whether committee passage counts"
  ],
  "evidence": [
    {
      "id": "ev_001",
      "source_type": "reuters",
      "timestamp": "2026-03-07T12:00:00Z",
      "claim": "Senate committee voted 12-8 to advance bill",
      "direction": "YES",
      "strength": 0.6,
      "reliability": 0.95
    }
  ],
  "falsifiers": [
    "Presidential veto threat from March 5 press conference",
    "Filibuster possible — 60-vote threshold unclear in rules"
  ],
  "uncertainty_reasons": [
    "Committee passage vs. floor vote ambiguity",
    "No historical precedent for this bill type"
  ],
  "edge_or_no_edge": "edge",
  "reasoning_trace": "..."
}
```

**Pass B — Market-Aware Decision (GPT-5.4)**

Now show the model what it's trading against:
```json
{
  "p_blind": 0.67,
  "p_interval": [0.58, 0.75],
  "evidence_summary": [/* from Opus */],
  "current_mid": 0.62,
  "spread_cents": 1.0,
  "depth_at_best": 5000,
  "our_inventory": 15.0,
  "toxicity_score": 0.12,
  "time_to_resolution_hours": 720
}
```

Output:
```json
{
  "action": "BUY",
  "confidence": 0.6,
  "max_skew_cents": 2.0,
  "why_market_is_stale": "Committee vote result from 6 hours ago not priced in. Mid should be 65-67¢, currently 62¢.",
  "why_market_is_NOT_stale": null,
  "edge_after_costs": 0.03
}
```

**Escalate to GPT-5.4-pro only when**:
- Model disagreement > 15%
- EV of the market justifies the cost
- Rule ambiguity is high AND our capital exposure is significant

### 4.3 Type 3: Document-Rich Markets — Gemini Dossier + Opus Challenger

**Pass A — Dossier Synthesis (Gemini 3.1 Pro, 1M-token context)**

Input: Full document bundle — PDFs, articles, ruling texts, regulatory filings. Gemini's 1M-token window handles what other models can't.

Output: Same evidence graph schema as Opus, plus:
```json
{
  "document_count": 12,
  "total_tokens_processed": 245000,
  "cross_document_contradictions": [
    "Document A says 'by end of Q2', Document B says 'by June 30 EOD' — ambiguous if these are the same deadline"
  ],
  "key_document": "Federal Register Vol. 91 No. 45, pages 12001-12015"
}
```

**Pass B — Adversarial Challenge (Opus 4.6)**

Opus receives Gemini's synthesis and tries to break it:
```json
{
  "gemini_assessment": { /* full output */ },
  "task": "Find errors, missing evidence, or overconfident claims in this assessment. If the assessment is sound, say so."
}
```

**Pass C — Final Judgment (GPT-5.4)**

Same market-aware pass as Type 2.

### 4.4 Simple Binary Markets — Sonnet Only

For straightforward markets with clear rules and obvious resolution:
- Sonnet triage output IS the specialist output
- No escalation unless change detected
- Cheapest path

---

## 5. Tier 3 — Calibration Layer

### 5.1 Calibrated Stacker (Logit-Space)

Replace the weighted median with a learned linear model in logit space:

```
ℓ_t = α + β₀·logit(m_t) + β₁·logit(p_opus) + β₂·logit(p_gemini) + β₃·logit(p_gpt)
       + β₄·s_sonnet + β₅·c_rule - β₆·r_dispute - β₇·d_disagree + β₈·f_freshness

p̂_t = σ(ℓ_t)
```

Where:
| Variable | Description |
|----------|-------------|
| `m_t` | Current market prior (mid) |
| `p_opus` | Opus blind probability estimate |
| `p_gemini` | Gemini blind probability estimate |
| `p_gpt` | GPT blind probability estimate |
| `s_sonnet` | Sonnet triage score |
| `c_rule` | Rule clarity (0-1) |
| `r_dispute` | Dispute risk (0-1) |
| `d_disagree` | Cross-model dispersion: `std([p_opus, p_gemini, p_gpt])` |
| `f_freshness` | Evidence freshness (recency-weighted) |

**When not all models ran** (most common case): missing logits default to `logit(m_t)` (market prior), effectively zeroing their contribution.

### 5.2 Calibration Training

**Training data**: Every resolved market where we logged model predictions.

```python
# For each resolved market:
X = [logit(m_t), logit(p_opus), logit(p_gemini), logit(p_gpt),
     s_sonnet, c_rule, r_dispute, d_disagree, f_freshness]
y = 1 if resolved_YES else 0

# Fit logistic regression (with regularization)
from sklearn.linear_model import LogisticRegression
calibrator = LogisticRegression(C=1.0, max_iter=1000)
calibrator.fit(X_train, y_train)

# Post-hoc: isotonic regression for residual miscalibration
from sklearn.isotonic import IsotonicRegression
iso = IsotonicRegression(out_of_bounds='clip')
iso.fit(calibrator.predict_proba(X_val)[:, 1], y_val)
```

**Cold start**: Until we have 50+ resolved markets with model predictions, use `β₀ = 1.0` (trust market) and all other β = 0 (ignore models). Gradually increase model weights as data accumulates.

### 5.3 Uncertainty Estimation

```
u_t = a₀ + a₁·model_dispersion + a₂·rule_ambiguity + a₃·source_age + a₄·missing_evidence
```

| Component | Weight | Description |
|-----------|--------|-------------|
| `a₀` | 0.05 | Base uncertainty floor |
| `model_dispersion` | `std([p_opus, p_gemini, p_gpt])` | Models disagree |
| `rule_ambiguity` | `1 - c_rule` | Rules are unclear |
| `source_age` | `hours_since_last_source_check / 24` | Data is stale |
| `missing_evidence` | count of `uncertainty_reasons` / 5 | Evidence gaps |

### 5.4 Dynamic Hurdle Gate

**Do not trade on model signal unless edge exceeds hurdle:**

```
|p̂_t - m_t| > h_t
```

Where:
```
h_t = h₀ + h_model + h_resolution + h_tox
```

| Component | Default | Description |
|-----------|---------|-------------|
| `h₀` | 0.03 (3¢) | Base hurdle — minimum edge to act |
| `h_model` | `0.02 × d_disagree / 0.1` | Higher when models disagree |
| `h_resolution` | `0.02 × (1 - c_rule)` | Higher when rules are ambiguous |
| `h_tox` | `0.01 × toxicity_score` | Higher in toxic markets |

If hurdle not met → `skew_t = 0` → V3 emits no signal → V2 uses market mid as fair value.

### 5.5 Edge-After-Uncertainty Skew Formula

When hurdle is met:

```
skew_t = clip(k · (p̂_t - m_t) · (1 - u_t) · liq_t · tox_t,  -s_max, s_max)
```

Where:
- `k` = aggressiveness parameter (default: 1.0)
- `liq_t` = liquidity multiplier: `min(1.0, depth_at_best / 5000)`
- `tox_t` = toxicity discount: `max(0.2, 1.0 - toxicity_score)`
- `s_max` = maximum skew (default: 5¢, configurable)

### 5.6 Output: FairValueSignal

```python
class FairValueSignal(BaseModel):
    condition_id: str
    p_calibrated: float           # calibrated probability
    p_interval: tuple[float, float]  # confidence interval
    uncertainty: float            # u_t
    edge_vs_market: float         # p̂_t - m_t
    hurdle: float                 # h_t
    hurdle_met: bool              # |edge| > hurdle
    skew_cents: float             # skew_t × 100
    recommended_action: str       # BUY, SELL, NEUTRAL
    evidence_count: int
    model_agreement: float        # 1 - d_disagree
    rule_clarity: float
    dispute_risk: float
    freshness: float
    specialist_path: str          # "numeric", "rule_heavy", "dossier", "simple"
    models_called: list[str]      # ["sonnet_4.6"] or ["sonnet_4.6", "opus_4.6", "gpt_5.4"]
    total_requests: int           # API calls consumed for this signal
    rate_budget_remaining: dict[str, int]  # provider → remaining quota
    timestamp: datetime
```

---

## 6. Model Assignments & Access

### 6.0 Access Strategy — OAuth Consumer Endpoints ($0 Marginal Cost)

**All model access routes through existing subscription OAuth, NOT paid API keys.**

We have three active subscriptions that provide unlimited (rate-limited) access:

| Subscription | Models Available | OAuth Endpoint | Auth Method |
|-------------|-----------------|----------------|-------------|
| **Anthropic Pro** | Opus 4.6, Sonnet 4.6 | Claude consumer API | OAuth device flow → access token |
| **Google AI Ultra** | Gemini 3.1 Pro, Flash-Lite | Cloud Code Assist (`cloudcode-pa.googleapis.com`) | Google OAuth → bearer token |
| **ChatGPT Pro ($200/mo)** | GPT-5.4, GPT-5.4-pro | Codex API (`chatgpt.com/backend-api/codex/responses`) | OpenAI OAuth device flow → access token |

**Proven infrastructure**: The Chimera semantic proxy already runs all three backends via OAuth with automatic token refresh. V3 reuses the same auth layer.

**Token management**:
```python
class OAuthTokenManager:
    """Manages OAuth tokens for all three providers.
    
    Reads from auth-profiles.json, auto-refreshes expired tokens,
    handles rate limits with exponential backoff.
    """
    providers: dict[str, OAuthProfile]  # anthropic, google, openai
    
    async def get_token(self, provider: str) -> str:
        """Returns valid bearer token, refreshing if expired."""
    
    async def call_model(self, provider: str, model: str, messages: list, **kwargs) -> dict:
        """Unified interface across all three providers."""
```

**Rate limits** (the real constraint, not cost):
- Anthropic Pro: ~45 messages/5 min for Opus, higher for Sonnet
- Google AI Ultra / CCA: ~10 req/min (observed 403 after burst — needs backoff)
- ChatGPT Pro / Codex: SSE streaming, rate limit TBD (~60 req/min observed)

**Implication for architecture**: The escalation gate (`EV > Cost`) becomes a **rate-limit gate** (`remaining_quota > 0 ∧ priority > threshold`). Since marginal token cost = $0, we can be more aggressive with escalation — the binding constraint is requests/minute, not dollars/day.

### 6.1 Sonnet 4.6 — Hot-Path Triage & Extraction

**Role**: First-pass classifier. Runs on every triggered market.
**Access**: Anthropic Pro OAuth
**Strengths**: Fast, cheap, web search/fetch, adaptive thinking.
**Used for**:
- News triage and entity extraction
- Rule-to-schema extraction
- "Does this market need escalation?" classification
- Simple binary market probability (when no escalation needed)

**Thinking mode**: Adaptive (let the model decide thinking depth).
**Rate budget**: Primary consumer of Anthropic quota. ~70% of Anthropic calls.

### 6.2 Opus 4.6 — Rule Lawyer & Adversarial Challenger

**Role**: Deep rule analysis. Called conditionally.
**Access**: Anthropic Pro OAuth
**Strengths**: Highest reasoning, adversarial analysis, nuance detection.
**Used for**:
- Rule ambiguity analysis (Type 2 markets)
- Adversarial challenge of Gemini dossiers (Type 3 markets)
- Dispute risk assessment near resolution
- Edge case detection when another model found large edge

**Thinking mode**: Adaptive (extended thinking when rules are complex).
**When NOT to call**: Clear rules, no clarifications, simple binary, no change detected.
**Rate budget**: ~30% of Anthropic calls. Reserve for high-value decisions.

### 6.3 Gemini 3.1 Pro — Long-Context Dossier Model

**Model**: `gemini-3.1-pro-preview` (NOT gemini-3-pro-preview — deprecated March 9, 2026)
**Access**: Google AI Ultra → Cloud Code Assist OAuth (`cloudcode-pa.googleapis.com/v1internal:generateContent`)
**Role**: Document synthesis. Called for Type 3 markets.
**Strengths**: 1M-token context, structured outputs, search grounding, function calling.
**Used for**:
- Multi-document evidence synthesis
- PDF/regulatory filing analysis
- Large clarification bundles
- Cross-document contradiction detection

**Alternative for high-volume extraction**: Gemini 3.1 Flash-Lite via same OAuth (cheaper quota usage). Reserve Pro for hard long-context cases.
**Rate budget**: ~10 req/min observed limit. Use for dossier markets only.

### 6.4 GPT-5.4 / GPT-5.4-pro — Orchestrator & Final Judge

**Role**: Market-aware decision maker. Final call on action.
**Access**: ChatGPT Pro OAuth → Codex API (`chatgpt.com/backend-api/codex/responses`, SSE streaming)
**Strengths**: 1M-token context, web search, file search, tool use.
**Used for**:
- Pass B (market-aware decision) in all escalated paths
- Default judge when only one specialist ran
- Orchestration when multiple specialists disagree

**Escalate to GPT-5.4-pro when**:
- Model disagreement > 15%
- Market is capital-intensive (>$50 our exposure)
- Ambiguity is high enough that a bad read costs real money

**Rate budget**: Primary consumer of OpenAI quota. SSE streaming required (`stream: true`).

### 6.5 Provider Failover

If one provider hits rate limits or is down:
```
Sonnet (triage)  → fallback: Gemini Flash-Lite → GPT-5.4
Opus (rules)     → fallback: GPT-5.4-pro → Gemini Pro
Gemini (dossier) → fallback: GPT-5.4 (1M context) → Opus (slower but capable)
GPT-5.4 (judge)  → fallback: Opus → Sonnet (degraded)
```

---

## 7. Integration with V2

V3 feeds into V2's scorer via `FairValueSignal`:

```python
# In pmm2/scorer/combined.py
async def score_bundle(self, market, bundle, reservation_price, nav):
    # ... existing EV components ...

    # V3 fair value adjustment
    v3_signal = self.v3_engine.get_signal(market.condition_id)

    if v3_signal and v3_signal.hurdle_met:
        # Apply capped skew to reservation price
        skew = v3_signal.skew_cents / 100.0
        adjusted_r = reservation_price + skew

        # V3 NEVER overrides:
        # - V1 risk limits
        # - V2 allocation caps
        # - Circuit breakers
        # - Position limits
```

---

## 8. Data Pipeline

### 8.1 Free / Built-in Sources

| Source | What | Cadence |
|--------|------|---------|
| Polymarket API | Market metadata, rules, clarifications | Every cycle |
| Brave Search API | Breaking news headlines | On trigger |
| Web fetch | Resolution source values (CoinGecko, ESPN, etc.) | 1-5 min for active |
| Wikipedia | Background context | On first encounter |
| V1/V2 telemetry | Microstructure, toxicity, fills | Real-time |
| SQLite (PMM-2) | Historical fills, markout data | On demand |

### 8.2 Paid Sources (when justified by AUM)

| Source | Cost | Value |
|--------|------|-------|
| NewsAPI.org | $449/mo | Real-time news from 150K+ sources |
| Polygon.io | $99/mo | Real-time crypto/stock prices |
| AP API | Variable | Authoritative political/event news |

### 8.3 Resolution Source Registry

Maintain a mapping of known resolution sources to API endpoints:

```python
SOURCE_REGISTRY = {
    "coingecko_btc_usd": {
        "url": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
        "parser": lambda r: r["bitcoin"]["usd"],
        "type": "numeric",
        "refresh_seconds": 60,
    },
    "espn_nba_scores": {
        "url": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
        "parser": parse_espn_scores,
        "type": "numeric",
        "refresh_seconds": 30,
    },
    # ... extensible
}
```

This is the highest-alpha component of V3. When you can check the resolution source programmatically, you know the answer before the market does.

---

## 9. Cost Model

### 9.1 OAuth = $0 Marginal Token Cost

All three providers are accessed via existing subscription OAuth:

| Subscription | Monthly Cost | What We Get | Marginal Token Cost |
|-------------|-------------|-------------|-------------------|
| Anthropic Pro | ~$20/mo | Opus 4.6, Sonnet 4.6, web search | **$0** |
| Google AI Ultra | ~$20/mo | Gemini 3.1 Pro, Flash-Lite, search grounding | **$0** |
| ChatGPT Pro | $200/mo | GPT-5.4, GPT-5.4-pro, web/file search | **$0** |
| **Total fixed** | **~$240/mo** | All four frontier models | **$0/token** |

This is already paid regardless of V3. The only NEW costs V3 introduces:
- **Third-party data APIs** (NewsAPI, Polygon.io) — optional, only when AUM justifies
- **Compute** (Python process running source checkers) — negligible on existing server
- **Rate limit opportunity cost** — using Opus quota for V3 means less for other tasks

### 9.2 The Real Constraint: Rate Limits, Not Dollars

Since tokens are free, the optimization problem changes:

**Old (paid API)**: `escalate when EV_possible > Cost_API + Cost_latency + buffer`
**New (OAuth)**: `escalate when priority > other_uses_of_quota ∧ rate_budget_remaining > 0`

Rate limit budget allocation (per 5-minute window):

| Provider | Total Quota (est.) | V3 Allocation | Other Uses |
|----------|-------------------|---------------|------------|
| Anthropic | ~45 Opus + ~100 Sonnet | 30 Sonnet + 10 Opus | 15 for Butters/other |
| Google CCA | ~50 req | 30 req | 20 for other |
| OpenAI Codex | ~300 req (SSE) | 50 req | 250 for other |

### 9.3 Rate-Aware Escalation Gate

Replace the cost gate with a rate-aware priority queue:

```python
class RateBudget:
    """Manages rate limit allocation across V3 and other consumers."""
    
    def can_escalate(self, provider: str, priority: float) -> bool:
        """Allow escalation if rate budget permits and priority warrants it."""
        remaining = self.remaining_quota(provider)
        if remaining <= self.reserve_for_other:
            return False
        if priority < self.min_escalation_priority:
            return False
        return True
    
    def priority_score(self, market: MarketMetadata, triage: TriageResult) -> float:
        """Higher priority = more likely to escalate."""
        return (
            triage.estimated_specialist_value * our_capital_in_market
            + urgency_bonus_if_near_resolution
            + stale_market_bonus
        )
```

### 9.4 Cost Simulation Still Needed

Even though tokens are free, we should still measure:
- Actual requests per market type per day (to verify rate limits aren't hit)
- Latency per provider per call type (affects quote staleness)
- Token consumption trends (subscriptions may change terms)
- Whether the $200/mo ChatGPT Pro is justified by V3 alone vs. shared across all uses

### 9.5 Break-Even Analysis

V3 doesn't need to cover API costs (already paid). It needs to generate **incremental alpha** above V1+V2 alone:
- If V3 improves Brier score by 0.01 across 12 markets → estimated +$2-5/day at current NAV
- If V3 catches one stale market per day that V2 missed → estimated +$1-3 per event
- At $104 NAV, even $3/day incremental = ~1,000% annualized improvement on V3 dev cost (labor only)

The real ROI calculation is at scale: at $10K+ NAV with 30+ markets, V3 edge compounds.

---

## 10. Safety Rails

1. **Max skew = 5¢**: V3 cannot move quotes more than 5 cents from market mid
2. **Dynamic hurdle gate**: Must clear `h_t` before any skew applied
3. **Uncertainty dampening**: High uncertainty → skew multiplied by `(1 - u_t)` → approaches zero
4. **Dispute gate**: If `dispute_risk > 0.2` → action = NEUTRAL, skew = 0
5. **Model agreement gate**: If `d_disagree > 0.15` → halve max skew
6. **Stale model detection**: Signal confidence decays 10%/hour when models haven't refreshed
7. **Cold start protection**: Until 50+ calibration samples, all β except β₀ (market prior) = 0
8. **Human overrides**: `/v3 pause`, `/v3 override <market> <probability>`, `/v3 cost-limit <$/day>`
9. **Shadow mode**: V3 runs 10+ days in shadow before any live skew. Log everything, trade nothing.
10. **Evidence requirement**: If specialist returns `edge_or_no_edge: "no_edge"`, signal is suppressed regardless of calibrator output

---

## 11. Research Path

### 11.1 Model Evaluation

Before production routing:

1. Run all four models on 200+ resolved Polymarket markets
2. Score each on:
   - Rule parsing accuracy (did it identify the correct resolution criteria?)
   - Stale-market detection (did it catch when mid should have moved?)
   - Probability calibration (Brier score on resolved outcomes)
   - Realized post-trade edge (if we had traded the signal, what was PnL?)
3. Use OpenAI Evals with external models/custom endpoints for cross-vendor comparison
4. Pick routing thresholds from data, not taste

### 11.2 Calibration Feedback Loop

```
Market resolves → record (features, prediction, outcome) → retrain stacker weekly
                                                         → update routing thresholds monthly
                                                         → publish calibration report daily
```

### 11.3 A/B Testing

Run two V3 configurations simultaneously in shadow:
- Configuration A: current routing + weights
- Configuration B: experimental changes
- Compare Brier scores, edge capture, cost efficiency

---

## 12. Build Plan

| Sprint | What | Days | Dependencies |
|--------|------|------|--------------|
| V3-S0 | Source registry + deterministic checker | 2 | None |
| V3-S1 | Change detection + trigger logic | 2 | S0 |
| V3-S2 | Market-type classifier | 1 | S0 |
| V3-S3 | Sonnet triage (Tier 1) + escalation gate | 3 | S1, S2 |
| V3-S4 | Opus rule lawyer (Type 2 specialist) | 2 | S3 |
| V3-S5 | Gemini dossier model (Type 3 specialist) | 2 | S3 |
| V3-S6 | GPT-5.4 judge (Pass B, market-aware) | 2 | S4, S5 |
| V3-S7 | Calibrated stacker + uncertainty + hurdle | 3 | S6 |
| V3-S8 | V2 integration (FairValueSignal → scorer) | 1 | S7 |
| V3-S9 | Shadow mode + logging + cost tracking | 2 | S8 |
| V3-S10 | Model evaluation on historical markets | 5 | S9 |
| V3-S11 | Calibration training (50+ resolved markets) | ongoing | S10 |
| V3-S12 | Live with 1¢ max skew | 1 week calendar | S11 |
| V3-S13 | Full production (5¢ max skew) | ongoing | S12 |

**Dev time: ~25 days. Calendar: ~6 weeks to shadow, ~3 months to full production.**
(Longer than v1 spec — but it ships a system that actually works.)

---

## 13. What V3 Does NOT Do

- **Does not replace V1 or V2.** It's a signal layer.
- **Does not override risk limits.** Ever.
- **Does not use LLMs for barrier probability on numeric markets.** That's quant math.
- **Does not assume LLMs are always right.** The calibrator can learn to ignore unreliable models.
- **Does not run frontier models when a $0 deterministic check suffices.**
- **Does not pass market price to blind estimation passes.** Anti-anchoring by design.
- **Does not ship a weighted median.** The calibrated stacker is the production aggregator.

---

*V1 = hands. V2 = brain. V3 = eyes, ears, and judgment.*

*The right system is not "AI instead of quant." It's deterministic tools + cheap triage + conditional specialists + learned calibration. The LLMs earn their keep by doing what math cannot: reading rules, synthesizing evidence, and detecting when the world changed but the market didn't notice.*

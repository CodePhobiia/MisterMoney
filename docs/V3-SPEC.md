# MisterMoney V3 — Resolution Intelligence Layer

*Spec by Butters, 2026-03-07*

## 1. Why V3

V1 makes markets. V2 allocates capital intelligently. But both treat fair value as book midpoint — they have zero opinion on whether a market *should* be at 60¢ or 80¢.

Polymarket markets resolve based on **written rules**, not price. The title is not the payout rule. Clarifications happen. Disputes take 4-6 days. Resolution sources are specific and documented.

An AI that can read rules, interpret news, and assess probability has **genuine alpha** that pure math cannot replicate. This is the one domain where LLMs decisively outperform quantitative models.

## 2. Architecture — Multi-Model Ensemble

V3 does NOT replace V1/V2. It produces a **fair value adjustment signal** that V2's scorer consumes.

```
┌─────────────────────────────────────────────┐
│                V3 Intelligence Layer         │
│                                             │
│  ┌──────────┐ ┌──────────┐ ┌──────────────┐│
│  │  Model A  │ │  Model B  │ │   Model C    ││
│  │ Opus 4.6  │ │Sonnet 4.6 │ │ Gemini 3 Pro ││
│  │           │ │           │ │              ││
│  │Resolution │ │  News &   │ │  Probability ││
│  │Rule Reader│ │ Sentiment │ │  Synthesizer ││
│  └─────┬─────┘ └─────┬─────┘ └──────┬───────┘│
│        │              │              │        │
│        └──────────┬───┴──────────────┘        │
│                   ▼                           │
│          ┌──────────────┐                     │
│          │  Ensemble     │                     │
│          │  Aggregator   │                     │
│          │  (weighted    │                     │
│          │   median)     │                     │
│          └──────┬───────┘                     │
│                 ▼                              │
│        ┌─────────────────┐                    │
│        │ Fair Value Delta │                    │
│        │  per market      │                    │
│        └────────┬────────┘                    │
└─────────────────┼─────────────────────────────┘
                  ▼
         V2 Scorer (adjusts reservation price)
                  ▼
         V1 Execution (quotes with directional skew)
```

## 3. The Three Roles

### 3.1 Model A — Resolution Rule Reader (Opus 4.6)

**Job**: Read the market's resolution rules, resolution source, and any clarifications. Produce a structured assessment.

**Input** (per market, every 30 minutes):
```json
{
  "question": "Will Bitcoin reach $100,000 by June 30, 2026?",
  "description": "This market resolves YES if Bitcoin...",
  "resolution_source": "CoinGecko BTC/USD spot price",
  "resolution_rules": "Resolves YES if the CoinGecko...",
  "clarifications": ["Clarification 1: ...", "..."],
  "current_mid": 0.62,
  "end_date": "2026-06-30T23:59:59Z",
  "hours_to_resolution": 2784
}
```

**Output**:
```json
{
  "rule_clarity_score": 0.85,        // 0-1, how unambiguous the rules are
  "dispute_risk": 0.05,             // P(dispute)
  "resolution_complexity": "simple", // simple, conditional, multi-step
  "key_criteria": [                  // what specifically must happen
    "BTC/USD spot on CoinGecko >= $100,000",
    "At any point before June 30, 2026 23:59 UTC"
  ],
  "ambiguities": [                   // identified issues
    "Does not specify which CoinGecko pair (BTC/USD vs BTC/USDT)"
  ],
  "early_resolution_possible": true, // can resolve before end_date?
  "placeholder_risk": false          // is this an "Other" outcome?
}
```

**Why Opus**: Highest reasoning capability. Rule interpretation requires nuanced reading comprehension and edge case detection. Worth the cost for the 30-min cadence.

### 3.2 Model B — News & Sentiment Analyst (Sonnet 4.6)

**Job**: Monitor real-time news, social media, and data sources. Detect events that shift market probabilities.

**Input** (per market, every 5 minutes):
```json
{
  "question": "Will Bitcoin reach $100,000 by June 30, 2026?",
  "current_mid": 0.62,
  "key_criteria": ["BTC/USD >= $100,000 before June 30"],  // from Model A
  "recent_news": [
    {"source": "Reuters", "title": "Bitcoin surges past $95,000...", "ts": "..."},
    {"source": "Twitter/X", "summary": "Whale alert: 10,000 BTC moved to...", "ts": "..."}
  ],
  "current_btc_price": 94500,  // from data feeds where applicable
  "resolution_source_current_value": "94,500"  // if checkable
}
```

**Output**:
```json
{
  "sentiment_score": 0.7,           // -1 (very bearish) to +1 (very bullish)
  "event_impact": "moderate",       // none, low, moderate, high, critical
  "probability_shift": +0.05,       // suggested probability adjustment
  "confidence": 0.6,                // how confident in the shift
  "reasoning": "BTC at $94.5K, only $5.5K from target with 4 months remaining...",
  "catalysts": [
    {"event": "BTC crossed $94K", "impact": "+3%", "confidence": 0.7},
    {"event": "Fed meeting next week", "impact": "±5%", "confidence": 0.4}
  ],
  "stale_market_flag": false         // true if mid hasn't moved but world has
}
```

**Why Sonnet**: Fast, cheap, good at summarization and pattern matching. The 5-min cadence needs throughput over depth.

### 3.3 Model C — Probability Synthesizer (Gemini 3 Pro)

**Job**: Take Model A's rule assessment and Model B's news analysis, plus market data, and produce a calibrated probability estimate.

**Input** (per market, every 15 minutes):
```json
{
  "question": "Will Bitcoin reach $100,000 by June 30, 2026?",
  "current_mid": 0.62,
  "rule_assessment": { /* Model A output */ },
  "news_analysis": { /* Model B output */ },
  "historical_similar_markets": [
    {"question": "Will BTC reach $50K by Dec 2024?", "resolved": "YES", "final_mid_before": 0.75}
  ],
  "time_series": {
    "mid_24h_ago": 0.58,
    "mid_7d_ago": 0.55,
    "volume_24h": 250000
  }
}
```

**Output**:
```json
{
  "fair_value": 0.68,               // Model C's probability estimate
  "confidence_interval": [0.60, 0.76],
  "edge_vs_market": +0.06,          // fair_value - current_mid
  "edge_confidence": 0.55,          // how confident in the edge
  "recommended_skew": "BUY",        // BUY, SELL, or NEUTRAL
  "max_edge_to_capture": 0.04,      // don't skew more than this (conservative)
  "reasoning": "Rule is clear. BTC at $94.5K with 4 months, historical base rate for similar 'reach $X' markets with asset within 6% of target is ~70%"
}
```

**Why Gemini**: Strong at structured reasoning and calibration. Google's training data includes extensive probability/forecasting research.

## 4. Ensemble Aggregator

The three models disagree. The aggregator resolves disagreements:

```python
def aggregate_fair_value(
    model_a_rule_clarity: float,
    model_b_probability_shift: float,
    model_b_confidence: float,
    model_c_fair_value: float,
    model_c_confidence: float,
    current_mid: float,
) -> FairValueSignal:
    """
    Weighted median of model outputs.
    
    Weights:
    - Model C gets base weight (it's the synthesizer)
    - Model B's shift is applied proportional to its confidence
    - Model A doesn't produce a probability — it modulates confidence
      (low rule_clarity → widen confidence interval → reduce edge capture)
    
    If models strongly disagree (>15% spread), reduce overall confidence.
    If rule_clarity < 0.5, halve max edge capture.
    If dispute_risk > 0.2, set recommended_skew to NEUTRAL.
    """
```

**Output: FairValueSignal**
```python
class FairValueSignal(BaseModel):
    condition_id: str
    fair_value: float          # ensemble probability
    confidence: float          # 0-1
    edge_vs_market: float      # fair_value - mid
    recommended_skew: str      # BUY, SELL, NEUTRAL
    max_skew_cents: float      # max cents to skew our quotes
    rule_clarity: float        # from Model A
    dispute_risk: float        # from Model A
    model_agreement: float     # how much models agree (0-1)
    stale_flag: bool           # Model B says market hasn't moved but should have
    timestamp: str
```

## 5. Integration with V2

V3 doesn't touch orders directly. It feeds into V2's scorer:

```python
# In pmm2/scorer/combined.py
async def score_bundle(self, market, bundle, reservation_price, nav):
    # ... existing EV components ...
    
    # V3 fair value adjustment
    v3_signal = self.v3_engine.get_signal(market.condition_id)
    if v3_signal and v3_signal.confidence > 0.4:
        # Adjust reservation price toward V3's fair value
        adjusted_r = reservation_price + v3_signal.max_skew_cents / 100.0
        # If V3 says BUY, we lower our bid less (more aggressive buying)
        # If V3 says SELL, we raise our ask less (more aggressive selling)
```

**V3 never overrides V2's risk limits.** It can skew our quotes by at most `max_skew_cents` (default: 2¢). If V3 is very confident, it can go up to 5¢. It cannot:
- Exceed position limits
- Override circuit breakers
- Force trades in tripped markets
- Ignore resolution risk from its own Model A

## 6. News & Data Sources

### Free / Built-in
- **Polymarket API**: Market metadata, resolution rules, clarifications
- **Web search**: Headlines, breaking news (via Brave API)
- **Wikipedia**: Background context for political/sports markets
- **Social media**: X/Twitter trending topics (via web scrape)

### Paid (optional, worth it)
- **NewsAPI.org** ($449/mo): Real-time news from 150K+ sources
- **Polygon.io** ($99/mo): Real-time crypto/stock prices for financial markets
- **Associated Press API**: Authoritative news for political markets

### Resolution Source Checking
For markets where the resolution source is programmatic (e.g., CoinGecko price, sports scores):
- V3 can CHECK the actual source in real time
- If BTC is at $99,500 and the resolution threshold is $100,000, that's a 99%+ probability
- This is the purest form of alpha: reading the resolution source before other traders update their orders

## 7. Cadences

| Loop | Interval | Model | What |
|------|----------|-------|------|
| Rule refresh | 30 min | Opus 4.6 | Re-read rules, check for clarifications |
| News scan | 5 min | Sonnet 4.6 | Check news, social, data feeds |
| Synthesis | 15 min | Gemini 3 Pro | Produce updated probability |
| Ensemble | 15 min | Local (no API) | Aggregate, produce FairValueSignal |
| Alert | Event-driven | Sonnet 4.6 | Critical event detected → immediate update |

**Cost estimate** (12 active markets):
- Opus: 12 markets × 2/hour × ~$0.03/call = ~$0.72/hour = **$17/day**
- Sonnet: 12 × 12/hour × ~$0.005/call = ~$0.72/hour = **$17/day**
- Gemini: 12 × 4/hour × ~$0.01/call = ~$0.48/hour = **$12/day**
- **Total: ~$46/day** at 12 markets

Scale to 30 markets: ~$115/day. This must generate >$115/day in alpha to be worth it.

## 8. Safety Rails

1. **Max skew = 5¢**: V3 can never move our quotes more than 5 cents from book mid
2. **Confidence floor = 0.4**: Below this, V3 signal is ignored
3. **Model agreement gate**: If models disagree by >15%, halve the skew
4. **Rule clarity gate**: If clarity < 0.5, max skew drops to 1¢
5. **Dispute gate**: If dispute_risk > 0.2, recommended_skew = NEUTRAL
6. **Stale model detection**: If a model hasn't responded in 2× its expected cadence, use last signal with decaying confidence
7. **Human override**: `/v3 pause` stops all AI signals, `/v3 override <market> <probability>` sets a manual fair value
8. **Shadow first**: V3 runs 10 days in shadow before any live skew

## 9. What This Enables

**Without V3** (V1+V2): We make money from spread capture, queue optimization, and rewards. Our fair value = book midpoint. We have zero opinion on direction.

**With V3**: We have an *informed opinion* on every market. When BTC is at $99,500 and the market says 62¢ for "BTC to $100K", we know that's mispriced and aggressively skew our quotes. When a market's resolution rules are ambiguous and a clarification just dropped, we pull our quotes before others react.

**The alpha sources**:
1. **Stale market detection**: Mid hasn't moved but the world has → skew first
2. **Resolution source reading**: Check the actual data source → update before crowd
3. **Rule interpretation**: Understand edge cases competitors don't → avoid bad markets
4. **Event reaction speed**: News breaks → update fair values in 5 minutes vs hours
5. **Dispute prediction**: Spot ambiguous rules → reduce exposure before disputes

## 10. Build Plan

| Sprint | What | Estimate |
|--------|------|----------|
| V3-S1 | Data pipeline (news feeds, resolution source checker) | 2-3 days |
| V3-S2 | Model A — Rule Reader (Opus) | 2 days |
| V3-S3 | Model B — News Analyst (Sonnet) | 2 days |
| V3-S4 | Model C — Probability Synthesizer (Gemini) | 2 days |
| V3-S5 | Ensemble aggregator + FairValueSignal | 1 day |
| V3-S6 | V2 integration (scorer adjustment) | 1 day |
| V3-S7 | Shadow mode (10 days observation) | 10 days calendar |
| V3-S8 | Live with 1¢ max skew | 1 week |
| V3-S9 | Full production (5¢ max skew) | ongoing |

**Total dev: ~10-12 days. Calendar: ~4 weeks to full production.**

## 11. The Vision

V1 = hands. V2 = brain. V3 = eyes and ears.

The complete system:
- **V1** executes orders safely and fast
- **V2** decides where to put capital and when to move orders
- **V3** sees the world and forms opinions about what markets are worth
- **V4** (future): cross-market graph intelligence — "if Market A resolves YES, Markets B, C, D all shift"

This is the correct sequence. Each layer earns the right for the next.

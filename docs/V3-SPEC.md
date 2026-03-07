# MisterMoney V3 — Resolution Intelligence Layer

*Spec v3.0 — 2026-03-07*
*Architecture: Theyab. Implementation: Butters.*
*Supersedes v2.1 (OAuth consumer model), v2.0 (routed architecture), v1.0 (fixed committee).*

---

## Design Principles

V3 is a **signal layer feeding V2**. It consumes Polymarket market discovery via Gamma events, live market updates from the public market WebSocket, and reward state where useful. Resolution logic keys off Polymarket's written rules, resolution source, end date, clarifications, and dispute flow — the title is not the payout rule. V2 still owns allocation and execution.

The system is **evidence-centric, not model-centric**. The question is not "which model do we call?" but "what evidence exists and how do we normalize it?"

## Access Layer

OAuth consumer endpoints via existing subscriptions. $0 marginal token cost — rate limits are the binding constraint, not dollars.

- **Anthropic**: Claude Pro/Max subscription OAuth → native Claude API endpoints (prompt caching, citations, PDF handling, thinking)
- **OpenAI**: ChatGPT Pro subscription OAuth → Responses API (tool use, prompt caching, configurable reasoning). Codex endpoint (`chatgpt.com/backend-api/codex/responses`) for GPT-5.4/5.4-pro.
- **Google**: Google AI Ultra subscription OAuth → Cloud Code Assist endpoint (`cloudcode-pa.googleapis.com`). Gemini 3.1 Pro Preview via CCA.

OAuth tokens stored in `auth-profiles.json`, auto-refreshed on expiry. V3 is engineered around **latency budgets and rate budgets** — cost is $0 per token.

Native vendor APIs and SDKs where possible. Anthropic's OpenAI SDK compatibility layer is for testing, not production — use native Claude API for full prompt caching and thinking support. Gemini's official SDK handles thought signatures automatically for tool/function workflows.

### Auth Flow
Each provider adapter handles OAuth token refresh internally. Tokens are persisted to disk and refreshed before expiry. If a provider's OAuth is broken or rate-limited, the route publishes cached signal or neutral — no silent model substitution.

## Topology

Modular monolith with worker pools. No microservice fleet yet.

- One API process
- One hot-path worker pool
- One deep-analysis worker pool
- One offline/evals worker pool

Backing services:

- **Postgres/TimescaleDB**: Durable state and time series
- **Redis**: Hot cache, locks, and event streams
- **Object storage**: Raw PDFs/pages/article bodies
- **pgvector** (inside Postgres): Evidence retrieval

No Kafka. No Temporal. No separate vector DB. Not yet.

## Logical Architecture

```
Polymarket Gamma / WS / rules / clarifications
Official resolution-source APIs
News + document fetchers
V1/V2 telemetry
        │
        ▼
 [1] Intake + Normalization
        │
        ▼
 [2] Evidence Graph + Hot Cache
        │
        ▼
 [3] Change Detector + Router
        │
   ┌────────┼───────────┬───────────────┐
   ▼        ▼           ▼               ▼
Numeric   Simple    Rule-heavy       Dossier
route     route       route           route
  │        │           │               │
  │      Sonnet      Sonnet          Sonnet
  │        │           │               │
  │        │         Opus            Gemini
  │        │           │               │
  │        └──────► GPT-5.4 ◄────────┘
  │                    │
  └──── optional AI    │
         anomaly       │
                       ▼
            [4] Calibrator + Uncertainty
                       │
                       ▼
            [5] Signal Publisher
                       │
                       ▼
              V2 scorer / allocator
                       │
                       ▼
                V1 execution
```

---

## Layer 1 — Intake + Normalization

Deterministic and cheap. **No LLM calls.**

Ingests:
- Polymarket market metadata and active markets
- Rule text and clarifications
- Public market-channel order-book updates
- V1/V2 telemetry
- Source-check values from known resolution-source adapters
- Fetched articles, docs, PDFs, and official statements

### Module Structure

```
v3/intake/
    gamma_sync.py
    market_ws.py
    rules_sync.py
    source_registry.py
    source_checkers/
        coingecko.py
        cpi_bls.py
        espn_scores.py
        sec_filings.py
    news_fetch.py
    doc_fetch.py
```

---

## Layer 2 — Evidence Graph + Hot Cache

Every fetched artifact becomes:
- A `SourceDocument`
- Zero or more normalized `EvidenceItem`s
- Zero or more structured `Claim`s
- A linked `RuleGraph`
- A provenance chain

This gives us reproducibility, de-duplication, and cross-model consistency.

### Core Entities

```python
class SourceDocument(BaseModel):
    doc_id: str
    url: str | None
    source_type: Literal["official", "news", "pdf", "api", "social", "manual"]
    publisher: str | None
    fetched_at: datetime
    content_hash: str
    title: str | None
    text_path: str  # object store key
    metadata: dict = {}

class EvidenceItem(BaseModel):
    evidence_id: str
    condition_id: str
    doc_id: str
    ts_event: datetime | None
    ts_observed: datetime
    polarity: Literal["YES", "NO", "MIXED", "NEUTRAL"]
    claim: str
    reliability: float  # source quality prior
    freshness_hours: float
    extracted_values: dict = {}

class RuleGraph(BaseModel):
    condition_id: str
    source_name: str
    operator: Literal[">=", "<=", "==", "contains", "exists", "manual"]
    threshold_num: float | None = None
    threshold_text: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    edge_cases: list[str] = []
    clarification_ids: list[str] = []
```

**Key design decision**: Models should almost never see raw web pages first. They see normalized evidence bundles built from this graph.

---

## Layer 3 — Change Detector + Router

The online brain. Decides whether a market needs:
- No refresh
- Deterministic refresh only
- Cheap triage
- Deep specialist pass
- Async adjudication

### Trigger Schema (Event-Driven, Not Cron)

```python
class ChangeEvent(BaseModel):
    condition_id: str
    trigger: Literal[
        "source_crossed_band",
        "new_clarification",
        "new_high_reliability_evidence",
        "world_changed_mid_stale",
        "approaching_resolution",
        "large_mid_move",
        "large_volume_spike",
        "provider_refresh",
    ]
    severity: float
    payload: dict
    ts: datetime
```

### Route Plan

```python
class RoutePlan(BaseModel):
    condition_id: str
    market_type: Literal["numeric", "simple", "rule", "dossier"]
    sla_ms: int
    route: list[str]  # e.g. ["numeric_solver", "sonnet", "gpt_5_4"]
    allow_async_escalation: bool
    evidence_bundle_id: str
    cost_budget_usd: float
    token_budget_in: int
    token_budget_out: int
```

---

## Model Roles — Production Placement

### Sonnet 4.6

Hot-path triage and extraction model. Adaptive thinking enabled. First-pass for:
- Rule-to-schema extraction
- Entity extraction
- News triage
- "Does this need escalation?" decisions

### Opus 4.6

Blind rule analysis and adversarial challenge **only**. Adaptive thinking (not manual `budget_tokens`). For:
- Ambiguity detection
- Disputes and clarifications
- Near-resolution markets

**Not** run on a timer across the full universe.

### Gemini 3.1 Pro Preview

Long-context dossier specialist. 1,048,576-token input window, structured outputs, function calling, search grounding, URL context, caching, thinking support. For:
- Large multi-document bundles
- PDFs and filings
- Contradiction detection

Use official Google Gen AI SDK. Keep provider state opaque inside the adapter. **Not** Gemini 3 Pro Preview (deprecated, shuts down March 9 2026). Gemini Interactions API is Beta — not a hot-path dependency.

**Do not use Gemini's hosted file search for production RAG** (AI Studio only). Use our own retrieval layer; Gemini consumes normalized evidence bundles.

### GPT-5.4

Online market-aware judge and orchestrator. Default mainline model. Tool use, 1M context, configurable reasoning effort. The pass that sees blind estimate + market state → tradable judgment.

### GPT-5.4-pro High

**Async adjudicator only.** Responses-API-only, slower, some requests take minutes. Background mode recommended. For:
- High-notional decisions
- Strong cross-model disagreement
- Near-resolution markets
- Post-mortem review
- Weekly label generation and evals

**Never blocks quote publication.**

---

## Route Design

### Numeric Route

1. Source checker
2. Quant barrier / hazard solver
3. Optional Sonnet regime-shift classifier
4. GPT-5.4 only if anomaly detected

**No LLM should own the probability for machine-readable threshold markets.**

### Simple Route

1. Sonnet blind estimate + extraction
2. GPT-5.4 market-aware judge
3. Calibrator

### Rule-Heavy Route

1. Sonnet extraction
2. Opus blind rule analysis
3. GPT-5.4 market-aware judge
4. Optional GPT-5.4-pro High async review

### Dossier Route

1. Sonnet extraction / routing
2. Gemini dossier synthesis
3. Opus adversarial challenge
4. GPT-5.4 market-aware judge
5. Optional GPT-5.4-pro High async review

---

## Output Contracts

**No `reasoning_trace` in business logic.** Claude 4 returns summarized thinking (not stable raw trace). Gemini 3 uses thought signatures that must be round-tripped exactly during tool/function turns.

Store:
- Structured fields
- Evidence IDs
- Uncertainty reasons
- Counterevidence IDs
- Opaque provider continuation state

### Schemas

```python
class BlindEstimate(BaseModel):
    condition_id: str
    route: Literal["numeric", "simple", "rule", "dossier"]
    p_blind: float
    p_low: float
    p_high: float
    rule_clarity: float
    dispute_risk: float
    evidence_ids: list[str]
    counterevidence_ids: list[str]
    uncertainty_reasons: list[str]
    provider_state_ref: str | None = None
    prompt_version: str

class MarketAwareDecision(BaseModel):
    condition_id: str
    action: Literal["BUY", "SELL", "NEUTRAL"]
    p_market_aware: float
    edge_after_costs: float
    max_skew_cents: float
    stale_market: bool
    rationale_summary: str
    evidence_ids: list[str]
    prompt_version: str

class FairValueSignal(BaseModel):
    condition_id: str
    p_calibrated: float
    p_low: float
    p_high: float
    uncertainty: float
    skew_cents: float
    hurdle_cents: float
    hurdle_met: bool
    route: Literal["numeric", "simple", "rule", "dossier"]
    evidence_ids: list[str]
    counterevidence_ids: list[str]
    models_used: list[str]
    generated_at: datetime
    expires_at: datetime
```

---

## Calibration

**Route-specific calibrators, not one global calibrator.** Numeric markets and rule-heavy markets fail in different ways.

- `numeric_calibrator`
- `simple_calibrator`
- `rule_calibrator`
- `dossier_calibrator`

### Math

Raw score:

$$p_{raw} = \sigma(\beta_{route}^\top x)$$

Conformal intervals per route:

$$[p_{low}, p_{high}] = \text{ConformalInterval}_{route}(x)$$

Stale signal decay toward market prior:

$$p_{live} = \lambda(age) \cdot p_{raw} + (1 - \lambda(age)) \cdot m_t$$

where $\lambda(age)$ shrinks with signal age and source staleness.

---

## Retrieval and Tool Policy

**Centralized retrieval.** Do not let every model browse the live web independently on the hot path.

- Anthropic charges separately for web search
- OpenAI charges web-search tool calls + search content tokens
- Gemini search grounding is separately priced after free allotment

### Policy

- **Primary**: Our own evidence service and retrieval
- **Secondary**: Vendor built-in search/fetch only when evidence bundle is incomplete or stale
- **Never**: Unrestricted model web access on quote-critical paths

### Internal Tools Exposed to Models

```
get_rule_graph(condition_id)
get_source_check(condition_id)
search_evidence(condition_id, query)
fetch_doc_excerpt(doc_id, start, end)
get_market_context(condition_id)
```

Narrow tool set per turn for safety, predictability, and caching.

---

## Caching Strategy

Design prompts so the long, static prefix is stable:
- System instructions
- Rule text
- Clarifications
- Canonical market metadata
- Normalized evidence bundle
- JSON schema

Only the small market-context suffix varies.

Anthropic, OpenAI, and Gemini 3.1 Pro all support prompt caching. Use aggressively for blind passes and dossier routes.

**Long-context nuance**: Anthropic offers 1M context on Opus/Sonnet 4.6 (beta, dedicated rate limits, special pricing >200K tokens). **Don't make Claude the default raw-dossier reader.** Use Gemini for raw dossiers, Claude as challenger over condensed evidence bundles.

---

## Runtime SLAs

| Route | Publish | Refresh | Notes |
|-------|---------|---------|-------|
| Numeric | 250ms–1s | — | Source checker + quant solver |
| Simple | 1–3s | — | Sonnet + GPT-5.4 |
| Rule-heavy | Cached immediately | 5–15s | Opus async |
| Dossier | Cached immediately | 15–120s async | Gemini + Opus + GPT-5.4 |
| GPT-5.4-pro | — | Async only | Never blocks publication |

If a deep route misses SLA, V2 gets the last non-expired signal or neutral.

---

## Hard TTLs by Route

| Route | TTL |
|-------|-----|
| Numeric | 60s |
| Simple | 15 min |
| Rule-heavy | 30 min |
| Dossier | 2 hours |

---

## Guardrails

1. **No silent downgrade on critical routes.** If Opus or Gemini is unavailable where that route matters, publish cached or neutral — don't silently replace with cheaper model and pretend signal is comparable.

2. **Hard TTLs by route** (see above).

3. **Provider-state isolation.** Store provider continuation state separately per vendor. Never merge across vendors.

4. **Evidence minimum.** No non-neutral signal unless at least one high-reliability evidence item or deterministic source check supports it.

5. **Dynamic hurdle gate**: `|p̂_t - m_t| > h_t` where `h_t = h₀ + h_model + h_resolution + h_tox`. No trade without real edge.

6. **Cold start**: Until 50+ resolved markets with predictions, all β except β₀ (market prior) = 0. Gradually increase model weights.

---

## Deployment Shape

Four process types from one repo:

```
services/
    v3_api           # FastAPI read/write endpoints, operator controls
    v3_hot_worker    # numeric/simple routing, source checks, Sonnet, GPT-5.4
    v3_deep_worker   # Opus, Gemini, async dossier jobs
    v3_offline_worker  # GPT-5.4-pro, replay, evals, retraining
```

### Repository Structure

```
pmm/
  v3/
    config.py
    api/
      app.py
      routes.py
    intake/
      gamma_sync.py
      market_ws.py
      rules_sync.py
      source_registry.py
      source_checkers/
    evidence/
      normalizer.py
      graph.py
      retrieval.py
      entities.py
      claims.py
    routing/
      classifier.py
      change_detector.py
      orchestrator.py
      slas.py
    providers/
      base.py
      anthropic_adapter.py
      gemini_adapter.py
      openai_adapter.py
      schemas.py
    routes/
      numeric.py
      simple.py
      rule_heavy.py
      dossier.py
    calibration/
      route_models.py
      conformal.py
      decay.py
    serving/
      publisher.py
      cache.py
      consumer.py
    evals/
      dataset_builder.py
      replay.py
      model_eval.py
      reports.py
```

---

## Observability and Evals

V3 lives or dies on measurement.

Track:
- Route latency
- Provider latency and failures
- Cache-hit rate
- Token and tool spend
- Stale-market precision / recall
- Rule-parse accuracy
- Brier score by route
- Realized post-trade edge
- Model disagreement by market type
- V3 contribution to PnL before and after rewards

Cross-vendor benchmarking: OpenAI evals support for external models and custom endpoints on our Polymarket datasets. One evaluation harness for GPT, Claude, and Gemini outputs.

---

## Build Order

| Sprint | Description | Depends On |
|--------|-------------|------------|
| S0 | **Access layer** — official vendor APIs, server-side keys in KMS, native SDK adapters, remove consumer-endpoint assumptions | — |
| S1 | **Evidence layer** — source registry, rule graph, evidence graph, retrieval, Postgres schema, pgvector | S0 |
| S2 | **Numeric route** — source checkers, quant barrier/hazard solver, fastest ROI, least model spend | S1 |
| S3 | **Simple route** — Sonnet hot-path triage + GPT-5.4 online judge | S1 |
| S4 | **Rule-heavy route** — Opus blind rule analysis + GPT-5.4 judge | S3 |
| S5 | **Dossier route** — Gemini long-context synthesis + Opus adversarial + GPT-5.4 judge | S3, S4 |
| S6 | **Route-specific calibrators** — conformal intervals, signal decay, per-route β weights | S2, S3, S4, S5 |
| S7 | **Shadow mode** — 10-14 days shadow only, signal logging, counterfactual comparison with V2 | S6 |
| S8 | **Canary live** — 1¢ max V3 skew, gradual ramp | S7 |
| S9 | **GPT-5.4-pro async** — high-notional adjudication, post-mortem review, weekly evals | S8 |

---

*This is the architecture we ship.*

# MisterMoney V3 — Engineering Work Breakdown Structure

*Generated 2026-03-07 from V3-SPEC v3.0*

---

## Sprint 0 — Access Layer (~3 days)

**Goal**: OAuth consumer endpoint integration with native SDKs. Token refresh, rate-limit tracking, provider health.

### Tickets

#### S0-T1: Provider Base Class + Config
```python
# v3/providers/base.py
class ProviderConfig(BaseModel):
    provider: Literal["anthropic", "openai", "google"]
    model: str
    max_tokens_in: int
    max_tokens_out: int
    timeout_ms: int
    rate_limit_rpm: int        # binding constraint (not cost)
    auth_profile: str          # key in auth-profiles.json

class ProviderResponse(BaseModel):
    text: str
    structured: dict | None
    input_tokens: int
    output_tokens: int
    latency_ms: float
    provider_state_ref: str | None  # opaque continuation state
    cache_hit: bool

class BaseProvider(ABC):
    async def complete(self, messages: list, tools: list | None,
                       response_format: type[BaseModel] | None,
                       reasoning_effort: str | None) -> ProviderResponse: ...
    async def health_check(self) -> bool: ...
    async def refresh_token(self) -> bool: ...
```

#### S0-T2: OAuth Token Manager
```python
# v3/providers/oauth.py
class OAuthTokenManager:
    """Manages OAuth tokens for all providers. Reads/writes auth-profiles.json."""
    profiles_path: Path  # auth-profiles.json
    
    async def get_token(self, profile: str) -> str: ...
    async def refresh_if_needed(self, profile: str) -> str: ...
    async def save_token(self, profile: str, token_data: dict) -> None: ...
    # Auto-refresh before expiry (5 min buffer)
    # Persists to disk on every refresh
    # Existing profiles:
    #   - openai-codex (ChatGPT Pro, Codex endpoint)
    #   - google-gemini-cli (Google AI Ultra, CCA endpoint)
    #   - anthropic (Claude Pro/Max — TBD, need to discover OAuth flow)
```

#### S0-T3: Anthropic OAuth Adapter
- Native `anthropic` Python SDK with OAuth bearer token
- Prompt caching via `cache_control` blocks
- Extended thinking with adaptive mode on Sonnet/Opus
- Structured output via `response_format`
- Tool use via native tool schema (not OpenAI shim)
- **Auth**: Claude Pro/Max subscription OAuth (need to discover endpoint + refresh flow — may need Anthropic web session cookies initially)

```python
# v3/providers/anthropic_adapter.py
class AnthropicProvider(BaseProvider):
    # Supports: sonnet-4-6, opus-4-6
    # Prompt caching: system + evidence prefix marked cacheable
    # Thinking: adaptive (Sonnet triage), adaptive (Opus rule analysis)
    # Auth: OAuth token from OAuthTokenManager
```

#### S0-T4: OpenAI OAuth Adapter
- Codex endpoint: `chatgpt.com/backend-api/codex/responses` with SSE streaming
- GPT-5.4: online, configurable `reasoning.effort` (low/medium/high)
- GPT-5.4-pro: `reasoning.effort=high|xhigh`, background mode for async jobs
- **Auth**: ChatGPT Pro OAuth token (already have working flow from Chimera semantic proxy)

```python
# v3/providers/openai_adapter.py
class OpenAIProvider(BaseProvider):
    # GPT-5.4: default reasoning, online judge
    # GPT-5.4-pro: background=True for async, reasoning.effort=high
    # Auth: Codex OAuth token → chatgpt.com/backend-api/codex/responses
    # SSE streaming: stream=true, parse delta events
```

#### S0-T5: Google OAuth Adapter
- Cloud Code Assist endpoint: `cloudcode-pa.googleapis.com/v1internal:generateContent`
- Gemini 3.1 Pro Preview: 1M context, structured output, thinking
- **Auth**: Google AI Ultra OAuth (already have working flow from Chimera)
- **Known rate limit**: CCA throttles after first call per burst with 403 — needs backoff/retry
- **No** Interactions API (beta, unstable)
- **No** hosted file search (AI Studio only)

```python
# v3/providers/gemini_adapter.py
class GeminiProvider(BaseProvider):
    # Model: gemini-3.1-pro-preview via CCA
    # Auth: Google OAuth token from OAuthTokenManager
    # Rate limit handling: exponential backoff on 403
    # Structured output via response schema in request body
```

#### S0-T6: Provider Registry + Health Monitor
```python
# v3/providers/registry.py
class ProviderRegistry:
    providers: dict[str, BaseProvider]
    async def get(self, role: str) -> BaseProvider: ...
    async def is_available(self, role: str) -> bool: ...
    # No silent downgrade: if provider OAuth broken/rate-limited,
    # return None (caller publishes cached/neutral)
    # No fallback chain between providers — each route specifies exact provider
```

#### S0-T7: Rate Budget Tracker
```python
# v3/providers/budgets.py
class RateBudgetTracker:
    async def check_rate_budget(self, provider: str) -> bool: ...
    async def record_usage(self, provider: str, input_tokens: int,
                           output_tokens: int, latency_ms: float): ...
    # Per-provider rate limits (RPM, TPM)
    # Sliding window in Redis
    # Cost tracking for observability only (all calls are $0 marginal)
```

**Deliverables**: OAuth token manager, 3 provider adapters, registry, rate tracker. All compile + unit test. Verified with live OAuth tokens against each provider.

---

## Sprint 1 — Evidence Layer (~5 days)

**Goal**: Source registry, rule graph, evidence graph, retrieval. Postgres schema + pgvector.

### Tickets

#### S1-T1: Postgres Schema + Migrations
```sql
-- source_documents
CREATE TABLE source_documents (
    doc_id TEXT PRIMARY KEY,
    url TEXT,
    source_type TEXT NOT NULL,  -- official|news|pdf|api|social|manual
    publisher TEXT,
    fetched_at TIMESTAMPTZ NOT NULL,
    content_hash TEXT NOT NULL,
    title TEXT,
    text_path TEXT NOT NULL,     -- object store key
    metadata JSONB DEFAULT '{}',
    embedding vector(1536)       -- pgvector for retrieval
);

-- evidence_items
CREATE TABLE evidence_items (
    evidence_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL,
    doc_id TEXT REFERENCES source_documents(doc_id),
    ts_event TIMESTAMPTZ,
    ts_observed TIMESTAMPTZ NOT NULL,
    polarity TEXT NOT NULL,      -- YES|NO|MIXED|NEUTRAL
    claim TEXT NOT NULL,
    reliability FLOAT NOT NULL,
    freshness_hours FLOAT NOT NULL,
    extracted_values JSONB DEFAULT '{}',
    embedding vector(1536)
);
CREATE INDEX ON evidence_items(condition_id);

-- rule_graphs
CREATE TABLE rule_graphs (
    condition_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    operator TEXT NOT NULL,
    threshold_num FLOAT,
    threshold_text TEXT,
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    edge_cases JSONB DEFAULT '[]',
    clarification_ids JSONB DEFAULT '[]',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- signals (output)
CREATE TABLE fair_value_signals (
    condition_id TEXT NOT NULL,
    p_calibrated FLOAT NOT NULL,
    p_low FLOAT NOT NULL,
    p_high FLOAT NOT NULL,
    uncertainty FLOAT NOT NULL,
    skew_cents FLOAT NOT NULL,
    hurdle_cents FLOAT NOT NULL,
    hurdle_met BOOLEAN NOT NULL,
    route TEXT NOT NULL,
    evidence_ids JSONB NOT NULL,
    counterevidence_ids JSONB NOT NULL,
    models_used JSONB NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (condition_id, generated_at)
);
-- TimescaleDB hypertable for time-series queries
SELECT create_hypertable('fair_value_signals', 'generated_at');
```

#### S1-T2: Entity Models (Pydantic)
All entities from spec: `SourceDocument`, `EvidenceItem`, `RuleGraph`, `BlindEstimate`, `MarketAwareDecision`, `FairValueSignal`, `ChangeEvent`, `RoutePlan`.

```python
# v3/evidence/entities.py — all Pydantic models
# v3/providers/schemas.py — provider I/O schemas
# v3/routing/schemas.py — ChangeEvent, RoutePlan
```

#### S1-T3: Evidence Graph CRUD
```python
# v3/evidence/graph.py
class EvidenceGraph:
    async def upsert_document(self, doc: SourceDocument) -> str: ...
    async def add_evidence(self, item: EvidenceItem) -> str: ...
    async def upsert_rule_graph(self, rule: RuleGraph) -> None: ...
    async def get_evidence_bundle(self, condition_id: str, max_items: int = 20) -> list[EvidenceItem]: ...
    async def get_rule_graph(self, condition_id: str) -> RuleGraph | None: ...
    async def deduplicate(self, condition_id: str) -> int: ...  # by content_hash
```

#### S1-T4: Evidence Retrieval (pgvector)
```python
# v3/evidence/retrieval.py
class EvidenceRetrieval:
    async def search(self, condition_id: str, query: str, top_k: int = 10) -> list[EvidenceItem]: ...
    async def embed_and_store(self, doc: SourceDocument, text: str) -> None: ...
    # Uses pgvector cosine similarity
    # Embedding model: text-embedding-3-small (OpenAI) or local ONNX
```

#### S1-T5: Evidence Normalizer
```python
# v3/evidence/normalizer.py
class EvidenceNormalizer:
    def normalize_article(self, raw_html: str, url: str, publisher: str) -> SourceDocument: ...
    def normalize_api_response(self, data: dict, source_name: str) -> SourceDocument: ...
    def extract_claims(self, doc: SourceDocument, text: str) -> list[EvidenceItem]: ...
    # Deterministic extraction first (regex, structured data)
    # LLM extraction only for unstructured text (deferred to S3+)
```

#### S1-T6: Object Storage Adapter
```python
# v3/evidence/storage.py
class ObjectStore:
    async def put(self, key: str, content: bytes, content_type: str) -> str: ...
    async def get(self, key: str) -> bytes: ...
    # Local filesystem for dev, S3 for prod
```

**Deliverables**: Postgres schema, all Pydantic entities, evidence graph CRUD, pgvector retrieval, normalizer, object store. Integration tested with real Polymarket data.

---

## Sprint 2 — Numeric Route (~3 days)

**Goal**: Fastest ROI, least model spend. Deterministic source checkers + quant solvers.

### Tickets

#### S2-T1: Source Registry
```python
# v3/intake/source_registry.py
class SourceRegistry:
    # Maps resolution_source → checker function
    # e.g. "coingecko.com" → CoinGeckoChecker
    #      "bls.gov" → CPIChecker
    #      "espn.com" → ESPNChecker
    def get_checker(self, resolution_source: str) -> SourceChecker | None: ...
    def classify_market(self, question: str, rules: str, source: str) -> Literal["numeric", "simple", "rule", "dossier"]: ...
```

#### S2-T2: Source Checkers (3-4 initial)
```python
# v3/intake/source_checkers/base.py
class SourceCheckResult(BaseModel):
    condition_id: str
    source: str
    current_value: float | str
    threshold: float | str
    probability: float  # deterministic computation
    confidence: float
    checked_at: datetime
    raw_data: dict

class SourceChecker(ABC):
    async def check(self, condition_id: str, rule: RuleGraph) -> SourceCheckResult: ...

# Implement: CoinGeckoChecker, CPIBLSChecker, ESPNChecker, SECFilingsChecker
```

#### S2-T3: Quant Barrier / Hazard Solver
```python
# v3/routes/numeric.py
class NumericRoute:
    async def solve(self, condition_id: str, rule: RuleGraph,
                    source_check: SourceCheckResult,
                    evidence: list[EvidenceItem]) -> FairValueSignal: ...
    # Barrier probability for price/threshold markets
    # Hazard rate for time-to-event markets
    # Optional Sonnet classifier only if anomaly detected
```

#### S2-T4: Anomaly Detector (Optional Sonnet)
```python
# Only fires if source_check.confidence < 0.8 or regime_shift detected
# Sonnet classifies: normal | regime_shift | data_quality_issue
# If normal → publish numeric signal
# If anomaly → escalate to GPT-5.4
```

**Deliverables**: 4 source checkers, quant solver, numeric route end-to-end. Testable against live Polymarket numeric markets.

---

## Sprint 3 — Simple Route (~4 days)

**Goal**: Sonnet hot-path triage + GPT-5.4 online judge. The workhorse route.

### Tickets

#### S3-T1: Intake — Gamma Sync + Rules Sync
```python
# v3/intake/gamma_sync.py
class GammaSync:
    async def sync_markets(self, limit: int = 200) -> list[MarketMeta]: ...
    async def sync_rules(self, condition_ids: list[str]) -> dict[str, str]: ...
    async def sync_clarifications(self, condition_ids: list[str]) -> dict[str, list[str]]: ...
```

#### S3-T2: Change Detector
```python
# v3/routing/change_detector.py
class ChangeDetector:
    async def detect(self, condition_id: str) -> ChangeEvent | None: ...
    # Checks: source crossed band, new evidence, mid move, volume spike, approaching resolution
    # Returns None if no refresh needed
```

#### S3-T3: Sonnet Blind Estimate Pass
```python
# v3/routes/simple.py
class SimpleRoute:
    async def blind_pass(self, condition_id: str,
                         evidence_bundle: list[EvidenceItem],
                         rule: RuleGraph | None) -> BlindEstimate: ...
    # Sonnet 4.6 with adaptive thinking
    # Prompt: evidence bundle + rule text + JSON schema
    # Output: BlindEstimate (p_blind, uncertainty, evidence_ids)
    # NO market price visible in this pass
```

#### S3-T4: GPT-5.4 Market-Aware Judge
```python
    async def market_aware_pass(self, condition_id: str,
                                blind: BlindEstimate,
                                market_mid: float,
                                market_volume: float,
                                market_spread: float) -> MarketAwareDecision: ...
    # GPT-5.4 with medium reasoning
    # Sees: blind estimate + current market state
    # Output: MarketAwareDecision (action, p_market_aware, edge_after_costs)
```

#### S3-T5: Prompt Templates + Versioning
```python
# v3/routes/prompts/
#   simple_blind_v1.py
#   simple_judge_v1.py
# Each prompt has a version string embedded in output
# Cache-friendly: long stable prefix (system + evidence), short dynamic suffix (market state)
```

#### S3-T6: Route Orchestrator
```python
# v3/routing/orchestrator.py
class RouteOrchestrator:
    async def execute(self, plan: RoutePlan) -> FairValueSignal: ...
    # Dispatches to numeric/simple/rule/dossier based on plan.market_type
    # Enforces SLA timeouts
    # On SLA miss: return last cached signal or neutral
```

**Deliverables**: End-to-end simple route: Sonnet blind → GPT-5.4 judge → FairValueSignal. Tested on 10+ live markets.

---

## Sprint 4 — Rule-Heavy Route (~3 days)

**Goal**: Opus blind rule analysis for ambiguous/disputed markets.

### Tickets

#### S4-T1: Opus Blind Rule Analysis
```python
# v3/routes/rule_heavy.py
class RuleHeavyRoute:
    async def opus_rule_pass(self, condition_id: str,
                             rule: RuleGraph,
                             clarifications: list[str],
                             evidence: list[EvidenceItem]) -> BlindEstimate: ...
    # Opus 4.6 with adaptive thinking
    # Focus: ambiguity, edge cases, dispute risk, clarification interpretation
    # Output: BlindEstimate with high dispute_risk / low rule_clarity flags
```

#### S4-T2: Escalation Logic
```python
    async def should_escalate_async(self, blind: BlindEstimate,
                                     market_notional: float) -> bool: ...
    # Escalate to GPT-5.4-pro if:
    #   dispute_risk > 0.3
    #   rule_clarity < 0.5
    #   market_notional > $50
    #   approaching_resolution (< 24h)
```

**Deliverables**: Opus rule analysis + escalation logic. Tested on 5+ ambiguous markets.

---

## Sprint 5 — Dossier Route (~4 days)

**Goal**: Gemini long-context synthesis for document-heavy markets.

### Tickets

#### S5-T1: Gemini Dossier Synthesis
```python
# v3/routes/dossier.py
class DossierRoute:
    async def gemini_synthesis(self, condition_id: str,
                               documents: list[SourceDocument],
                               evidence: list[EvidenceItem],
                               rule: RuleGraph) -> BlindEstimate: ...
    # Gemini 3.1 Pro with up to 1M tokens
    # Input: normalized evidence bundle (NOT raw web pages)
    # Use CachedContent API for stable evidence prefix
    # Contradiction detection across documents
```

#### S5-T2: Opus Adversarial Challenge
```python
    async def opus_challenge(self, condition_id: str,
                             gemini_estimate: BlindEstimate,
                             evidence: list[EvidenceItem]) -> BlindEstimate: ...
    # Opus sees Gemini's estimate + condensed evidence
    # Challenge: find counterevidence, flag overconfidence, dispute risk
    # Output: independent BlindEstimate (may disagree with Gemini)
```

#### S5-T3: Disagreement Resolution
```python
    async def resolve_disagreement(self, gemini: BlindEstimate,
                                    opus: BlindEstimate,
                                    market_mid: float) -> MarketAwareDecision: ...
    # GPT-5.4 judges if Gemini and Opus disagree by > threshold
    # If strong disagreement + high notional → queue for GPT-5.4-pro async
```

**Deliverables**: Full dossier pipeline. Tested on 3+ document-heavy markets.

---

## Sprint 6 — Route-Specific Calibrators (~4 days)

**Goal**: Four calibrators with conformal intervals + signal decay.

### Tickets

#### S6-T1: Route Calibrator Models
```python
# v3/calibration/route_models.py
class RouteCalibrator:
    route: Literal["numeric", "simple", "rule", "dossier"]
    beta: np.ndarray  # learned weights
    
    def calibrate(self, raw_p: float, features: dict) -> float: ...
    # p_raw = sigmoid(beta_route^T @ x)
    
    def conformal_interval(self, features: dict) -> tuple[float, float]: ...
    # [p_low, p_high] = ConformalInterval_route(x)
    # Requires resolved market dataset (cold start: wide intervals)
```

#### S6-T2: Signal Decay
```python
# v3/calibration/decay.py
def decay_signal(p_raw: float, market_mid: float, age_seconds: float,
                 source_staleness: float, route: str) -> float:
    """p_live = lambda(age) * p_raw + (1 - lambda(age)) * m_t"""
    # lambda shrinks with age and staleness
    # Route-specific half-lives
```

#### S6-T3: Cold Start Strategy
```python
# Until 50+ resolved markets with predictions:
#   - beta = [beta_0 (market prior), 0, 0, ...]
#   - Conformal intervals are wide
#   - Signal published but with high uncertainty + low hurdle_met rate
```

#### S6-T4: Signal Publisher
```python
# v3/serving/publisher.py
class SignalPublisher:
    async def publish(self, signal: FairValueSignal) -> None: ...
    # Write to fair_value_signals table
    # Push to Redis for V2 consumer
    # Log for observability
    
    async def get_latest(self, condition_id: str) -> FairValueSignal | None: ...
    async def get_cached_or_neutral(self, condition_id: str) -> FairValueSignal: ...
```

#### S6-T5: V2 Consumer Integration
```python
# v3/serving/consumer.py
class V3Consumer:
    """Called by V2 scorer to get V3 fair value signal."""
    async def get_fair_value(self, condition_id: str) -> float | None: ...
    # Returns p_calibrated if hurdle_met and not expired
    # Returns None otherwise (V2 uses book midpoint as before)
```

**Deliverables**: 4 calibrators, decay, publisher, V2 consumer. Cold start safe.

---

## Sprint 7 — Shadow Mode (~3 days)

**Goal**: All routes running, signals logged, nothing affecting V2 decisions. 10-14 day observation.

### Tickets

#### S7-T1: Shadow Logger
- All FairValueSignals written to DB + JSONL
- Counterfactual: what V2 would have done with vs without V3 signal
- Daily Telegram report

#### S7-T2: Brier Score Tracker
- Per-route Brier scores on resolved markets
- Model disagreement tracking
- Cache-hit rates, latency distributions

#### S7-T3: Cost Tracker
- Per-provider token spend
- Per-route cost distribution
- Daily cost Telegram alert

---

## Sprint 8 — Canary Live (~2 days)

**Goal**: V3 signals influence V2 with 1¢ max skew cap.

- `v3_max_skew_cents: 1` in config
- V2 scorer clamps V3 contribution
- Gradual ramp: 1¢ → 2¢ → 5¢ → uncapped
- Kill switch: `v3_enabled: false`

---

## Sprint 9 — GPT-5.4-pro Async Adjudication (~3 days)

**Goal**: Async review tier for high-stakes decisions.

- Queue: Redis sorted set by priority
- Worker: `v3_offline_worker` pulls jobs, runs GPT-5.4-pro with `reasoning.effort=high`, `background=True`
- Results update signals DB + trigger re-publish
- Weekly evals: GPT-5.4-pro reviews all resolved markets, generates labels for calibration retraining

---

## Total Estimate

| Sprint | Days | Depends On |
|--------|------|------------|
| S0 — Access Layer | 3 | — |
| S1 — Evidence Layer | 5 | S0 |
| S2 — Numeric Route | 3 | S1 |
| S3 — Simple Route | 4 | S1 |
| S4 — Rule-Heavy Route | 3 | S3 |
| S5 — Dossier Route | 4 | S3, S4 |
| S6 — Calibrators | 4 | S2-S5 |
| S7 — Shadow Mode | 3 | S6 |
| S8 — Canary Live | 2 | S7 + 14 days shadow |
| S9 — GPT-5.4-pro Async | 3 | S8 |
| **Total** | **~34 days** | |

S2 and S3 can run in parallel after S1. S4 and S5 are sequential on S3.

**Critical path**: S0 → S1 → S3 → S4 → S5 → S6 → S7 → [14d shadow] → S8 → S9

**Time to shadow**: ~23 dev days (~5-6 weeks)
**Time to canary live**: +14 days shadow + 2 days = ~8 weeks from start
**Time to full production**: ~10 weeks from start

---

## Infrastructure Prerequisites

Before S0:
- [ ] Postgres 16 + TimescaleDB + pgvector extension installed
- [ ] Redis 7+ running
- [ ] OpenAI Codex OAuth token (ChatGPT Pro — already have working flow)
- [ ] Google CCA OAuth token (AI Ultra — already have working flow)
- [ ] Anthropic OAuth token (Claude Pro/Max — need to discover auth flow)
- [ ] Object storage path configured (local FS for now)

### OAuth Token Sources (Existing)
- **OpenAI**: `~/.codex/auth.json` — ChatGPT Pro, Codex endpoint, device-auth flow
- **Google**: `auth-profiles.json` → `google-gemini-cli:*` profile — CCA endpoint, auto-refresh
- **Anthropic**: TBD — need to reverse-engineer Claude web OAuth or use `claude.ai` session token

---

*Build order is strict. Ship what works, measure everything, ramp slowly.*


# MisterMoney V4 Specification
**Autonomous Cross-Market Intelligence, Research Scheduling, Strategy Composition, and Self-Improving Learning**

Version: 1.0  
Status: Design / Not Yet Implemented  
Owner: MisterMoney Core  
Last updated: 2026-03-07

---

## 1. Executive summary

V4 is **not** “more AI on the same pipeline.”  
V4 is the layer that turns V3’s single-market intelligence into **portfolio-level intelligence**.

- **V1** handles safe execution, inventory, market making, and structural arbitrage.
- **V2** decides where to allocate capital and how to preserve queue value.
- **V3** converts evidence into calibrated per-market signals.
- **V4** builds a **cross-market world model**, decides **what research is worth paying for**, chooses the **correct strategy class** for each market, and learns from realized outcomes.

### Core thesis

V3 can tell us:

> “This market looks stale.”

V4 should tell us:

> “These six markets are jointly stale because event factor X changed, only two of them deserve deeper research, market A should be taken, market B should be market-made, market C should be hedged, and we should update routing policy Y because the last 30 days show model Z is not worth its cost in this regime.”

### The biggest design correction

V4 must remain **evidence-centric and portfolio-aware**, not model-centric.

That means:
- deterministic source checks first,
- evidence graph before model calls,
- event graph before portfolio action,
- value-of-information gating before expensive reasoning,
- route-specific calibration before trading,
- self-evaluation after outcomes.

### Deployment stance

V4 should be implemented as a **modular monolith with worker pools**, not a distributed swarm or early microservice architecture. That keeps operational risk and debugging complexity low while we are still learning.

---

## 2. Why V4 exists

V3 is still mostly **market-local**. It can be very strong at:
- rule parsing,
- evidence extraction,
- ambiguity detection,
- calibrated fair value per market.

But V3 does not naturally solve:
- cross-market consistency,
- research-budget allocation,
- portfolio-level scenario propagation,
- dynamic strategy selection,
- route policy learning from outcomes.

Polymarket’s current market-making surface makes those gaps material:
- market discovery and metadata live across Gamma, CLOB, WebSocket, and Data APIs;
- liquidity rewards favor passive, balanced quoting near midpoint;
- maker rebates are market-specific and require correct fee handling on fee-enabled markets;
- the market channel pushes orderbook, trade, and custom events like `best_bid_ask`, `new_market`, and `market_resolved`;
- written rules, clarifications, and the UMA dispute flow determine payout, not the title alone.  
See references [R1]-[R9].

---

## 3. Goals and non-goals

## 3.1 Goals

V4 must:

1. Build and maintain a **cross-market event/entity graph**.
2. Compute **scenario shocks** that propagate across related markets.
3. Estimate **value of information (VOI)** for possible research actions.
4. Decide which model/tool path to run **only when the expected value justifies cost and latency**.
5. Compose the correct **strategy class** per market:
   - market-make
   - take
   - arbitrage
   - hedge
   - abstain
6. Learn from realized outcomes and continuously improve:
   - model routing,
   - calibration,
   - source priors,
   - strategy-selection policy,
   - research-budget policy.
7. Publish **bounded, auditable signals** into V2, never direct uncapped order instructions.

## 3.2 Non-goals

V4 must **not**:
- place orders directly,
- bypass V1/V2 hard risk controls,
- rely on unrestricted live web browsing in the hot path,
- depend on raw chain-of-thought text,
- assume all markets need frontier-model reasoning,
- replace deterministic numeric models where direct source checks are stronger,
- become a microservice maze before we have production evidence that it is worth the cost.

---

## 4. Dependencies and readiness gates

V4 should not be treated as the next mandatory sprint unless the following are already true:

### 4.1 Required upstream components
- V1 execution loop is stable.
- V2 allocator and queue persistence are functional in paper or canary mode.
- V3 evidence layer exists:
  - source registry,
  - normalized evidence items,
  - rule graph,
  - market-local calibrated signals.
- Full recorder exists for:
  - market WS events,
  - user WS events,
  - order lifecycle,
  - fills,
  - markouts,
  - quote aging,
  - route decisions.
- Route-level eval harness exists.

### 4.2 V4 implementation gate
Do not activate live V4 control unless:
- 30+ days of V2/V3 replay or shadow logs exist,
- route-specific calibration is live,
- signal contribution can be attributed to realized PnL,
- per-route model cost and latency are measured.

---

## 5. Current external assumptions and fixes

This spec intentionally bakes in several design fixes.

## 5.1 Polymarket assumptions

V4 should continue to treat Polymarket as:
- a **CLOB-first venue** with market discovery via Gamma and trading via CLOB / WebSocket / Data API,
- a venue where **liquidity rewards** reward passive, balanced quoting near midpoint,
- a venue where **maker rebates** are market-specific on fee-enabled markets,
- a venue where **rule text, clarifications, and UMA dispute mechanics** matter materially for resolution.  
See references [R1]-[R9].

### Practical implications
- V4 must preserve compatibility with official Polymarket clients and fee handling.
- V4 must use rule and clarification intelligence as first-class inputs.
- V4 must reason about clusters of related markets, not just one market at a time.

## 5.2 Model-stack assumptions

This spec uses the **current public API names and behaviors** from official vendor docs.

### Anthropic
- `claude-sonnet-4-6` is the fast production model for triage / extraction / cheap research.
- `claude-opus-4-6` is the deep specialist for adversarial rule analysis and high-value complex review.
- Sonnet 4.6 supports adaptive thinking and extended thinking; Opus 4.6 is the deepest Claude model and supports hybrid reasoning.  
See references [R10]-[R14].

### Google
- Current public Gemini 3 Pro API model name is `gemini-3-pro-preview`.
- Gemini 3 Pro supports:
  - 1M input context,
  - structured outputs,
  - function calling,
  - search grounding,
  - caching,
  - thinking,
  - URL context.
- Tool/function use requires correct handling of **thought signatures**.  
See references [R15]-[R18].

> **Naming note:** If earlier internal docs or conversation used “Gemini 3.1 Pro,” production code should map to the currently exposed public model ID at deploy time rather than hard-coding stale labels.

### OpenAI
- `gpt-5.4` is the default online judge/orchestrator.
- `gpt-5.4-pro` is Responses-API-first, slower, and should be treated as **async-only** for high-value adjudication and offline labeling.
- GPT-5.4 supports tool use, structured outputs, and a 1.05M context window.
- GPT-5.4-pro supports higher reasoning effort but may take much longer on hard requests.  
See references [R19]-[R21].

## 5.3 Architecture fixes that are mandatory
1. **Native vendor APIs only** for production integrations.
2. **No consumer/OAuth assumptions** in the trading stack.
3. **No raw reasoning traces** in business logic.
4. **Centralized retrieval** owned by MisterMoney.
5. **Route-specific calibration**, not a global one-size-fits-all calibrator.
6. **Async-only use of expensive slow models**.
7. **Evidence bundles** passed to models, not raw internet chaos.

---

## 6. V4 high-level architecture

```text
Polymarket Gamma / CLOB / WS / Data API
Official source checkers
News + docs + PDFs + filings
V1/V2/V3 telemetry
            │
            ▼
      1. Intake & Normalization
            │
            ▼
      2. Evidence Graph
            │
            ▼
      3. Event / Entity / Market Graph
            │
            ▼
      4. Scenario Engine
            │
   ┌────────┼──────────┬──────────────┐
   ▼        ▼          ▼              ▼
  VOI   Research     Strategy      Factor / Risk
Scheduler Router     Composer        Mapper
   │        │          │              │
   │        ▼          ▼              │
   │   Model Adapters  Candidate      │
   │   Sonnet / Opus   actions        │
   │   Gemini / GPT                     │
   └──────────────┬───────────────────┘
                  ▼
         5. Calibrator & Uncertainty
                  ▼
          6. Signal Publisher to V2
                  ▼
                V2/V1
                  ▼
      7. Outcome Attribution & Learning
```

---

## 7. Service topology

V4 should be deployed from one repository as four process types:

```text
services/
  v4_api             # FastAPI control plane, operator endpoints, status
  v4_hot_worker      # fast routes, source checks, VOI loop, strategy compose
  v4_deep_worker     # Opus/Gemini/GPT-5.4-pro async jobs
  v4_offline_worker  # replay, evals, relabeling, policy updates
```

### 7.1 Infra choices
- **Language:** Python 3.12+
- **Framework:** FastAPI + Pydantic v2 + asyncio
- **Queue / bus:** Redis Streams
- **Durable DB:** PostgreSQL + TimescaleDB
- **Vector retrieval:** pgvector in PostgreSQL
- **Object store:** S3-compatible bucket
- **Cache / locks:** Redis
- **Containerization:** Docker + K8s or Nomad
- **Metrics:** Prometheus
- **Logs:** structured JSON to Loki / ELK
- **Tracing:** OpenTelemetry

### 7.2 Why not Kafka / Temporal / separate vector DB yet
We should not pay distributed-systems complexity tax before the trading edge is proven. The expected bottleneck for the next phase is research quality and route calibration, not message throughput.

---

## 8. Core runtime domains

V4 adds four new runtime domains beyond V3:

1. **Graph domain**
2. **Research economics domain**
3. **Strategy composition domain**
4. **Self-improving learning domain**

Each of these is a first-class subsystem.

---

## 9. Domain 1: Graph system

## 9.1 Purpose

The graph system makes the bot think in **world states** instead of isolated market prices.

It maintains:
- entities,
- events,
- sources,
- markets,
- rules,
- evidence,
- relationships,
- scenario factors.

## 9.2 Graph layers

### Evidence graph
Nodes:
- source document
- evidence item
- claim
- rule clause
- clarification
- deterministic source check

Edges:
- supports
- contradicts
- clarifies
- supersedes
- extracted_from
- linked_to_condition

### Event graph
Nodes:
- real-world event
- latent scenario factor
- official source
- market condition
- market cluster

Edges:
- drives
- resolves_via
- depends_on
- correlates_with
- hedges
- implied_by
- stale_relative_to

### Market graph
Nodes:
- condition_id
- token_id / side
- event_id
- cluster_id
- strategy family

Edges:
- complement
- neg-risk transform
- same resolution source
- same event family
- common catalyst
- hedge candidate
- duplicate thesis

## 9.3 Key schemas

```python
class EntityNode(BaseModel):
    entity_id: str
    entity_type: Literal["person", "company", "team", "protocol", "macro_series", "court", "asset", "country"]
    canonical_name: str
    aliases: list[str] = []
    metadata: dict = {}

class EventNode(BaseModel):
    event_id: str
    event_type: Literal["economic_release", "legal_decision", "election", "sports_result", "price_barrier", "policy_action", "custom"]
    title: str
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    source_of_truth: str | None = None
    metadata: dict = {}

class MarketNode(BaseModel):
    condition_id: str
    token_ids: list[str]
    event_ref: str | None = None
    category: str | None = None
    resolution_source: str | None = None
    neg_risk: bool = False
    enable_order_book: bool = True
    metadata: dict = {}

class GraphEdge(BaseModel):
    src_id: str
    dst_id: str
    edge_type: str
    weight: float = 1.0
    ts_created: datetime
    ts_expires: datetime | None = None
    metadata: dict = {}
```

## 9.4 Graph storage
Recommended:
- adjacency tables in Postgres,
- selective denormalized materialized views for hot traversal,
- pgvector embeddings for semantic retrieval across evidence.

### Tables
- `entity_node`
- `event_node`
- `market_node`
- `source_document`
- `evidence_item`
- `claim_node`
- `rule_clause`
- `graph_edge`
- `graph_snapshot`

---

## 10. Domain 2: Scenario engine

## 10.1 Purpose

The scenario engine turns evidence and graph structure into **shared latent factors** that influence multiple markets.

Examples:
- “BTC volatility regime increased.”
- “Fed outcome more hawkish than consensus.”
- “Court likely delays ruling.”
- “Clarification narrows interpretation band.”
- “Source data now inconsistent with stale market cluster.”

## 10.2 Factor model

For each market \(i\):

\[
\ell_i^{(v4)} = \ell_i^{(v3)} + \sum_{k=1}^{K} B_{ik} z_k
\]

Where:
- \(\ell_i = \logit(p_i)\)
- \(z_k\) = latent scenario shock for factor \(k\)
- \(B_{ik}\) = sensitivity/loading of market \(i\) to factor \(k\)

### Inputs to factor inference
- V3 calibrated signals,
- deterministic source check changes,
- evidence polarity shifts,
- market co-movement residuals,
- resolution-source updates,
- clarifications,
- historical factor templates.

### Outputs
- factor shock vector,
- shock confidence,
- affected market set,
- stale-market candidates,
- hedge suggestions.

## 10.3 Factor schema

```python
class ScenarioFactor(BaseModel):
    factor_id: str
    name: str
    factor_type: Literal["macro", "legal", "sports", "crypto", "election", "custom"]
    shock_value: float
    confidence: float
    ts_generated: datetime
    evidence_ids: list[str]
    affected_markets: list[str]
    metadata: dict = {}

class MarketFactorExposure(BaseModel):
    condition_id: str
    factor_id: str
    beta: float
    confidence: float
    ts_updated: datetime
```

## 10.4 Stale-cluster detector

A market becomes a stale-cluster candidate if:

\[
S_i = \left| \ell_i^{(observed)} - \ell_i^{(scenario)} \right| > \tau_i
\]

Where threshold \(\tau_i\) depends on:
- liquidity,
- market toxicity,
- route confidence,
- time to resolution,
- recent realized disagreement.

---

## 11. Domain 3: Value-of-information scheduler

## 11.1 Purpose

This is the key economic controller in V4.

It decides **when** research is worth running and **which** research action is worth paying for.

## 11.2 Candidate research actions

For a market / cluster / event, candidate actions may include:
- deterministic source refresh,
- Sonnet triage,
- Opus rule challenge,
- Gemini dossier synthesis,
- GPT-5.4 judgment refresh,
- GPT-5.4-pro async adjudication,
- human review request.

## 11.3 VOI equation

For research action \(a\):

\[
VOI(a) = \mathbb{E}[\Delta EV(a)] - Cost(a) - LatencyRisk(a) - SlotCost(a)
\]

Where:
- \(\mathbb{E}[\Delta EV(a)]\) = expected improvement in downstream trading EV from running action \(a\),
- \(Cost(a)\) = token/tool/infrastructure cost,
- \(LatencyRisk(a)\) = expected opportunity decay while waiting,
- \(SlotCost(a)\) = budget pressure on provider capacity.

Run action \(a\) **only if**:

\[
VOI(a) > 0
\]

and all hard constraints pass.

## 11.4 Research policy schema

```python
class ResearchAction(BaseModel):
    action_id: str
    action_type: Literal[
        "source_refresh",
        "sonnet_triage",
        "opus_rule_challenge",
        "gemini_dossier",
        "gpt54_judge",
        "gpt54pro_async",
        "human_review"
    ]
    scope_type: Literal["market", "event", "cluster", "portfolio"]
    scope_id: str
    estimated_cost_usd: float
    estimated_latency_ms: int
    expected_ev_delta_bps: float
    voi_bps: float
    reason: str
    priority: int
    metadata: dict = {}
```

## 11.5 Scheduling constraints

Global constraints:
- daily model spend cap,
- per-provider RPM/TPM cap,
- per-route concurrency cap,
- deep-analysis queue size cap,
- research slot cap per event family.

### Provider-aware constraints
The scheduler must account for real vendor traits:
- GPT-5.4-pro can take much longer and is suited to background mode.
- Gemini 3 Pro tool use requires thought-signature preservation.
- Anthropic prompt caching should be exploited for long static prefixes.
See references [R10]-[R21].

---

## 12. Domain 4: Strategy composer

## 12.1 Purpose

V2 decides how to quote once a market is chosen.  
V4 decides **what class of strategy** is appropriate.

For each market \(m\):

\[
a_m \in \{\text{market-make}, \text{take}, \text{arb}, \text{hedge}, \text{abstain}\}
\]

Choose:

\[
a_m^* = \arg\max_a \left(EV_a - Risk_a - Ops_a - CapitalCharge_a\right)
\]

## 12.2 Candidate action families

### Market-make
Use when:
- uncertainty is moderate,
- queue persistence and reward capture are attractive,
- no immediate taker edge dominates.

### Take
Use when:
- strong evidence shock,
- high confidence,
- low time-to-edge decay,
- spread crossing remains positive after costs.

### Arbitrage
Use when:
- parity / neg-risk / conversion / cross-market mismatch is detected.

### Hedge
Use when:
- portfolio factor exposures exceed limits,
- another market offers a better hedge than quote skew reduction.

### Abstain
Use when:
- uncertainty is high,
- resolution risk dominates,
- evidence is insufficient,
- model disagreement is too large,
- opportunity cost is better elsewhere.

## 12.3 Strategy scoring

For each strategy \(a\):

\[
Score_a = EV_a - \lambda_1 \cdot Var_a - \lambda_2 \cdot Tail_a - \lambda_3 \cdot Ops_a - \lambda_4 \cdot Cap_a
\]

Publish the highest positive score above a hurdle. Otherwise publish `abstain`.

## 12.4 Action recommendation schema

```python
class StrategyRecommendation(BaseModel):
    recommendation_id: str
    condition_id: str
    strategy_type: Literal["market_make", "take", "arb", "hedge", "abstain"]
    confidence: float
    expected_edge_cents: float
    max_skew_cents: float
    max_position_delta_usdc: float
    hedge_targets: list[str] = []
    evidence_ids: list[str]
    factor_ids: list[str]
    rationale_summary: str
    ts_generated: datetime
    ttl_seconds: int
```

---

## 13. Model roles in V4

## 13.1 Sonnet 4.6 — hot-path scout
Use for:
- evidence extraction,
- rule-to-schema extraction,
- market/event triage,
- cheap contradiction checks,
- research escalation proposals.

Do **not** treat Sonnet as the final judge by default.

## 13.2 Opus 4.6 — rule lawyer / adversarial challenger
Use for:
- rule-heavy markets,
- clarification impacts,
- disagreement adjudication on rule interpretation,
- near-resolution review,
- high-value ambiguity challenges.

Run only when VOI is positive.

## 13.3 Gemini 3 Pro Preview — long-context dossier engine
Use for:
- large evidence bundles,
- long PDFs / filings / docs,
- contradiction synthesis,
- relationship extraction for graph building,
- multi-document scenario construction.

Important:
- preserve tool/function thought signatures,
- use native Google SDK adapter,
- treat as deep worker only when needed.

## 13.4 GPT-5.4 — online portfolio-aware judge
Use for:
- market-aware decision pass,
- action-family recommendation,
- portfolio-aware resolution of blind estimates vs market state,
- bounded signal generation.

## 13.5 GPT-5.4-pro — async adjudicator / labeler
Use only for:
- high-notional async review,
- post-mortems,
- weekly relabeling,
- difficult route disputes,
- policy update generation.

Never block quote-critical publication on GPT-5.4-pro.

---

## 14. Retrieval and tool policy

## 14.1 Retrieval principle

**MisterMoney owns retrieval.**  
Models consume normalized evidence bundles and narrow internal tools.

Do not let every model independently browse the live web in the hot path.

## 14.2 Internal tool surface

Expose only controlled internal tools:

- `get_rule_graph(condition_id)`
- `get_market_context(condition_id)`
- `get_factor_context(factor_ids)`
- `search_evidence(scope_id, query, filters)`
- `fetch_doc_excerpt(doc_id, span)`
- `get_source_check(condition_id)`
- `get_cluster_state(cluster_id)`
- `get_recent_signal_history(condition_id)`

## 14.3 Tool design rule

A given route should only receive the tools it actually needs.  
This improves:
- determinism,
- cacheability,
- cost control,
- safety,
- debugging.

---

## 15. Signal pipeline

## 15.1 Signal stages

### Stage A: blind estimate
Generated without current market price where possible.

Output:
- `p_blind`
- interval,
- rule clarity,
- evidence IDs,
- counterevidence IDs,
- uncertainty reasons.

### Stage B: market-aware decision
Generated after adding:
- current midpoint,
- spread,
- depth,
- volatility,
- toxicity,
- our inventory and portfolio context.

Output:
- action family,
- `p_market_aware`,
- edge after costs,
- max skew,
- stale-market flag.

### Stage C: portfolio-aware overlay
Adds:
- scenario factors,
- cluster exposure,
- hedge availability,
- research-budget state,
- route confidence.

Output:
- final V4 signal to V2.

## 15.2 Final signal schema

```python
class V4Signal(BaseModel):
    signal_id: str
    condition_id: str
    route_type: Literal["numeric", "simple", "rule", "dossier", "portfolio_overlay"]
    p_calibrated: float
    p_low: float
    p_high: float
    uncertainty: float
    stale_market: bool
    recommended_strategy: Literal["market_make", "take", "arb", "hedge", "abstain"]
    max_skew_cents: float
    max_position_delta_usdc: float
    factor_ids: list[str]
    evidence_ids: list[str]
    counterevidence_ids: list[str]
    models_used: list[str]
    expires_at: datetime
    metadata: dict = {}
```

---

## 16. Calibration and uncertainty

## 16.1 Route-specific calibrators

V4 must not use one global calibrator.

Maintain:
- `numeric_calibrator`
- `simple_calibrator`
- `rule_calibrator`
- `dossier_calibrator`
- `portfolio_overlay_calibrator`

## 16.2 Base calibration

\[
p_{raw} = \sigma(\beta_r^\top x_r)
\]

Where \(r\) is route family.

Then apply route-specific calibration:
- isotonic,
- Platt,
- or beta calibration depending on empirical performance.

## 16.3 Conformal intervals

For route \(r\):

\[
[p_{low}, p_{high}] = \text{ConformalInterval}_r(x)
\]

## 16.4 Signal aging / decay

As signals age:

\[
p_{live} = \lambda(age, freshness, regime) \cdot p_{raw} + (1 - \lambda(age, freshness, regime)) \cdot m_t
\]

Where \(m_t\) is current market prior.

## 16.5 Portfolio uncertainty penalty

\[
u_t = a_0 + a_1 d_{\text{models}} + a_2 a_{\text{rule}} + a_3 s_{\text{source}} + a_4 m_{\text{missing}}
\]

Then strategy sizing is reduced by \(1-u_t\).

---

## 17. Learning loop

## 17.1 Purpose

V4 should continuously answer:
- which routes are predictive,
- which models are worth their cost,
- which evidence types matter,
- which source priors should be raised or lowered,
- which strategy-selection policies improve realized PnL.

## 17.2 Feedback labels

Primary labels:
- realized market resolution,
- realized post-trade markouts,
- realized PnL,
- realized hedge effectiveness,
- realized research ROI,
- route regret.

## 17.3 Route regret

\[
Regret_t = EV(\text{best feasible route policy}) - EV(\text{actual chosen route policy})
\]

## 17.4 Model / route weight update

\[
w_{j,r,g} \propto \exp(-\alpha \cdot Brier_{j,r,g} - \beta \cdot Cost_{j,r,g} - \gamma \cdot Latency_{j,r,g})
\]

Where:
- \(j\) = model/provider,
- \(r\) = route,
- \(g\) = regime.

## 17.5 Strategy policy updates
Track realized performance by:
- market category,
- time-to-resolution bucket,
- liquidity bucket,
- ambiguity bucket,
- factor regime,
- provider route.

Feed this into monthly or weekly policy updates.

---

## 18. Storage design

## 18.1 Core tables

### Evidence / graph
- `source_document`
- `evidence_item`
- `claim_node`
- `rule_clause`
- `clarification_event`
- `entity_node`
- `event_node`
- `market_node`
- `graph_edge`

### Scenario and research
- `scenario_factor`
- `market_factor_exposure`
- `research_action`
- `research_run`
- `voi_estimate`
- `route_plan`

### Signals and actions
- `blind_estimate`
- `market_aware_decision`
- `strategy_recommendation`
- `v4_signal`

### Learning and attribution
- `route_performance_daily`
- `model_cost_ledger`
- `signal_outcome`
- `policy_version`
- `route_regret`

## 18.2 Example tables

```sql
create table scenario_factor (
  factor_id text primary key,
  name text not null,
  factor_type text not null,
  shock_value double precision not null,
  confidence double precision not null,
  ts_generated timestamptz not null,
  evidence_ids jsonb not null default '[]'::jsonb,
  affected_markets jsonb not null default '[]'::jsonb,
  metadata jsonb not null default '{}'::jsonb
);

create table research_run (
  run_id text primary key,
  action_type text not null,
  scope_type text not null,
  scope_id text not null,
  route_type text not null,
  models_used jsonb not null default '[]'::jsonb,
  started_at timestamptz not null,
  completed_at timestamptz,
  latency_ms integer,
  estimated_cost_usd double precision,
  realized_cost_usd double precision,
  status text not null,
  output_ref text,
  metadata jsonb not null default '{}'::jsonb
);

create table v4_signal (
  signal_id text primary key,
  condition_id text not null,
  route_type text not null,
  p_calibrated double precision not null,
  p_low double precision not null,
  p_high double precision not null,
  uncertainty double precision not null,
  stale_market boolean not null,
  recommended_strategy text not null,
  max_skew_cents double precision not null,
  max_position_delta_usdc double precision not null,
  factor_ids jsonb not null default '[]'::jsonb,
  evidence_ids jsonb not null default '[]'::jsonb,
  counterevidence_ids jsonb not null default '[]'::jsonb,
  models_used jsonb not null default '[]'::jsonb,
  generated_at timestamptz not null,
  expires_at timestamptz not null,
  metadata jsonb not null default '{}'::jsonb
);
```

---

## 19. Runtime workflows

## 19.1 New evidence workflow

1. Ingest new document / source update.
2. Normalize to `SourceDocument` + `EvidenceItem`.
3. Link to event graph and candidate markets.
4. Run cheap contradiction / relevance pass.
5. Update scenario factors if needed.
6. Compute candidate VOI actions.
7. Run only positive-VOI research.
8. Publish updated V4 signals to V2.
9. Log attribution hooks.

## 19.2 World-changed / market-stale workflow

Trigger:
- deterministic source crossed threshold,
- factor shock significant,
- peer market moved but this market did not.

Pipeline:
1. stale-cluster detector fires,
2. route planner selects action,
3. GPT-5.4 market-aware decision refresh,
4. strategy composer chooses take / MM / abstain,
5. signal published with short TTL.

## 19.3 Near-resolution workflow

Trigger:
- `hours_to_resolution < threshold`,
- clarification added,
- dispute risk elevated.

Pipeline:
1. rule-heavy route,
2. Opus challenge,
3. GPT-5.4 decision,
4. optional GPT-5.4-pro async review,
5. if ambiguity remains high -> `abstain` or sharply reduced max skew.

## 19.4 Portfolio stress workflow

Trigger:
- factor exposure breach,
- correlated market cascade,
- cluster shock.

Pipeline:
1. factor/risk mapper recomputes exposures,
2. strategy composer requests hedge / abstain / reduced skew,
3. V2 receives tighter capital and skew envelopes,
4. post-event learning records hedge effectiveness.

---

## 20. Latency, TTL, and SLA design

## 20.1 Route SLAs

- **Numeric route:** 250ms – 1s
- **Simple route:** 1s – 3s
- **Rule-heavy route:** cached result immediately, refresh in 5s – 15s
- **Dossier route:** cached result immediately, refresh async in 15s – 120s
- **GPT-5.4-pro route:** async only, never blocks signal publication

## 20.2 Signal TTLs
- Numeric: 60s
- Simple: 15m
- Rule-heavy: 30m
- Dossier: 2h
- Portfolio overlay: 5m default, shorter during active catalysts

## 20.3 Fail-open vs fail-closed
- If deep route misses SLA, publish cached or neutral.
- Never silently swap a critical deep route for a cheaper incompatible route without marking reduced confidence.

---

## 21. Provider adapter architecture

```text
providers/
  base.py
  anthropic_adapter.py
  gemini_adapter.py
  openai_adapter.py
  schemas.py
  retry.py
  costing.py
  cache_keys.py
```

## 21.1 Base adapter contract

```python
class ProviderAdapter(Protocol):
    async def run_structured(
        self,
        model: str,
        system_prompt: str,
        user_payload: dict,
        response_schema: type[BaseModel],
        tools: list[dict] | None = None,
        cache_key: str | None = None,
        timeout_s: float = 30.0,
        metadata: dict | None = None,
    ) -> BaseModel: ...
```

## 21.2 Adapter rules
- Native SDK/API only.
- Structured outputs where supported.
- Stable prompt versioning.
- Provider-specific state kept opaque.
- Cost and latency metering per request.
- Automatic retries only for idempotent safe operations.
- No uncontrolled tool exposure.

---

## 22. Cost engineering

## 22.1 Principle

V4 must optimize **net research value**, not just intelligence quality.

## 22.2 Budget hierarchy
- global daily spend cap,
- per-provider daily cap,
- per-route daily cap,
- per-event cap,
- emergency spend reserve for exceptional market events.

## 22.3 Caching
Use provider caching aggressively for long static prefixes:
- rules,
- clarifications,
- canonical market metadata,
- evidence bundle prefix,
- tool definitions,
- schema instructions.

This is particularly important for Anthropic prompt caching and Gemini caching, and OpenAI cached input on GPT-5.4.  
See references [R12], [R16], [R19].

## 22.4 Model economics defaults
These are starting policies, not hardcoded truths:
- Sonnet: cheap scout
- GPT-5.4: default online judge
- Opus: selective high-value rule pass
- Gemini 3 Pro: selective long-context pass
- GPT-5.4-pro: async only

---

## 23. Security, secrets, and compliance

## 23.1 Secret handling
- KMS-backed secrets only
- per-provider API keys
- no client-side key exposure
- least-privilege service accounts

## 23.2 Data governance
- object-store encryption at rest,
- DB encryption at rest,
- network egress allowlists for provider endpoints,
- audit logs on all provider calls,
- request/response sampling with redaction.

## 23.3 Prompt-injection policy
Because V4 will consume untrusted documents:
- sanitize external documents,
- strip hidden markup where possible,
- isolate retrieval tools from execution tools,
- never let untrusted web content change system prompt or tool availability,
- maintain source-level trust priors.

---

## 24. Observability

## 24.1 Metrics

### Provider metrics
- request count
- latency p50/p95/p99
- cost per request
- token usage in/out
- cache hit rate
- retry count
- provider error rate

### Route metrics
- route invocation count
- route latency
- confidence distribution
- disagreement rate
- stale-market precision / recall
- calibration error by route

### Portfolio metrics
- signal contribution to PnL
- strategy-family contribution
- hedge effectiveness
- factor exposure drift
- research ROI
- route regret
- abstention quality

## 24.2 Logging
Every published signal must be replayable:
- evidence IDs,
- factor IDs,
- route plan,
- model IDs,
- prompt version,
- policy version,
- uncertainty,
- recommended strategy,
- downstream action reference.

## 24.3 Tracing
One trace should connect:
- intake event,
- evidence normalization,
- research actions,
- model calls,
- calibration,
- publication,
- downstream V2 action,
- realized outcome.

---

## 25. Rollout plan

## 25.1 Phase 0 — offline-only skeleton
Build:
- evidence graph extension,
- event graph,
- scenario factor tables,
- VOI scheduler skeleton,
- strategy composer stub.

No live publication.

## 25.2 Phase 1 — shadow graph and factor overlays
Run:
- graph building,
- scenario factors,
- stale-cluster detection,
- route selection in shadow only.

Compare against V3-only output.

## 25.3 Phase 2 — shadow recommendations
Publish strategy recommendations internally only.
Measure:
- take-vs-make regret,
- hedge quality,
- abstain quality,
- research ROI.

## 25.4 Phase 3 — bounded live influence
Allow V4 to influence:
- max skew,
- strategy family,
- hedge suggestions,
- abstention.

Hard caps:
- tiny size only,
- V2 retains final authority,
- V1 retains all hard risk.

## 25.5 Phase 4 — portfolio authority
Allow V4 portfolio overlay to:
- adjust cluster budgets,
- trigger hedge families,
- throttle research spend dynamically,
- update route preferences within guardrails.

---

## 26. Acceptance criteria

V4 is not “done” until these are true:

### Research economics
- positive measured research ROI over rolling 30 days,
- deep model spend justified by realized EV lift,
- async adjudication improves decisions in high-value cases.

### Signal quality
- lower Brier score than V3-only baseline on affected routes,
- improved stale-market detection precision,
- improved cross-market consistency.

### Trading impact
- improved realized PnL attribution after V4 influence,
- lower cluster-regret,
- better hedge effectiveness,
- fewer bad trades caused by local-only reasoning.

### Ops quality
- no runaway spend,
- no quote-path blocking on async providers,
- no silent incompatible route downgrade,
- full traceability from evidence to action.

---

## 27. Suggested repository layout

```text
mistermoney/
  v4/
    config.py
    settings.py

    api/
      app.py
      routes.py
      admin.py

    intake/
      gamma_sync.py
      clob_ws.py
      rules_sync.py
      clarifications.py
      source_registry.py
      source_checkers/

    graph/
      evidence_graph.py
      event_graph.py
      market_graph.py
      graph_builder.py
      graph_queries.py

    scenario/
      factor_store.py
      factor_inference.py
      stale_cluster.py
      exposure_map.py

    research/
      voi.py
      route_planner.py
      scheduler.py
      budgets.py

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
      portfolio_overlay.py

    strategy/
      composer.py
      hedger.py
      abstain.py
      policy.py

    calibration/
      route_models.py
      conformal.py
      decay.py

    serving/
      publisher.py
      contracts.py
      cache.py

    learning/
      attribution.py
      route_performance.py
      policy_updates.py
      relabel.py
      regret.py

    storage/
      postgres.py
      redis.py
      object_store.py

    tests/
      unit/
      integration/
      replay/
      chaos/
```

---

## 28. Pseudocode

## 28.1 Main hot-path loop

```python
async def v4_hot_loop(event):
    norm = normalize_event(event)
    evidence_updates = await evidence_graph.ingest(norm)
    graph_updates = await graph_builder.apply(evidence_updates)

    factor_updates = await scenario_engine.update(graph_updates)
    affected = affected_market_selector(graph_updates, factor_updates)

    research_candidates = []
    for scope in affected:
        research_candidates.extend(
            voi_scheduler.propose_actions(scope)
        )

    approved_actions = [
        a for a in research_candidates
        if a.voi_bps > 0 and budget_manager.allow(a)
    ]

    results = await route_runner.execute_fast_actions(approved_actions)

    draft_signals = signal_builder.combine(
        evidence=evidence_updates,
        factors=factor_updates,
        research=results,
    )

    final_signals = calibrator.apply(draft_signals)
    recommendations = strategy_composer.compose(final_signals)

    await publisher.publish(recommendations)
    await attribution_logger.log(event, final_signals, recommendations)
```

## 28.2 Deep-worker loop

```python
async def v4_deep_worker(job):
    bundle = await retrieval.build_bundle(job.scope_id, job.route_type)

    if job.route_type == "rule_heavy":
        blind = await anthropic.run_structured(
            model="claude-opus-4-6",
            system_prompt=RULE_LAWYER_PROMPT,
            user_payload=bundle,
            response_schema=BlindEstimate,
        )
    elif job.route_type == "dossier":
        dossier = await gemini.run_structured(
            model="gemini-3-pro-preview",
            system_prompt=DOSSIER_PROMPT,
            user_payload=bundle,
            response_schema=BlindEstimate,
        )
        blind = await anthropic.run_structured(
            model="claude-opus-4-6",
            system_prompt=CHALLENGER_PROMPT,
            user_payload={"bundle": bundle, "peer_estimate": dossier.model_dump()},
            response_schema=BlindEstimate,
        )
    else:
        raise ValueError("unsupported route")

    final = await openai.run_structured(
        model="gpt-5.4",
        system_prompt=PORTFOLIO_JUDGE_PROMPT,
        user_payload={
            "blind_estimate": blind.model_dump(),
            "market_context": await market_context(job.scope_id),
            "portfolio_context": await portfolio_context(job.scope_id),
        },
        response_schema=MarketAwareDecision,
    )

    await storage.save_deep_result(job, blind, final)
```

## 28.3 Strategy composition

```python
def choose_strategy(signal, portfolio_state, market_state):
    candidates = enumerate_candidate_strategies(signal, portfolio_state, market_state)

    best = None
    best_score = float("-inf")
    for cand in candidates:
        score = (
            cand.expected_ev
            - LAMBDA_VAR * cand.risk_var
            - LAMBDA_TAIL * cand.tail_risk
            - LAMBDA_OPS * cand.ops_cost
            - LAMBDA_CAP * cand.capital_charge
        )
        if score > best_score:
            best_score = score
            best = cand

    if best is None or best_score <= 0:
        return abstain_recommendation(signal)

    return best.to_recommendation()
```

---

## 29. Open questions

1. How aggressively should factor shocks override market-local V3 signals?
2. When should strategy composer prefer `hedge` over simple size reduction?
3. Should factor exposures be learned purely from realized co-movement or partly curated?
4. When should human review be inserted for especially ambiguous legal/political markets?
5. How should V4 measure the opportunity cost of waiting for async adjudication near catalysts?
6. When do we graduate from Redis Streams to Kafka?

---

## 30. Final stance

V4 is the first architecture layer that is **truly close to full-capacity design**, but only if we respect sequencing.

The right order remains:

1. V1 execution safety
2. V2 allocator + queue persistence
3. V3 evidence-to-signal
4. V4 cross-market intelligence and research economics

If we skip the measurement work and jump straight to V4, it will become architecture theater.  
If we build it on top of V2/V3 telemetry and replay, it becomes the layer that converts a smart bot into a **self-improving trading system**.

---

## 31. References

### Polymarket
- [R1] Polymarket Developer Quickstart / API overview — https://docs.polymarket.com/quickstart/overview
- [R2] Polymarket Market Channel (WebSocket) — https://docs.polymarket.com/market-data/websocket/market-channel
- [R3] Polymarket Liquidity Rewards — https://docs.polymarket.com/developers/market-makers/liquidity-rewards
- [R4] Polymarket Maker Rebates Program (developer docs) — https://docs.polymarket.com/developers/market-makers/maker-rebates-program
- [R5] Polymarket Maker Rebates Program (user-facing updated scope) — https://docs.polymarket.com/polymarket-learn/trading/maker-rebates-program
- [R6] Polymarket How Are Prediction Markets Resolved? — https://docs.polymarket.com/polymarket-learn/markets/how-are-markets-resolved
- [R7] Polymarket How Are Markets Clarified? — https://docs.polymarket.com/polymarket-learn/markets/how-are-markets-clarified
- [R8] Polymarket How Are Markets Disputed? — https://docs.polymarket.com/polymarket-learn/markets/dispute
- [R9] Polymarket UMA integration / clarifications — https://docs.polymarket.com/developers/resolution/UMA

### Anthropic
- [R10] Claude Sonnet 4.6 announcement — https://www.anthropic.com/news/claude-sonnet-4-6
- [R11] Claude Opus 4.6 overview — https://www.anthropic.com/claude/opus
- [R12] Anthropic prompt caching — https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
- [R13] Anthropic citations — https://docs.anthropic.com/en/docs/build-with-claude/citations
- [R14] Anthropic extended thinking — https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking

### Google
- [R15] Gemini models overview — https://ai.google.dev/gemini-api/docs/models/gemini
- [R16] Gemini 3 Developer Guide — https://ai.google.dev/gemini-api/docs/gemini-3
- [R17] Gemini function calling — https://ai.google.dev/gemini-api/docs/function-calling
- [R18] Gemini changelog — https://ai.google.dev/gemini-api/docs/changelog

### OpenAI
- [R19] GPT-5.4 model docs — https://developers.openai.com/api/docs/models/gpt-5.4
- [R20] GPT-5.4-pro model docs — https://developers.openai.com/api/docs/models/gpt-5.4-pro
- [R21] GPT-5 model docs (historical comparison / successor note) — https://developers.openai.com/api/docs/models/gpt-5

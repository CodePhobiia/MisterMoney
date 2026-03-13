# MisterMoney Data Spine Specification

**Status:** Draft v1.0  
**Date:** 2026-03-13  
**Owner:** MisterMoney engineering  
**Purpose:** Formal architecture and engineering specification for the telemetry, replay, analytics, attribution, and continuous-improvement data system required to make MisterMoney a genuinely self-improving trading system.

---

## 1. Executive Summary

MisterMoney currently contains multiple useful but fragmented data paths:

- SQLite persistence for operational facts
- Parquet recording for research / replay artifacts
- fill recorder with 1s / 5s / 30s markouts
- runtime status snapshots
- PMM2 shadow cycle persistence
- ad hoc logs for order lifecycle, suppressions, and ops alerts

This is **not yet sufficient** for continuous strategy improvement at production quality.

The system lacks a single coherent data spine that can answer, with audit-grade confidence:

- what the bot intended to do,
- what it actually attempted,
- what the exchange acknowledged,
- what became live,
- what filled,
- what the market state was at each step,
- what the expected economics were at decision time,
- what the realized economics were afterward,
- which config / code / controller produced the behavior,
- and whether the decision can be replayed exactly.

This specification defines that missing system.

### Core design decision
MisterMoney will adopt an **append-only event-backed data spine** with derived fact tables, replay bundles, and operator-facing analytical models.

### Recommended stack
- **Postgres + TimescaleDB** — durable operational analytics and fact storage
- **Redis Streams** — hot event capture / bus / low-latency coordination
- **Parquet** — cold archival raw events and replay bundles
- **DuckDB** — offline analytical exploration on archived data
- **Grafana / Metabase** — operator and performance dashboards
- **dbt / SQL materializations** — reproducible fact models and diagnostics

### Core principle
> Every meaningful quote, order, fill, suppression, risk adjustment, and PMM decision must become a queryable, replayable, lineage-bearing fact.

---

## 2. Goals

### 2.1 Primary goals
1. Provide a **single source of truth** for market-making telemetry and execution behavior.
2. Enable **exact replay** of historical trading decisions and market states.
3. Support **performance attribution** at order, fill, market, cycle, controller, and rollout-stage levels.
4. Make PMM2 shadow and canary evaluation **auditable and promotion-safe**.
5. Provide enough labeled data to continuously improve:
   - fill models,
   - toxicity / markout models,
   - reward capture logic,
   - suppression policies,
   - rollout criteria.

### 2.2 Secondary goals
1. Improve debugging and incident response.
2. Reduce ambiguity in operator decisions.
3. Enable statistical evaluation of V1 vs PMM2 vs future controllers.
4. Provide a durable foundation for V3 / V4 / future model-driven components.

---

## 3. Non-Goals

This system is **not** intended to:

- replace PMM1 or PMM2 execution logic,
- be the matching engine,
- serve end-user dashboards directly from raw event streams,
- optimize for maximum theoretical throughput at the expense of auditability,
- prematurely optimize around billion-row query benchmarks before the measurement model is correct.

---

## 4. Architectural Principles

### 4.1 Append-only truth first
Truth must be captured as immutable events before it is summarized.

### 4.2 Separate write truth from read models
Operational writes and analytical reads must be separated conceptually and, where appropriate, physically.

### 4.3 Lineage is mandatory
Every decision and fact must carry enough metadata to trace back to:
- config version,
- code version,
- controller,
- model/scorer version,
- runtime stage.

### 4.4 Replayability is a product requirement
If a decision cannot be replayed with its original inputs and context, it is not fully trustworthy.

### 4.5 Derived facts beat raw log scraping
Operator dashboards and performance analysis should be built from structured fact tables, not grep-driven interpretation of logs.

### 4.6 Safer first, faster second
Correctness, auditability, and promotion safety outrank raw analytics performance in early versions.

---

## 5. High-Level Architecture

```text
                ┌─────────────────────────┐
                │   PMM1 / PMM2 Runtime   │
                │  (decision + execution) │
                └────────────┬────────────┘
                             │
                  canonical domain events
                             │
                ┌────────────▼────────────┐
                │     Redis Streams       │
                │   hot event transport   │
                └────────────┬────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         │                   │                   │
         ▼                   ▼                   ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│ Postgres /      │  │ Parquet Archive │  │ Runtime Status  │
│ TimescaleDB     │  │ raw bundles     │  │ / Health Cache  │
│ fact storage    │  │ replay inputs   │  │ Redis / JSON     │
└────────┬────────┘  └────────┬────────┘  └─────────────────┘
         │                    │
         ▼                    ▼
┌─────────────────┐  ┌─────────────────┐
│ Materialized    │  │ Replay / Eval   │
│ fact models     │  │ engines         │
│ attribution     │  │ shadow analysis │
└────────┬────────┘  └─────────────────┘
         │
         ▼
┌────────────────────────────────────────┐
│ Dashboards / Reports / Promotion Gates │
│ Grafana / Metabase / dbt outputs       │
└────────────────────────────────────────┘
```

---

## 6. Storage Roles

## 6.1 Postgres + TimescaleDB
**Role:** durable operational analytics store.

Use for:
- canonical event log
- order/fill/quote/market/cycle facts
- PMM2 shadow/canary diagnostics
- markout / attribution metrics
- materialized views and read models

### Why TimescaleDB
- strong relational semantics
- good time-series ergonomics
- suitable for joins between orders, fills, market state, and controller metadata
- aligned with repository direction already described in V3/V4 docs

## 6.2 Redis Streams
**Role:** hot event transport and short-lived coordination.

Use for:
- event fan-out from live runtime
- async consumers / materializers
- hot state notifications
- optional low-latency queue estimator / alert pipelines

Redis is **not** the source of truth.

## 6.3 Parquet
**Role:** immutable archive and replay-friendly cold storage.

Use for:
- raw event slices
- book snapshots
- replay bundles
- cold analytical exports
- offline model-fitting datasets

## 6.4 DuckDB
**Role:** offline investigation and ad hoc research.

Use for:
- local analysis of Parquet archives
- fast forensic investigation
- model feature / label prototyping

## 6.5 Optional future systems
### ClickHouse / QuestDB
Not recommended as the primary truth store yet.

They become relevant if:
- row volume explodes,
- OLAP dashboards become slow on Timescale,
- wide aggregations dominate workloads,
- Postgres joins remain useful but need analytical offload.

Current recommendation: **defer**.

---

## 7. Canonical Event Model

All important runtime facts must be captured as append-only events.

## 7.1 Event envelope
Every event MUST include:

- `event_id` — UUID
- `event_type` — stable domain event name
- `ts_event` — event-time in UTC
- `ts_ingest` — ingestion-time in UTC
- `controller` — `v1`, `pmm2_shadow`, `pmm2_canary`, `pmm2_production`, etc.
- `strategy` — fine-grained strategy label
- `session_id` — runtime session identifier
- `git_sha` — exact code revision
- `config_hash` — normalized config snapshot hash
- `run_stage` — `shadow`, `canary_5pct`, `production`, etc.
- `condition_id` — when applicable
- `token_id` — when applicable
- `order_id` — when applicable
- `payload_json` — event-specific structured payload

## 7.2 Event families

### Decision events
- `universe_selected`
- `market_selected`
- `market_rejected`
- `quote_intent_created`
- `quote_side_suppressed`
- `risk_scaled_quote`
- `pmm2_shadow_cycle`
- `pmm2_canary_decision`

### Execution events
- `order_submit_requested`
- `order_submit_acknowledged`
- `order_live`
- `order_partially_filled`
- `order_filled`
- `order_cancel_requested`
- `order_canceled`
- `order_rejected`
- `order_replaced`
- `order_expired`

### Market-state events
- `book_snapshot`
- `book_delta`
- `tick_size_changed`
- `market_metadata_refreshed`
- `reward_surface_refreshed`

### Accounting / control events
- `position_reconciled`
- `position_mismatch_detected`
- `nav_snapshot`
- `drawdown_tier_changed`
- `kill_switch_triggered`
- `ops_alert_sent`

---

## 8. Core Durable Fact Tables

## 8.1 `event_log`
Append-only canonical truth log.

### Required columns
- `event_id`
- `ts_event`
- `ts_ingest`
- `event_type`
- `controller`
- `strategy`
- `session_id`
- `git_sha`
- `config_hash`
- `run_stage`
- `condition_id`
- `token_id`
- `order_id`
- `payload_json`

### Purpose
- exact historical trace
- deterministic rebuild input
- forensic audit source

---

## 8.2 `order_fact`
One row per logical order.

### Example columns
- `order_id`
- `client_order_id`
- `controller`
- `strategy`
- `condition_id`
- `token_id`
- `side`
- `submit_requested_at`
- `submit_ack_at`
- `live_at`
- `canceled_at`
- `filled_at`
- `terminal_status`
- `original_size`
- `filled_size`
- `remaining_size`
- `price`
- `post_only`
- `neg_risk`
- `replacement_reason_json`
- `replaced_from_order_id`
- `replace_count`
- `time_live_sec`
- `config_hash`
- `git_sha`

### Purpose
- order lifecycle analytics
- churn analysis
- strategy/controller attribution

---

## 8.3 `fill_fact`
One row per fill.

### Example columns
- `fill_id`
- `order_id`
- `condition_id`
- `token_id`
- `controller`
- `strategy`
- `side`
- `fill_ts`
- `fill_price`
- `fill_size`
- `quote_intent_id`
- `queue_ahead_estimate`
- `fill_prob_estimate`
- `expected_spread_ev_usdc`
- `expected_reward_ev_usdc`
- `expected_rebate_ev_usdc`
- `markout_1s`
- `markout_5s`
- `markout_30s`
- `markout_300s`
- `resolution_markout`
- `realized_spread_capture`
- `adverse_selection_estimate`
- `reward_eligible`
- `scoring_flag`
- `config_hash`
- `git_sha`

### Purpose
- realized performance attribution
- fill quality / toxicity model training

---

## 8.4 `quote_fact`
One row per quote intent per side.

### Example columns
- `quote_intent_id`
- `cycle_id`
- `controller`
- `strategy`
- `condition_id`
- `token_id`
- `side`
- `intended_price`
- `intended_size`
- `submitted_price`
- `submitted_size`
- `suppression_reason_json`
- `risk_adjustment_reason_json`
- `book_quality_json`
- `expected_spread_ev_usdc`
- `expected_reward_ev_usdc`
- `expected_rebate_ev_usdc`
- `expected_total_ev_usdc`
- `fill_prob_30s`
- `became_live`
- `filled_any`
- `filled_full`
- `terminal_status`

### Purpose
- explain every quote attempt
- compare intended vs executed behavior

---

## 8.5 `book_snapshot_fact`
Periodic book state for replay and analytics.

### Example columns
- `token_id`
- `condition_id`
- `ts`
- `best_bid`
- `best_ask`
- `mid`
- `spread`
- `spread_cents`
- `depth_best_bid`
- `depth_best_ask`
- `depth_within_1c`
- `depth_within_2c`
- `depth_within_5c`
- `is_stale`

### Purpose
- market quality diagnostics
- replay reconstruction
- fill model features

---

## 8.6 `market_cycle_fact`
One row per market per cycle.

### Example columns
- `cycle_id`
- `controller`
- `condition_id`
- `event_id`
- `theme`
- `selected`
- `rejected`
- `suppressed`
- `reason_json`
- `universe_score`
- `reward_eligible`
- `reward_capture_ok`
- `liquidity`
- `spread_cents`
- `hours_to_resolution`
- `ambiguity_score`
- `inventory`
- `fair_value`
- `book_state_ref`

### Purpose
- why markets were or were not traded
- suppression / selection diagnostics

---

## 8.7 `shadow_cycle_fact`
One row per PMM2 shadow evaluation cycle.

### Example columns
- `cycle_num`
- `ts`
- `ready_for_live`
- `window_cycles`
- `ev_sample_count`
- `reward_sample_count`
- `churn_sample_count`
- `v1_market_count`
- `pmm2_market_count`
- `market_overlap_pct`
- `overlap_quote_distance_bps`
- `v1_total_ev_usdc`
- `pmm2_total_ev_usdc`
- `ev_delta_usdc`
- `v1_reward_market_count`
- `pmm2_reward_market_count`
- `reward_market_delta`
- `v1_reward_ev_usdc`
- `pmm2_reward_ev_usdc`
- `reward_ev_delta_usdc`
- `v1_cancel_rate_per_order_min`
- `pmm2_cancel_rate_per_order_min`
- `churn_delta_per_order_min`
- `gate_blockers_json`
- `gate_diagnostics_json`
- `v1_state_json`
- `pmm2_plan_json`

### Purpose
- promotion gating
- shadow auditability
- PMM2 improvement analysis

---

## 8.8 `canary_cycle_fact`
One row per PMM2 live canary cycle.

### Example columns
- `cycle_num`
- `ts`
- `canary_stage`
- `live_capital_pct`
- `controlled_market_count`
- `controlled_order_count`
- `realized_fills`
- `realized_cancels`
- `realized_markout_1s`
- `realized_markout_5s`
- `reward_capture_count`
- `rollback_flag`
- `promotion_eligible`
- `incident_flag`
- `summary_json`

### Purpose
- canary evaluation
- promotion / rollback decisions

---

## 8.9 `config_snapshot`
Frozen config history.

### Columns
- `config_hash`
- `created_at`
- `git_sha`
- `config_json`

### Purpose
- exact reproducibility

---

## 8.10 `model_snapshot`
Controller / scorer / model version history.

### Columns
- `model_id`
- `component`
- `version`
- `params_json`
- `trained_on_window`
- `created_at`

### Purpose
- model lineage
- experiment reproducibility

---

## 9. Real-Time State Layer

Hot operational state should live in Redis / runtime caches, not be re-derived from analytics queries during the live loop.

### Store in Redis / hot state
- latest books
- latest order states
- active alerts
- latest queue estimates
- latest market sanity snapshots
- latest PMM2 controller status

### Do not treat Redis as truth
Redis is a serving/cache layer only.

---

## 10. Replay and Evaluation Layer

## 10.1 Replay bundle contents
Every replayable bundle should include:
- event slice from `event_log`
- referenced config snapshot
- referenced model/scorer snapshots
- relevant book snapshots
- relevant market metadata snapshots
- controller stage metadata

## 10.2 Replay capabilities
The replay engine must support:
- cycle-range replay
- market-subset replay
- controller re-evaluation under new scorer versions
- shadow-vs-live comparative evaluation
- markout / toxicity re-labeling

## 10.3 Determinism requirements
Replay must be deterministic enough to reproduce:
- same controller decision inputs
- same feature vector state
- same suppressions and risk adjustments

If non-determinism remains, it must be documented explicitly in the replay metadata.

---

## 11. Performance Attribution Framework

## 11.1 Required attribution categories
Every realized performance result should be decomposable into:
- spread capture
- adverse selection
- reward EV
- rebate EV
- inventory carry / burden
- churn cost
- stale-book suppression cost
- missed opportunity due to safety filters

## 11.2 Markout horizons
Minimum required:
- 1s
- 5s
- 30s

Recommended extension:
- 300s
- resolution-time markout for binary markets

## 11.3 Controller comparison modes
Support comparisons between:
- V1 actual vs PMM2 shadow
- V1 actual vs PMM2 canary
- PMM2 canary stage vs previous canary stage
- same-market paired comparisons where possible

---

## 12. Continuous Improvement Loop

## 12.1 Nightly / scheduled analysis jobs
Produce:
- top churn markets
- worst adverse-selection markets
- stale-book suppression rate by market/theme
- reward market underperformance report
- fill-probability calibration error report
- PMM2 shadow-vs-live disagreement report

## 12.2 Model / policy update inputs
The data spine should support fitting or recalibrating:
- fill hazard model
- toxicity / markout weights
- reward capture heuristics
- suppression thresholds
- PMM2 launch gate thresholds

## 12.3 Promotion policy
Promotion decisions must require:
- minimum sample counts
- no critical incidents
- acceptable churn
- acceptable reconciliation behavior
- no materially worse realized quality vs baseline

---

## 13. Dashboards and Reporting

## 13.1 Operator dashboard
Should show:
- service health
- reconciliation state
- active alerts
- current NAV
- active orders / tracked orders
- quote churn rate
- no-active-quotes condition
- websocket freshness

## 13.2 Strategy dashboard
Should show:
- fills by market/theme/controller
- markouts by horizon
- reward capture
- suppressions by reason
- quote survival / fill rates
- inventory burden

## 13.3 PMM2 dashboard
Should show:
- shadow cycles
- canary cycles
- overlap with V1
- EV delta
- reward EV delta
- churn delta
- gate blockers
- promotion recommendation state

---

## 14. Security and Integrity Requirements

1. Event log is append-only.
2. All config snapshots are hash-addressable.
3. All controller decisions reference config and code revision.
4. Replay bundles are immutable once written.
5. Production analytics writes are idempotent.
6. Event consumers must tolerate duplicate delivery.
7. Sensitive secrets must never be copied into analytics payloads.
8. Order / account identifiers exposed in operator UIs should be minimized or redacted where appropriate.

---

## 15. Bespoke Tooling Requirements

## 15.1 `mm-health`
Single-command health report.

Must report:
- runtime status
- alert state
- websocket freshness
- active quotes / orders
- reconciler stats
- PMM2 status

## 15.2 `mm-replay`
Replay tool for event ranges / sessions.

## 15.3 `mm-attribution`
Compute fill / markout / reward / churn attribution over a chosen window.

## 15.4 `mm-promotion-report`
Produce a canary/shadow promotion report using current gates and minimum sample thresholds.

---

## 16. Recommended Implementation Phases

## Phase A — Truth spine MVP
- introduce `event_log`
- emit canonical events from PMM1 and PMM2
- add `config_snapshot`
- attach `git_sha`, `config_hash`, `controller`

## Phase B — Fact tables
- materialize `order_fact`, `fill_fact`, `quote_fact`, `market_cycle_fact`, `shadow_cycle_fact`

## Phase C — Dashboard layer
- operator dashboard
- strategy dashboard
- PMM2 dashboard

## Phase D — Replay layer
- replay bundles
- deterministic cycle replay
- scorer comparison tooling

## Phase E — Continuous improvement layer
- nightly calibration jobs
- attribution reports
- promotion report generation

---

## 17. Practical Recommendation

MisterMoney should build this system now as a **MisterMoney Performance Spine**, not as “more logging.”

### Recommended concrete stack
- **Postgres + TimescaleDB** — primary durable analytics store
- **Redis Streams** — live event capture and hot event bus
- **Parquet** — cold archive + replay bundles
- **DuckDB** — offline investigations and model-fitting exploration
- **Grafana / Metabase** — dashboards
- **dbt / SQL materializations** — reproducible fact models

### Minimum required durable tables
- `event_log`
- `order_fact`
- `fill_fact`
- `quote_fact`
- `book_snapshot_fact`
- `market_cycle_fact`
- `shadow_cycle_fact`
- `canary_cycle_fact`
- `config_snapshot`
- `model_snapshot`

### Final principle
> If a quote, suppression, order, fill, or controller decision cannot be traced, joined, attributed, and replayed, it is not good enough for continuous performance improvement.

---

## 18. Final Verdict

This data spine is **necessary** if MisterMoney is expected to:
- improve continuously,
- justify PMM2 promotion decisions,
- diagnose live behavior honestly,
- and become more than a partially instrumented bot.

Without it, future strategy iteration will continue to be slower, noisier, and more ambiguous than it needs to be.

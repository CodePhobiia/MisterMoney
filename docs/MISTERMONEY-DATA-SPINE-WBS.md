# MisterMoney Data Spine - Engineering Work Breakdown

*Generated 2026-03-13 from `MISTERMONEY-DATA-SPINE-SPEC.md` and the current repo state*

---

## Objective

Build the data spine as a production-grade measurement layer without stopping V1, PMM2, or V3 development. The plan should:

- preserve existing runtime behavior,
- introduce append-only truth before analytics refactors,
- reuse the repo's current storage and replay pieces where they already exist,
- and move the system from fragmented telemetry to replayable, lineage-bearing facts.

---

## Current Repo Fit

The repo already has several pieces we should build on instead of replacing:

- `pmm1/main.py`
  - PMM1 is the only packaged live runtime entry point, and PMM2 is currently wired in-process rather than deployed as a separate service.
- `pmm1/storage/database.py` + `pmm1/storage/schema.sql`
  - Async SQLite already stores `fill_record`, `book_snapshot`, `market_score`, `queue_state`, `allocation_decision`, `reward_actual`, `rebate_actual`, `scoring_history`, and `shadow_cycle`.
- `pmm1/storage/postgres.py`
  - A Postgres wrapper already exists for `orders`, `fills`, `positions`, `pnl_ledger`, `bot_events`, `market_metadata`, and `daily_nav`.
- `pmm1/storage/redis.py`
  - Redis is already used as hot state for books, orders, features, positions, NAV, and kill-switch state.
- `pmm1/backtest/recorder.py` + `pmm1/storage/parquet.py`
  - JSONL and Parquet recording already capture `book_snapshot`, `book_delta`, `trade`, `quote_intent`, `features`, `orders`, and `fills`.
- `pmm1/backtest/replay.py`
  - A replay engine already exists for recorded JSONL sessions.
- `pmm1/recorder/fill_recorder.py` + `pmm1/recorder/book_recorder.py`
  - Fill markouts and periodic book snapshots already exist.
- `pmm2/shadow/logger.py`
  - PMM2 shadow cycles are already logged to JSONL and persisted to SQLite `shadow_cycle`.
- `pmm2/config.py`, `pmm2/runtime/loops.py`, and `pmm2/runtime/v1_bridge.py`
  - Controller labels and rollout stages for `pmm2_shadow`, `pmm2_canary`, and live stages already exist.
- `v3/evidence/db.py`, `v3/evidence/migrations/001_initial.sql`, and `v3/serving/publisher.py`
  - V3 already uses Postgres, optional TimescaleDB, and Redis publication patterns we can reuse.
- `pmm1/tools/healthcheck.py` + `pmm1/ops.py`
  - A health report foundation already exists.

What is still missing:

- no canonical `event_log`,
- no shared event envelope with `git_sha`, `config_hash`, and `session_id`,
- no `config_snapshot` or `model_snapshot`,
- no Redis Streams implementation,
- no derived fact-table pipeline from a single source of truth,
- no immutable replay bundle format tied to config/model lineage,
- no unified `mm-health`, `mm-replay`, `mm-attribution`, or `mm-promotion-report`,
- no dashboard layer fed by stable read models,
- no end-to-end dual-run migration from current SQLite/JSONL telemetry to spine-backed facts.

Two repo-specific realities should shape the rollout plan:

- the **live PMM1 path currently initializes SQLite**, while the Postgres and Redis backends exist but are not yet the live write path;
- the **PMM2 live canary path appears miswired** today because live PMM2 initialization expects `bot_state.order_manager`, but PMM1 creates `order_manager` as a local runtime object rather than attaching it to `state` before PMM2 init.

That means the data spine rollout should be treated as an additive migration on top of the current in-process PMM1 + PMM2 runtime, not as a separate service launch.

---

## Delivery Principles

1. **Additive first.** Do not rip out SQLite, JSONL, Parquet, or existing PMM2/V3 persistence during the first pass.
2. **Event log before fact tables.** Fact tables must be derived from the canonical event stream, not maintained as another set of ad hoc writes.
3. **Dual-write, then cut over.** During migration, continue existing writes while adding spine writes and parity checks.
4. **Hot loop stays hot.** Redis remains the live serving/cache layer; analytics storage must not sit on the critical quote path.
5. **No silent loss.** Event emission, stream transport, and materialization failures must be explicit and observable.
6. **Replay must be earned.** Do not claim deterministic replay until config, code revision, and model references are recorded with events.

---

## Preflight Blockers

Before the spine rollout can safely claim canary-grade truth, fix these runtime mismatches:

1. **PMM2 live init wiring**
   - Ensure the live PMM2 initializer can actually access `order_manager` on the object passed into `maybe_init_pmm2(...)`.
   - Until this is fixed, treat `canary_cycle_fact` work as schema-ready but runtime-incomplete.
2. **Recorder output path mismatch**
   - PMM1 currently constructs `LiveRecorder()` and a separate `ParquetWriter(...)`, but the configured Parquet writer is not injected into the recorder.
   - Fix this before relying on replay-bundle paths for cold archive discovery.
3. **Explicit live-storage decision**
   - Decide whether early spine writes happen directly from the PMM1 process to Postgres, through Redis Streams, or through a dual-write bridge.
   - The current live path is SQLite-first, so this cannot be left implicit.

These are small but important because the spine is supposed to improve auditability, not layer new uncertainty on top of already-fragile rollout code.

---

## Recommended Architecture Adjustment

Use the existing Postgres direction already present in V3 as the spine's durable home, while keeping current SQLite and Parquet producers during transition.

Recommended write path:

1. Runtime emits canonical domain events.
2. Events are written to `event_log` in Postgres.
3. The same events are optionally published to Redis Streams for async materializers and fast fan-out.
4. Existing JSONL / Parquet recording remains in place and becomes replay archive input rather than the primary truth source.
5. Fact tables are materialized from `event_log`, not hand-maintained by business logic.

This keeps the system aligned with the spec without forcing an immediate rewrite of PMM1, PMM2, or V3 storage internals.

---

## Dependency Graph

```text
Phase 0 - Spine contract and storage decisions
    |
    v
Phase 1 - Canonical event emission + lineage capture
    |
    v
Phase 2 - Durable event store + backfill
    |
    +--> Phase 3 - Fact materializers
    |         |
    |         v
    |    Phase 5 - Dashboards and operator tooling
    |
    +--> Phase 4 - Replay bundles and replay CLI
              |
              v
Phase 6 - Attribution, nightly jobs, and promotion reports
    |
    v
Phase 7 - Cutover and deprecation of fragmented truth paths
```

Critical path:

`Phase 0 -> Phase 1 -> Phase 2 -> Phase 3 -> Phase 4 -> Phase 6 -> Phase 7`

---

## Phase 0 - Spine Contract and Storage Decisions

**Goal:** lock the event contract, lineage fields, ownership boundaries, and migration approach before touching runtime code.

### Tasks

- Finalize the canonical event envelope from the spec:
  - `event_id`, `event_type`, `ts_event`, `ts_ingest`, `controller`, `strategy`, `session_id`, `git_sha`, `config_hash`, `run_stage`, `condition_id`, `token_id`, `order_id`, `payload_json`
- Decide the durable database target:
  - preferred: same Postgres deployment pattern already used by V3, with a dedicated spine schema or table namespace
- Decide the source of `git_sha`, `config_hash`, and `session_id`:
  - `session_id` can build on the same session concept already used in `pmm1/backtest/recorder.py`
  - `config_hash` should come from normalized loaded config, not raw file bytes
- Define idempotency keys and duplicate-handling rules for event consumers.
- Define which existing tables remain temporary write targets during migration:
  - keep SQLite `fill_record`, `book_snapshot`, and `shadow_cycle` during dual-run
  - keep Parquet/JSONL recording during replay migration

### Deliverables

- Final event envelope doc section accepted.
- SQL DDL for `event_log`, `config_snapshot`, and `model_snapshot`.
- A short migration note describing which current writes stay in place temporarily.

### Validation

- Unit tests for config hashing and session-id generation.
- SQL migration test against a local Postgres instance.
- Review checklist confirming the live loop does not depend on analytics queries.

**Estimate:** 2-3 days

---

## Phase 1 - Canonical Event Emission and Lineage Capture

**Goal:** emit append-only events from the existing runtime surfaces without changing bot behavior.

### PMM1 emission surfaces to instrument

- order lifecycle
  - `pmm1/execution/order_manager.py`
  - `pmm1/state/orders.py`
  - user WS handling in `pmm1/ws/user_ws.py`
- market-state changes
  - `pmm1/ws/market_ws.py`
  - `pmm1/state/books.py`
  - `pmm1/recorder/book_recorder.py`
  - `pmm1/backtest/recorder.py`
- fills and markouts
  - `pmm1/recorder/fill_recorder.py`
  - `pmm1/storage/database.py`
- control/accounting events
  - `pmm1/execution/reconciler.py`
  - `pmm1/risk/drawdown.py`
  - `pmm1/risk/kill_switch.py`
  - `pmm1/ops.py`

### PMM2 emission surfaces to instrument

- market / cycle decisions
  - `pmm2/runtime/loops.py`
  - `pmm2/allocator/*`
  - `pmm2/planner/*`
- shadow and canary state
  - `pmm2/shadow/logger.py`
  - `pmm2/runtime/v1_bridge.py`
  - `pmm2/config.py`

### V3 surfaces to link, not necessarily fully migrate in phase 1

- fair-value publication
  - `v3/serving/publisher.py`
- model metadata
  - provider / route version info from `v3/providers/*`, `v3/routes/*`, and calibration code

### Tasks

- Introduce a shared event-construction helper under existing storage/runtime code.
- Emit canonical events for:
  - quote intent creation and suppression,
  - order submit / ack / live / cancel / fill / reject / expire,
  - book snapshots / deltas / tick-size changes,
  - NAV snapshots, reconciliation mismatches, drawdown tier changes, kill-switch triggers,
  - PMM2 shadow-cycle summaries and canary-stage control decisions.
- Attach `git_sha`, `config_hash`, `controller`, and `run_stage` everywhere.
- Record `config_snapshot` on startup or config reload.
- Start `model_snapshot` writes for V3 signal publications and any PMM2 scorer versions that are explicitly versioned.

### Deliverables

- Runtime can emit the full event envelope without changing trade decisions.
- `config_snapshot` begins accumulating durable snapshots.
- `model_snapshot` starts with at least V3 route/model publications and PMM2 controller labels.

### Validation

- Unit tests for event payload builders.
- Integration tests around order lifecycle emission.
- Updated tests for PMM2 shadow/canary telemetry to assert controller/stage lineage is present.

**Estimate:** 4-6 days

---

## Phase 2 - Durable Event Store and Backfill

**Goal:** stand up `event_log` as the durable truth store and backfill enough existing telemetry to make it immediately useful.

### Tasks

- Extend the existing Postgres layer instead of creating a second unrelated DB abstraction.
- Create:
  - `event_log`
  - `config_snapshot`
  - `model_snapshot`
- Add a write path that is safe for duplicates and retries.
- Add Redis Streams helpers to `pmm1/storage/redis.py` for fan-out, but do not make Streams the only durable path on day one.
- Backfill the following into the new shape where practical:
  - JSONL recordings from `pmm1/backtest/recorder.py`
  - SQLite `fill_record`
  - SQLite `book_snapshot`
  - SQLite `shadow_cycle`
  - existing Postgres `orders`, `fills`, and `bot_events`

### Migration stance

- Existing tables remain as temporary compatibility sources.
- `event_log` becomes the new source of truth for all new materializers.
- Backfill does not need to be perfect for every historical field; it must be explicit about missing lineage where it cannot be reconstructed.

### Deliverables

- Postgres event spine is live.
- Backfill jobs exist for the highest-value historical telemetry.
- Redis Streams exists as a transport layer for downstream consumers.

### Validation

- Reconciliation queries comparing:
  - `fill_record` vs `event_log` fill counts
  - `book_snapshot` vs `event_log` market-state counts
  - `shadow_cycle` vs `event_log` shadow-cycle counts
- Duplicate-delivery tests for stream consumers.
- Load test for bursty event emission around active order updates.

**Estimate:** 4-6 days

---

## Phase 3 - Fact Materializers

**Goal:** derive the spec's core fact tables from `event_log` so operator analytics no longer depend on direct log scraping or mixed truth sources.

### First fact tables to build

1. `order_fact`
2. `fill_fact`
3. `quote_fact`
4. `book_snapshot_fact`
5. `market_cycle_fact`
6. `shadow_cycle_fact`
7. `canary_cycle_fact`

### Tasks

- Build materializers or SQL jobs that consume only `event_log` plus snapshot tables.
- Map existing sources into the new facts:
  - order lifecycle events -> `order_fact`
  - fill + markout events -> `fill_fact`
  - quote intent / suppression / risk adjustment events -> `quote_fact`
  - book snapshot and delta events -> `book_snapshot_fact`
  - PMM2 allocator and shadow decisions -> `market_cycle_fact` and `shadow_cycle_fact`
  - PMM2 canary live summaries -> `canary_cycle_fact`
- Preserve compatibility with current PMM2 calibration jobs until fact-table parity is proven.

### Recommended sequencing

- Start with `order_fact`, `fill_fact`, and `shadow_cycle_fact`.
- Then build `quote_fact` and `market_cycle_fact`.
- Build `canary_cycle_fact` once the canary write path is confirmed complete.
- Keep `book_snapshot_fact` minimal at first; add richer depth features once the base model is stable.

### Deliverables

- Stable derived facts for the most important operator and research questions.
- A reproducible materialization path that can be rerun from `event_log`.

### Validation

- Fact-table parity tests against current SQLite / Postgres data.
- Regression tests on order lifecycle aggregation.
- PMM2 shadow readiness metrics reproduced from `shadow_cycle_fact` rather than direct SQLite reads.

**Estimate:** 5-7 days

---

## Phase 4 - Replay Bundles and Replay Tooling

**Goal:** convert the current recorder/replay setup into a lineage-aware replay system tied to the spine.

### Existing base to reuse

- `pmm1/backtest/recorder.py`
- `pmm1/storage/parquet.py`
- `pmm1/backtest/replay.py`

### Tasks

- Define a replay bundle manifest that references:
  - event range or session range from `event_log`
  - required `config_snapshot`
  - required `model_snapshot`
  - supporting Parquet slices for book / feature archives
  - controller stage metadata
- Update replay tooling so bundle selection is driven by the spine instead of directory scanning alone.
- Add a CLI entrypoint for `mm-replay`.
- Support:
  - session replay,
  - cycle-range replay,
  - market-subset replay,
  - scorer comparison on historical inputs.

### Deliverables

- Immutable replay bundles.
- Replay manifest format committed in repo.
- `mm-replay` wrapper around the upgraded replay flow.

### Validation

- Deterministic replay tests on a recorded session.
- Replay-vs-live comparison tests for PMM2 shadow cycles.
- Metadata completeness checks: each bundle must fail validation if config/model lineage is missing.

**Estimate:** 4-6 days

---

## Phase 5 - Dashboards and Operator Tooling

**Goal:** surface stable read models for operators without building dashboards directly on raw events.

### Existing base to reuse

- `pmm1/tools/healthcheck.py`
- `pmm1/ops.py`
- `pmm2/shadow/dashboard.py`
- `tools/dashboard.py`

### Tasks

- Build a unified `mm-health` command on top of existing healthcheck logic plus PMM2/V3 status.
- Add read models for:
  - operator health,
  - strategy performance,
  - PMM2 shadow / canary promotion state.
- Use SQL read models first; add Grafana / Metabase only after the views are stable.
- Keep dashboards focused on the facts defined in the spec:
  - fill quality,
  - markouts,
  - reward capture,
  - suppressions,
  - churn,
  - gate blockers.

### Deliverables

- `mm-health`
- first operator and PMM2 promotion views
- stable query layer for later dashboards

### Validation

- Snapshot tests for health output.
- SQL validation queries against known fixtures.
- Operator sanity check: dashboard numbers should match the same-window CLI reports.

**Estimate:** 4-5 days

---

## Phase 6 - Attribution Jobs and Promotion Reports

**Goal:** close the loop so the spine supports nightly analysis, recalibration, and promotion decisions.

### Existing base to reuse

- `pmm2/calibration/nightly.py`
- `pmm2/calibration/fill_calibrator.py`
- `pmm2/calibration/toxicity_fitter.py`
- `pmm2/calibration/attribution.py`
- `v3/calibration/*`

### Tasks

- Repoint calibration and attribution jobs to fact tables rather than direct SQLite tables where feasible.
- Produce scheduled reports for:
  - top churn markets
  - worst adverse-selection markets
  - stale-book suppression rate
  - reward market underperformance
  - fill-probability calibration error
  - PMM2 shadow-vs-live disagreement
- Add `mm-attribution` CLI.
- Add `mm-promotion-report` CLI.
- Encode promotion thresholds against `shadow_cycle_fact` and `canary_cycle_fact`.

### Deliverables

- Scheduled attribution outputs.
- Promotion report generation from spine facts.
- Clear fact-table inputs for future PMM2 and V3 model recalibration.

### Validation

- Golden-data tests for attribution math.
- End-to-end report tests using fixture windows.
- Promotion report tests covering pass, soft-fail, and hard-block cases.

**Estimate:** 4-6 days

---

## Phase 7 - Cutover and De-fragmentation

**Goal:** stop treating SQLite and JSONL side channels as long-term truth once spine parity is proven.

### Tasks

- Make `event_log` + fact tables the documented analytics source of truth.
- Reduce direct reads from:
  - SQLite `fill_record`
  - SQLite `book_snapshot`
  - SQLite `shadow_cycle`
  - ad hoc JSONL scans
- Keep Parquet as cold archive and replay input.
- Decide which legacy tables remain as local capture aids and which should be deprecated.
- Update docs and runbooks to point operators at spine-backed tooling.

### Deliverables

- Clear ownership boundaries.
- Legacy telemetry paths marked as compatibility-only or retired.
- Updated runbook and release-readiness checks.

### Validation

- One full dual-run period where:
  - existing reports and spine-backed reports match within tolerance,
  - no event-loss alerts fire,
  - replay bundles validate,
  - promotion reports are computed from spine facts only.

**Estimate:** 3-4 days plus observation window

---

## Suggested First PR Sequence

If we want the safest start, the first three PRs should be:

1. **Spine contract PR**
   - DDL for `event_log`, `config_snapshot`, `model_snapshot`
   - shared event envelope types
   - config-hash and git-sha helpers
2. **PMM1 runtime emission PR**
   - order lifecycle, fill, book, and control events
   - no fact tables yet
3. **PMM2 shadow/canary emission PR**
   - shadow-cycle and canary summary events
   - parity checks against current SQLite `shadow_cycle`

That gets append-only truth flowing before any analytics-layer complexity.

---

## Risks and Mitigations

### Risk: dual-write drift between old tables and the new event log
- Mitigation: parity reports and temporary reconciliation queries on every deployment.

### Risk: event emission adds latency to the hot loop
- Mitigation: async buffering plus Redis Streams fan-out; no analytics query in the quote path.

### Risk: missing lineage on historical backfill
- Mitigation: mark backfilled records explicitly as lineage-incomplete instead of inventing `git_sha` or `config_hash`.

### Risk: too much scope lands before replay is trustworthy
- Mitigation: do not claim replay completeness until bundle validation passes on real recorded sessions.

### Risk: dashboards arrive before facts stabilize
- Mitigation: build read models first; dashboards are phase 5, not phase 2.

---

## Timeline Summary

| Phase | Name | Estimate |
|------|------|----------|
| 0 | Spine contract and storage decisions | 2-3 days |
| 1 | Canonical event emission and lineage capture | 4-6 days |
| 2 | Durable event store and backfill | 4-6 days |
| 3 | Fact materializers | 5-7 days |
| 4 | Replay bundles and replay tooling | 4-6 days |
| 5 | Dashboards and operator tooling | 4-5 days |
| 6 | Attribution jobs and promotion reports | 4-6 days |
| 7 | Cutover and de-fragmentation | 3-4 days + observation |

**Expected implementation time:** ~30-43 engineering days  
**Recommended observation window before full cutover:** 10-14 days of dual-run parity

---

## Final Recommendation

Do **not** treat the data spine as a side logging project. Build it as the shared measurement substrate for PMM1, PMM2, and V3.

The right order is:

1. append-only truth,
2. lineage capture,
3. derived facts,
4. replay bundles,
5. attribution and promotion tooling,
6. cutover away from fragmented truth paths.

If we hold that sequence, the repo can evolve from "partially instrumented trading system" to "auditable, replayable, continuously improving trading system" without throwing away the storage and replay work already done.

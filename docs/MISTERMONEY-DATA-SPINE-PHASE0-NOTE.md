# MisterMoney Data Spine Phase 0 Migration Note

**Date:** 2026-03-13

Phase 0 establishes the data spine contract and durable schema without changing trading behavior.

## Decisions

- `event_log`, `config_snapshot`, and `model_snapshot` live in Postgres.
- TimescaleDB is optional and follows the same best-effort enablement pattern already used by V3.
- SQLite remains the active local capture store during migration.
- Redis remains hot state and transport, not durable truth.
- JSONL and Parquet remain replay/archive inputs during the cutover period.

## Lineage Rules

- `config_hash` is computed from a deterministic, redacted config snapshot.
- secrets such as API keys, passphrases, and private keys are not written into config snapshots.
- `git_sha` resolves from the local git checkout and falls back to `unknown` when unavailable.
- `session_id` uses the same timestamp format already used by the live recorder.

## Migration Boundary

- Existing SQLite tables such as `fill_record`, `book_snapshot`, and `shadow_cycle` remain in place.
- Existing JSONL / Parquet recording remains in place.
- No PMM1, PMM2, or V3 runtime path should be switched to spine-backed reads until Phase 2 and Phase 3 parity checks pass.

## Next Step

Phase 1 should start emitting canonical events from the current PMM1 and PMM2 runtime surfaces into the new Postgres spine while keeping legacy writes active for comparison.

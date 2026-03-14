"""Phase 0 data spine contract, lineage helpers, and durable schema."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import structlog
from pydantic import BaseModel, Field, field_validator

SPINE_SCHEMA_SQL = """
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'TimescaleDB not available, continuing without hypertables';
END;
$$;

CREATE TABLE IF NOT EXISTS event_log (
    event_id TEXT PRIMARY KEY,
    ts_event TIMESTAMPTZ NOT NULL,
    ts_ingest TIMESTAMPTZ NOT NULL,
    event_type TEXT NOT NULL,
    controller TEXT NOT NULL,
    strategy TEXT NOT NULL,
    session_id TEXT NOT NULL,
    git_sha TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    run_stage TEXT NOT NULL,
    condition_id TEXT,
    token_id TEXT,
    order_id TEXT,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_event_log_ts_event ON event_log(ts_event DESC);
CREATE INDEX IF NOT EXISTS idx_event_log_ts_ingest ON event_log(ts_ingest DESC);
CREATE INDEX IF NOT EXISTS idx_event_log_type ON event_log(event_type);
CREATE INDEX IF NOT EXISTS idx_event_log_controller ON event_log(controller);
CREATE INDEX IF NOT EXISTS idx_event_log_session ON event_log(session_id);
CREATE INDEX IF NOT EXISTS idx_event_log_condition ON event_log(condition_id);
CREATE INDEX IF NOT EXISTS idx_event_log_order ON event_log(order_id);

DO $$
BEGIN
    PERFORM create_hypertable(
        'event_log',
        'ts_ingest',
        chunk_time_interval => INTERVAL '1 day',
        if_not_exists => TRUE
    );
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'TimescaleDB not available, using regular table for event_log';
END;
$$;

CREATE TABLE IF NOT EXISTS config_snapshot (
    config_hash TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    git_sha TEXT NOT NULL,
    config_json JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_config_snapshot_created_at ON config_snapshot(created_at DESC);

CREATE TABLE IF NOT EXISTS model_snapshot (
    model_id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    version TEXT NOT NULL,
    params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    trained_on_window JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_model_snapshot_component ON model_snapshot(component);
CREATE INDEX IF NOT EXISTS idx_model_snapshot_created_at ON model_snapshot(created_at DESC);

CREATE TABLE IF NOT EXISTS order_fact (
    order_id TEXT PRIMARY KEY,
    client_order_id TEXT DEFAULT '',
    controller TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    condition_id TEXT DEFAULT '',
    token_id TEXT DEFAULT '',
    side TEXT DEFAULT '',
    submit_requested_at TIMESTAMPTZ,
    submit_ack_at TIMESTAMPTZ,
    live_at TIMESTAMPTZ,
    canceled_at TIMESTAMPTZ,
    filled_at TIMESTAMPTZ,
    terminal_status TEXT DEFAULT '',
    original_size DOUBLE PRECISION DEFAULT 0,
    filled_size DOUBLE PRECISION DEFAULT 0,
    remaining_size DOUBLE PRECISION DEFAULT 0,
    price DOUBLE PRECISION DEFAULT 0,
    post_only BOOLEAN DEFAULT TRUE,
    neg_risk BOOLEAN DEFAULT FALSE,
    order_type TEXT DEFAULT '',
    replacement_reason_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    replaced_from_order_id TEXT,
    replace_count INTEGER NOT NULL DEFAULT 0,
    time_live_sec DOUBLE PRECISION,
    config_hash TEXT NOT NULL DEFAULT '',
    git_sha TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    run_stage TEXT NOT NULL DEFAULT '',
    last_event_id TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_order_fact_condition ON order_fact(condition_id);
CREATE INDEX IF NOT EXISTS idx_order_fact_controller ON order_fact(controller);
CREATE INDEX IF NOT EXISTS idx_order_fact_terminal_status ON order_fact(terminal_status);
CREATE INDEX IF NOT EXISTS idx_order_fact_updated_at ON order_fact(updated_at DESC);

CREATE TABLE IF NOT EXISTS order_fact_applied_event (
    event_id TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS quote_fact (
    quote_intent_id TEXT PRIMARY KEY,
    cycle_id TEXT DEFAULT '',
    controller TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    condition_id TEXT DEFAULT '',
    token_id TEXT DEFAULT '',
    order_id TEXT DEFAULT '',
    side TEXT DEFAULT '',
    intended_price DOUBLE PRECISION,
    intended_size DOUBLE PRECISION,
    submitted_price DOUBLE PRECISION,
    submitted_size DOUBLE PRECISION,
    suppression_reason_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    risk_adjustment_reason_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    book_quality_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    expected_spread_ev_usdc DOUBLE PRECISION,
    expected_reward_ev_usdc DOUBLE PRECISION,
    expected_rebate_ev_usdc DOUBLE PRECISION,
    expected_total_ev_usdc DOUBLE PRECISION,
    fill_prob_30s DOUBLE PRECISION,
    became_live BOOLEAN DEFAULT FALSE,
    filled_any BOOLEAN DEFAULT FALSE,
    filled_full BOOLEAN DEFAULT FALSE,
    terminal_status TEXT DEFAULT '',
    config_hash TEXT NOT NULL DEFAULT '',
    git_sha TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    run_stage TEXT NOT NULL DEFAULT '',
    last_event_id TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_quote_fact_condition ON quote_fact(condition_id);
CREATE INDEX IF NOT EXISTS idx_quote_fact_token ON quote_fact(token_id);
CREATE INDEX IF NOT EXISTS idx_quote_fact_order ON quote_fact(order_id);
CREATE INDEX IF NOT EXISTS idx_quote_fact_terminal_status ON quote_fact(terminal_status);
CREATE INDEX IF NOT EXISTS idx_quote_fact_updated_at ON quote_fact(updated_at DESC);

CREATE TABLE IF NOT EXISTS quote_fact_applied_event (
    event_id TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fill_fact (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT DEFAULT '',
    condition_id TEXT DEFAULT '',
    token_id TEXT DEFAULT '',
    controller TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    side TEXT DEFAULT '',
    fill_ts TIMESTAMPTZ NOT NULL,
    fill_price DOUBLE PRECISION DEFAULT 0,
    fill_size DOUBLE PRECISION DEFAULT 0,
    quote_intent_id TEXT DEFAULT '',
    queue_ahead_estimate DOUBLE PRECISION,
    fill_prob_estimate DOUBLE PRECISION,
    expected_spread_ev_usdc DOUBLE PRECISION,
    expected_reward_ev_usdc DOUBLE PRECISION,
    expected_rebate_ev_usdc DOUBLE PRECISION,
    markout_1s DOUBLE PRECISION,
    markout_5s DOUBLE PRECISION,
    markout_30s DOUBLE PRECISION,
    markout_300s DOUBLE PRECISION,
    resolution_markout DOUBLE PRECISION,
    realized_spread_capture DOUBLE PRECISION,
    adverse_selection_estimate DOUBLE PRECISION,
    reward_eligible BOOLEAN DEFAULT FALSE,
    scoring_flag BOOLEAN DEFAULT FALSE,
    fee_usdc DOUBLE PRECISION,
    dollar_value_usdc DOUBLE PRECISION,
    mid_at_fill DOUBLE PRECISION,
    config_hash TEXT NOT NULL DEFAULT '',
    git_sha TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    run_stage TEXT NOT NULL DEFAULT '',
    source_event_id TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fill_fact_order ON fill_fact(order_id);
CREATE INDEX IF NOT EXISTS idx_fill_fact_condition ON fill_fact(condition_id);
CREATE INDEX IF NOT EXISTS idx_fill_fact_fill_ts ON fill_fact(fill_ts DESC);
CREATE INDEX IF NOT EXISTS idx_fill_fact_controller ON fill_fact(controller);

CREATE TABLE IF NOT EXISTS book_snapshot_fact (
    snapshot_id TEXT PRIMARY KEY,
    token_id TEXT NOT NULL DEFAULT '',
    condition_id TEXT DEFAULT '',
    ts TIMESTAMPTZ NOT NULL,
    best_bid DOUBLE PRECISION,
    best_ask DOUBLE PRECISION,
    mid DOUBLE PRECISION,
    spread DOUBLE PRECISION,
    spread_cents DOUBLE PRECISION,
    depth_best_bid DOUBLE PRECISION,
    depth_best_ask DOUBLE PRECISION,
    depth_within_1c DOUBLE PRECISION,
    depth_within_2c DOUBLE PRECISION,
    depth_within_5c DOUBLE PRECISION,
    is_stale BOOLEAN DEFAULT FALSE,
    tick_size DOUBLE PRECISION,
    controller TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    config_hash TEXT NOT NULL DEFAULT '',
    git_sha TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    run_stage TEXT NOT NULL DEFAULT '',
    source_event_id TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_book_snapshot_fact_token ON book_snapshot_fact(token_id);
CREATE INDEX IF NOT EXISTS idx_book_snapshot_fact_condition ON book_snapshot_fact(condition_id);
CREATE INDEX IF NOT EXISTS idx_book_snapshot_fact_ts ON book_snapshot_fact(ts DESC);

CREATE TABLE IF NOT EXISTS shadow_cycle_fact (
    source_event_id TEXT PRIMARY KEY,
    cycle_num INTEGER,
    ts TIMESTAMPTZ NOT NULL,
    ready_for_live BOOLEAN DEFAULT FALSE,
    window_cycles INTEGER,
    ev_sample_count INTEGER,
    reward_sample_count INTEGER,
    churn_sample_count INTEGER,
    v1_market_count INTEGER,
    pmm2_market_count INTEGER,
    market_overlap_pct DOUBLE PRECISION,
    overlap_quote_distance_bps DOUBLE PRECISION,
    v1_total_ev_usdc DOUBLE PRECISION,
    pmm2_total_ev_usdc DOUBLE PRECISION,
    ev_delta_usdc DOUBLE PRECISION,
    v1_reward_market_count INTEGER,
    pmm2_reward_market_count INTEGER,
    reward_market_delta DOUBLE PRECISION,
    v1_reward_ev_usdc DOUBLE PRECISION,
    pmm2_reward_ev_usdc DOUBLE PRECISION,
    reward_ev_delta_usdc DOUBLE PRECISION,
    v1_cancel_rate_per_order_min DOUBLE PRECISION,
    pmm2_cancel_rate_per_order_min DOUBLE PRECISION,
    churn_delta_per_order_min DOUBLE PRECISION,
    gate_blockers_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    gate_diagnostics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    v1_state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    pmm2_plan_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    controller TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    config_hash TEXT NOT NULL DEFAULT '',
    git_sha TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    run_stage TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_shadow_cycle_fact_ts ON shadow_cycle_fact(ts DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_cycle_fact_cycle_num ON shadow_cycle_fact(cycle_num);

CREATE TABLE IF NOT EXISTS canary_cycle_fact (
    source_event_id TEXT PRIMARY KEY,
    cycle_num INTEGER,
    ts TIMESTAMPTZ NOT NULL,
    canary_stage TEXT DEFAULT '',
    live_capital_pct DOUBLE PRECISION DEFAULT 0,
    controlled_market_count INTEGER,
    controlled_order_count INTEGER,
    realized_fills INTEGER,
    realized_cancels INTEGER,
    realized_markout_1s DOUBLE PRECISION,
    realized_markout_5s DOUBLE PRECISION,
    reward_capture_count INTEGER,
    rollback_flag BOOLEAN DEFAULT FALSE,
    promotion_eligible BOOLEAN DEFAULT FALSE,
    incident_flag BOOLEAN DEFAULT FALSE,
    summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    controller TEXT NOT NULL DEFAULT '',
    strategy TEXT NOT NULL DEFAULT '',
    config_hash TEXT NOT NULL DEFAULT '',
    git_sha TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    run_stage TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_canary_cycle_fact_ts ON canary_cycle_fact(ts DESC);
CREATE INDEX IF NOT EXISTS idx_canary_cycle_fact_cycle_num ON canary_cycle_fact(cycle_num);
"""


_SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "api_passphrase",
        "private_key",
        "token",
        "secret",
        "password",
        "passphrase",
    }
)

logger = structlog.get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonicalize(value: Any) -> Any:
    """Convert nested values into a deterministic JSON-serializable form."""
    if isinstance(value, BaseModel):
        return _canonicalize(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, tuple):
        return [_canonicalize(item) for item in value]
    if isinstance(value, set):
        return [_canonicalize(item) for item in sorted(value, key=repr)]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def sanitize_config_snapshot(config: Mapping[str, Any]) -> dict[str, Any]:
    """Redact secrets while preserving behavior-relevant config for lineage."""
    sanitized: dict[str, Any] = {}
    for key in sorted(config):
        value = config[key]
        if str(key).lower() in _SENSITIVE_CONFIG_KEYS:
            sanitized[str(key)] = "[REDACTED]"
            continue
        if isinstance(value, Mapping):
            sanitized[str(key)] = sanitize_config_snapshot(value)
            continue
        if isinstance(value, list):
            sanitized[str(key)] = [
                sanitize_config_snapshot(item) if isinstance(item, Mapping) else _canonicalize(item)
                for item in value
            ]
            continue
        sanitized[str(key)] = _canonicalize(value)
    return sanitized


def canonical_config_json(config: Mapping[str, Any]) -> str:
    """Serialize sanitized config deterministically for hashing and storage."""
    sanitized = sanitize_config_snapshot(config)
    return json.dumps(sanitized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_config_hash(config: Mapping[str, Any]) -> str:
    """Compute the stable spine config hash from sanitized config."""
    return hashlib.sha256(canonical_config_json(config).encode("utf-8")).hexdigest()


def resolve_git_sha(repo_root: Path | None = None) -> str:
    """Resolve the current git revision for lineage, or 'unknown' if unavailable."""
    resolved_root = repo_root or Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "-C", str(resolved_root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unknown"

    git_sha = result.stdout.strip()
    return git_sha or "unknown"


def make_session_id(now: datetime | None = None) -> str:
    """Build a stable runtime session identifier aligned with the recorder format."""
    timestamp = (now or _utc_now()).astimezone(UTC)
    return timestamp.strftime("%Y%m%d_%H%M%S")


def deterministic_event_id(namespace: str, key: str) -> str:
    """Build a stable UUID for backfilled/replayed events."""
    return str(uuid5(NAMESPACE_URL, f"{namespace}:{key}"))


class SpineEvent(BaseModel):
    """Canonical append-only event envelope for the data spine."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: str
    ts_event: datetime
    ts_ingest: datetime = Field(default_factory=_utc_now)
    controller: str
    strategy: str
    session_id: str
    git_sha: str
    config_hash: str
    run_stage: str
    condition_id: str | None = None
    token_id: str | None = None
    order_id: str | None = None
    payload_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "event_id",
        "event_type",
        "controller",
        "strategy",
        "session_id",
        "git_sha",
        "config_hash",
        "run_stage",
    )
    @classmethod
    def _require_non_empty_strings(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("spine string fields must be non-empty")
        return value.strip()


class ConfigSnapshotRecord(BaseModel):
    """Frozen redacted config snapshot for replay lineage."""

    config_hash: str
    created_at: datetime = Field(default_factory=_utc_now)
    git_sha: str
    config_json: dict[str, Any]

    @field_validator("config_hash", "git_sha")
    @classmethod
    def _require_snapshot_strings(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("config snapshot fields must be non-empty")
        return value.strip()


class ModelSnapshotRecord(BaseModel):
    """Model / scorer version lineage record."""

    model_id: str
    component: str
    version: str
    params_json: dict[str, Any] = Field(default_factory=dict)
    trained_on_window: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)

    @field_validator("model_id", "component", "version")
    @classmethod
    def _require_model_strings(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("model snapshot fields must be non-empty")
        return value.strip()


def build_config_snapshot(
    config: Mapping[str, Any],
    *,
    git_sha: str | None = None,
    created_at: datetime | None = None,
) -> ConfigSnapshotRecord:
    """Build a redacted config snapshot and stable hash from config data."""
    sanitized = sanitize_config_snapshot(config)
    return ConfigSnapshotRecord(
        config_hash=compute_config_hash(config),
        created_at=created_at or _utc_now(),
        git_sha=git_sha or resolve_git_sha(),
        config_json=sanitized,
    )


class SpineEmitter:
    """Async helper for Phase 1 runtime lineage and event writes."""

    def __init__(
        self,
        store: Any | None,
        *,
        session_id: str,
        git_sha: str,
        config_hash: str,
        default_controller: str = "v1",
        default_run_stage: str = "production",
        stream_store: Any | None = None,
        stream_key: str | None = None,
    ) -> None:
        self._store = store
        self._stream_store = stream_store
        self._stream_key = stream_key
        self.session_id = session_id
        self.git_sha = git_sha
        self.config_hash = config_hash
        self.default_controller = default_controller
        self.default_run_stage = default_run_stage

    @property
    def is_enabled(self) -> bool:
        return self._store is not None or self._stream_store is not None

    async def persist_config_snapshot(
        self, config: Mapping[str, Any],
    ) -> ConfigSnapshotRecord | None:
        """Persist the current redacted config snapshot."""
        if self._store is None:
            return None
        snapshot = build_config_snapshot(config, git_sha=self.git_sha)
        try:
            await self._store.upsert_config_snapshot(snapshot)
            return snapshot
        except Exception as exc:
            logger.error(
                "spine_config_snapshot_failed",
                session_id=self.session_id,
                config_hash=snapshot.config_hash,
                error=str(exc),
            )
            return None

    async def persist_model_snapshot(
        self,
        snapshot: ModelSnapshotRecord | Mapping[str, Any],
    ) -> bool:
        """Persist model/scorer lineage metadata."""
        if self._store is None:
            return False
        payload = (
            snapshot
            if isinstance(snapshot, ModelSnapshotRecord)
            else ModelSnapshotRecord.model_validate(snapshot)
        )
        try:
            await self._store.upsert_model_snapshot(payload)
            return True
        except Exception as exc:
            logger.error(
                "spine_model_snapshot_failed",
                session_id=self.session_id,
                model_id=payload.model_id,
                error=str(exc),
            )
            return False

    async def emit_event(
        self,
        *,
        event_type: str,
        strategy: str,
        ts_event: datetime | None = None,
        controller: str | None = None,
        run_stage: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        order_id: str | None = None,
        payload_json: Mapping[str, Any] | None = None,
    ) -> bool:
        """Write one canonical spine event."""
        if self._store is None and self._stream_store is None:
            return False
        event = SpineEvent(
            event_type=event_type,
            ts_event=ts_event or _utc_now(),
            controller=controller or self.default_controller,
            strategy=strategy,
            session_id=self.session_id,
            git_sha=self.git_sha,
            config_hash=self.config_hash,
            run_stage=run_stage or self.default_run_stage,
            condition_id=condition_id,
            token_id=token_id,
            order_id=order_id,
            payload_json=dict(payload_json or {}),
        )
        durable_ok = False
        try:
            if self._store is not None:
                await self._store.append_spine_event(event)
                durable_ok = True
        except Exception as exc:
            logger.error(
                "spine_event_write_failed",
                session_id=self.session_id,
                event_type=event.event_type,
                controller=event.controller,
                run_stage=event.run_stage,
                order_id=order_id,
                error=str(exc),
            )

        stream_ok = False
        try:
            if self._stream_store is not None:
                await self._stream_store.append_spine_stream_event(
                    event,
                    stream_key=self._stream_key,
                )
                stream_ok = True
        except Exception as exc:
            logger.error(
                "spine_stream_publish_failed",
                session_id=self.session_id,
                event_type=event.event_type,
                controller=event.controller,
                run_stage=event.run_stage,
                order_id=order_id,
                error=str(exc),
            )

        if self._store is None:
            return stream_ok
        return durable_ok

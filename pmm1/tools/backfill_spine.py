"""Backfill existing SQLite and JSONL telemetry into the Postgres data spine."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pmm1.settings import load_settings
from pmm1.storage.postgres import PostgresStore
from pmm1.storage.spine import SpineEvent, deterministic_event_id

BACKFILL_GIT_SHA = "backfill_unknown"
BACKFILL_CONFIG_HASH = "backfill_unknown"
BACKFILL_RUN_STAGE = "backfill_legacy"
BACKFILL_LINEAGE = {
    "lineage_complete": False,
    "git_sha_source": "unknown",
    "config_hash_source": "unknown",
}


def _parse_sqlite_timestamp(raw: Any) -> datetime:
    value = str(raw or "").strip()
    if not value:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return datetime.now(UTC)


def _parse_postgres_timestamp(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        parsed = raw
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return _parse_sqlite_timestamp(raw)


def _parse_recording_timestamp(raw: Any) -> datetime:
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return datetime.now(UTC)
    return datetime.fromtimestamp(ts, tz=UTC)


def _base_backfill_payload(source_type: str, source_ref: str) -> dict[str, Any]:
    return {
        "backfill": {
            "source_type": source_type,
            "source_ref": source_ref,
            **BACKFILL_LINEAGE,
        }
    }


def map_fill_record_to_event(row: dict[str, Any]) -> SpineEvent:
    payload = {
        **_base_backfill_payload("sqlite.fill_record", str(row["id"])),
        "side": row.get("side"),
        "price": row.get("price"),
        "size": row.get("size"),
        "dollar_value": row.get("dollar_value"),
        "fee": row.get("fee"),
        "markout_1s": row.get("markout_1s"),
        "markout_5s": row.get("markout_5s"),
        "markout_30s": row.get("markout_30s"),
        "mid_at_fill": row.get("mid_at_fill"),
        "is_scoring": bool(row.get("is_scoring", 0)),
        "reward_eligible": bool(row.get("reward_eligible", 0)),
    }
    return SpineEvent(
        event_id=deterministic_event_id("sqlite.fill_record", str(row["id"])),
        event_type="order_filled",
        ts_event=_parse_sqlite_timestamp(row.get("ts")),
        controller="v1",
        strategy="mm",
        session_id="backfill_sqlite_fill_record",
        git_sha=BACKFILL_GIT_SHA,
        config_hash=BACKFILL_CONFIG_HASH,
        run_stage=BACKFILL_RUN_STAGE,
        condition_id=row.get("condition_id"),
        token_id=row.get("token_id"),
        order_id=row.get("order_id"),
        payload_json=payload,
    )


def map_book_snapshot_to_event(row: dict[str, Any]) -> SpineEvent:
    payload = {
        **_base_backfill_payload("sqlite.book_snapshot", str(row["id"])),
        "best_bid": row.get("best_bid"),
        "best_ask": row.get("best_ask"),
        "bid_depth_5": row.get("bid_depth_5"),
        "ask_depth_5": row.get("ask_depth_5"),
        "spread_cents": row.get("spread_cents"),
        "mid": row.get("mid"),
    }
    return SpineEvent(
        event_id=deterministic_event_id("sqlite.book_snapshot", str(row["id"])),
        event_type="book_snapshot",
        ts_event=_parse_sqlite_timestamp(row.get("ts")),
        controller="v1",
        strategy="market_data",
        session_id="backfill_sqlite_book_snapshot",
        git_sha=BACKFILL_GIT_SHA,
        config_hash=BACKFILL_CONFIG_HASH,
        run_stage=BACKFILL_RUN_STAGE,
        condition_id=row.get("condition_id"),
        token_id=row.get("token_id"),
        payload_json=payload,
    )


def map_shadow_cycle_to_event(row: dict[str, Any]) -> SpineEvent:
    payload = {
        **_base_backfill_payload("sqlite.shadow_cycle", f"{row.get('cycle_num')}"),
        "cycle_num": row.get("cycle_num"),
        "ready_for_live": bool(row.get("ready_for_live", 0)),
        "window_cycles": row.get("window_cycles"),
        "ev_sample_count": row.get("ev_sample_count"),
        "reward_sample_count": row.get("reward_sample_count"),
        "churn_sample_count": row.get("churn_sample_count"),
        "market_overlap_pct": row.get("market_overlap_pct"),
        "overlap_quote_distance_bps": row.get("overlap_quote_distance_bps"),
        "v1_total_ev_usdc": row.get("v1_total_ev_usdc"),
        "pmm2_total_ev_usdc": row.get("pmm2_total_ev_usdc"),
        "ev_delta_usdc": row.get("ev_delta_usdc"),
        "reward_ev_delta_usdc": row.get("reward_ev_delta_usdc"),
        "churn_delta_per_order_min": row.get("churn_delta_per_order_min"),
        "gate_blockers_json": row.get("gate_blockers_json"),
        "gate_diagnostics_json": row.get("gate_diagnostics_json"),
        "summary_json": row.get("summary_json"),
    }
    return SpineEvent(
        event_id=deterministic_event_id(
            "sqlite.shadow_cycle", f"{row.get('cycle_num')}:{row.get('ts')}"
        ),
        event_type="pmm2_shadow_cycle",
        ts_event=_parse_sqlite_timestamp(row.get("ts")),
        controller="pmm2_shadow",
        strategy="pmm2_shadow",
        session_id="backfill_sqlite_shadow_cycle",
        git_sha=BACKFILL_GIT_SHA,
        config_hash=BACKFILL_CONFIG_HASH,
        run_stage="shadow",
        payload_json=payload,
    )


def map_legacy_order_to_event(row: dict[str, Any]) -> SpineEvent:
    status = str(row.get("status", "") or "").upper()
    event_type = {
        "LIVE": "order_live",
        "FILLED": "order_filled",
        "CONFIRMED": "order_filled",
        "CANCELED": "order_canceled",
        "CANCELLED": "order_canceled",
        "EXPIRED": "order_expired",
        "FAILED": "order_rejected",
    }.get(status, "order_submit_acknowledged")
    ts_event = (
        row.get("filled_at")
        or row.get("canceled_at")
        or row.get("submitted_at")
        or row.get("updated_at")
        or row.get("created_at")
    )
    payload = {
        **_base_backfill_payload("legacy.postgres.orders", str(row["id"])),
        "status": status,
        "price": float(row.get("price", 0) or 0),
        "original_size": float(row.get("original_size", 0) or 0),
        "filled_size": float(row.get("filled_size", 0) or 0),
        "post_only": bool(row.get("post_only", False)),
        "neg_risk": bool(row.get("neg_risk", False)),
        "order_type": row.get("order_type"),
        "expiration": row.get("expiration"),
        "error_msg": row.get("error_msg"),
    }
    return SpineEvent(
        event_id=deterministic_event_id("legacy.postgres.orders", str(row["order_id"])),
        event_type=event_type,
        ts_event=_parse_postgres_timestamp(ts_event),
        controller="v1",
        strategy=str(row.get("strategy") or "mm"),
        session_id="backfill_legacy_orders",
        git_sha=BACKFILL_GIT_SHA,
        config_hash=BACKFILL_CONFIG_HASH,
        run_stage=BACKFILL_RUN_STAGE,
        condition_id=row.get("condition_id"),
        token_id=row.get("token_id"),
        order_id=row.get("order_id"),
        payload_json=payload,
    )


def map_legacy_fill_to_event(row: dict[str, Any]) -> SpineEvent:
    payload = {
        **_base_backfill_payload("legacy.postgres.fills", str(row["id"])),
        "side": row.get("side"),
        "price": float(row.get("price", 0) or 0),
        "size": float(row.get("size", 0) or 0),
        "fee": float(row.get("fee", 0) or 0),
        "transaction_hash": row.get("transaction_hash"),
    }
    return SpineEvent(
        event_id=deterministic_event_id("legacy.postgres.fills", str(row["id"])),
        event_type="order_filled",
        ts_event=_parse_postgres_timestamp(row.get("timestamp")),
        controller="v1",
        strategy="mm",
        session_id="backfill_legacy_fills",
        git_sha=BACKFILL_GIT_SHA,
        config_hash=BACKFILL_CONFIG_HASH,
        run_stage=BACKFILL_RUN_STAGE,
        condition_id=row.get("condition_id"),
        token_id=row.get("token_id"),
        order_id=row.get("order_id"),
        payload_json=payload,
    )


def map_legacy_bot_event(row: dict[str, Any]) -> SpineEvent:
    original_event_type = str(row.get("event_type") or "ops_alert_sent")
    payload = row.get("details")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {"raw_details": payload}
    payload = {
        **_base_backfill_payload("legacy.postgres.bot_events", str(row["id"])),
        "original_event_type": original_event_type,
        "details": payload or {},
    }
    return SpineEvent(
        event_id=deterministic_event_id("legacy.postgres.bot_events", str(row["id"])),
        event_type=original_event_type,
        ts_event=_parse_postgres_timestamp(row.get("timestamp")),
        controller="v1",
        strategy="ops",
        session_id="backfill_legacy_bot_events",
        git_sha=BACKFILL_GIT_SHA,
        config_hash=BACKFILL_CONFIG_HASH,
        run_stage=BACKFILL_RUN_STAGE,
        payload_json=payload,
    )


def map_recording_event(
    *,
    event_type: str,
    data: dict[str, Any],
    session_id: str,
    source_ref: str,
) -> SpineEvent | None:
    normalized = {
        "quote_intent": ("quote_intent_created", "mm"),
        "book_snapshot": ("book_snapshot", "market_data"),
        "book_delta": ("book_delta", "market_data"),
        "tick_size_change": ("tick_size_changed", "market_data"),
    }.get(event_type)

    if normalized is None:
        return None

    target_event_type, strategy = normalized
    payload = {k: v for k, v in data.items() if k not in {"_ts", "_type"}}
    payload.update(_base_backfill_payload("jsonl.recording", source_ref))

    return SpineEvent(
        event_id=deterministic_event_id("jsonl.recording", source_ref),
        event_type=target_event_type,
        ts_event=_parse_recording_timestamp(data.get("_ts")),
        controller="v1",
        strategy=strategy,
        session_id=session_id,
        git_sha=BACKFILL_GIT_SHA,
        config_hash=BACKFILL_CONFIG_HASH,
        run_stage=BACKFILL_RUN_STAGE,
        condition_id=payload.get("condition_id"),
        token_id=payload.get("token_id"),
        order_id=payload.get("order_id"),
        payload_json=payload,
    )


def iter_recording_backfill_events(recording_dir: Path) -> Iterable[SpineEvent]:
    jsonl_root = recording_dir / "jsonl"
    if not jsonl_root.exists():
        return []

    def _iter() -> Iterable[SpineEvent]:
        for event_dir in sorted(jsonl_root.iterdir()):
            if not event_dir.is_dir():
                continue
            original_event_type = event_dir.name
            for date_dir in sorted(event_dir.iterdir()):
                if not date_dir.is_dir():
                    continue
                for jsonl_file in sorted(date_dir.glob("*.jsonl")):
                    session_id = jsonl_file.stem
                    rel_path = jsonl_file.relative_to(recording_dir).as_posix()
                    with jsonl_file.open(encoding="utf-8") as fh:
                        for line_no, line in enumerate(fh, start=1):
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            event = map_recording_event(
                                event_type=data.get("_type", original_event_type),
                                data=data,
                                session_id=session_id,
                                source_ref=f"{rel_path}:{line_no}",
                            )
                            if event is not None:
                                yield event

    return _iter()


async def backfill_sqlite(db_path: Path, store: PostgresStore) -> Counter[str]:
    counts: Counter[str] = Counter()
    if not db_path.exists():
        return counts

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        sqlite_sources = [
            ("fill_record", map_fill_record_to_event),
            ("book_snapshot", map_book_snapshot_to_event),
            ("shadow_cycle", map_shadow_cycle_to_event),
        ]
        for table_name, mapper in sqlite_sources:
            try:
                rows = conn.execute(f"SELECT * FROM {table_name}").fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                event = mapper(dict(row))
                await store.append_spine_event(event)
                counts[event.event_type] += 1
    finally:
        conn.close()
    return counts


async def backfill_recordings(recording_dir: Path, store: PostgresStore) -> Counter[str]:
    counts: Counter[str] = Counter()
    for event in iter_recording_backfill_events(recording_dir):
        await store.append_spine_event(event)
        counts[event.event_type] += 1
    return counts


async def backfill_legacy_postgres(store: PostgresStore) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in await store.get_legacy_orders():
        event = map_legacy_order_to_event(row)
        await store.append_spine_event(event)
        counts[event.event_type] += 1
    for row in await store.get_legacy_fills():
        event = map_legacy_fill_to_event(row)
        await store.append_spine_event(event)
        counts[event.event_type] += 1
    for row in await store.get_legacy_bot_events():
        event = map_legacy_bot_event(row)
        await store.append_spine_event(event)
        counts[event.event_type] += 1
    return counts


async def run_backfill(
    *,
    postgres_dsn: str,
    sqlite_db: Path | None,
    recording_dir: Path | None,
    include_legacy_postgres: bool = True,
) -> dict[str, Any]:
    store = PostgresStore(postgres_dsn)
    await store.connect()
    try:
        summary: dict[str, Any] = {
            "sqlite": {}, "recordings": {},
            "legacy_postgres": {}, "total": 0,
        }
        if sqlite_db is not None:
            sqlite_counts = await backfill_sqlite(sqlite_db, store)
            summary["sqlite"] = dict(sqlite_counts)
            summary["total"] += sum(sqlite_counts.values())
        if recording_dir is not None:
            recording_counts = await backfill_recordings(recording_dir, store)
            summary["recordings"] = dict(recording_counts)
            summary["total"] += sum(recording_counts.values())
        if include_legacy_postgres:
            legacy_counts = await backfill_legacy_postgres(store)
            summary["legacy_postgres"] = dict(legacy_counts)
            summary["total"] += sum(legacy_counts.values())
        summary["event_log_count"] = await store.count_spine_events()
        return summary
    finally:
        await store.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill existing telemetry into the Postgres data spine",
    )
    parser.add_argument(
        "--config",
        default="config/default.yaml",
        help="Path to YAML config for default Postgres DSN",
    )
    parser.add_argument(
        "--postgres-dsn",
        help="Override Postgres DSN instead of reading from config",
    )
    parser.add_argument(
        "--sqlite-db",
        default="data/pmm1.db",
        help="SQLite DB to backfill (default: data/pmm1.db)",
    )
    parser.add_argument(
        "--recording-dir",
        default="data/recordings",
        help="Recording directory to backfill JSONL from (default: data/recordings)",
    )
    parser.add_argument(
        "--skip-sqlite",
        action="store_true",
        help="Skip SQLite backfill",
    )
    parser.add_argument(
        "--skip-recordings",
        action="store_true",
        help="Skip JSONL recording backfill",
    )
    parser.add_argument(
        "--skip-legacy-postgres",
        action="store_true",
        help="Skip backfill from legacy Postgres orders, fills, and bot_events tables",
    )
    return parser


async def amain() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    settings = load_settings(args.config, enforce_runtime_guards=False)
    postgres_dsn = args.postgres_dsn or settings.storage.postgres_dsn
    sqlite_db = None if args.skip_sqlite else Path(args.sqlite_db)
    recording_dir = None if args.skip_recordings else Path(args.recording_dir)

    summary = await run_backfill(
        postgres_dsn=postgres_dsn,
        sqlite_db=sqlite_db,
        recording_dir=recording_dir,
        include_legacy_postgres=not args.skip_legacy_postgres,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()

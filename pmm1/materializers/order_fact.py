"""Redis stream consumer that materializes order_fact from spine events."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import structlog

from pmm1.storage.postgres import PostgresStore
from pmm1.storage.redis import RedisStateStore
from pmm1.storage.spine import SpineEvent

logger = structlog.get_logger(__name__)

ORDER_FACT_CONSUMER_GROUP = "order-fact-materializer"


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class OrderFactMaterializer:
    """Consumes the spine stream and maintains a derived order_fact table."""

    SUPPORTED_EVENTS = {
        "order_submit_requested",
        "order_submit_acknowledged",
        "order_live",
        "order_cancel_requested",
        "order_canceled",
        "order_partially_filled",
        "order_filled",
        "order_rejected",
        "order_expired",
    }

    def __init__(
        self,
        postgres_store: PostgresStore,
        stream_store: RedisStateStore | None,
        *,
        consumer_name: str = "pmm1",
        group_name: str = ORDER_FACT_CONSUMER_GROUP,
        block_ms: int = 1000,
        read_count: int = 100,
    ) -> None:
        self._postgres = postgres_store
        self._stream = stream_store
        self._consumer_name = consumer_name
        self._group_name = group_name
        self._block_ms = block_ms
        self._read_count = read_count
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._pending_requests: dict[str, SpineEvent] = {}

    def _request_signature(self, event: SpineEvent) -> str:
        payload = event.payload_json
        return "|".join(
            [
                event.controller,
                event.strategy,
                event.session_id,
                event.condition_id or "",
                event.token_id or "",
                str(payload.get("side") or ""),
                str(payload.get("price") or ""),
                str(payload.get("size") or ""),
                str(payload.get("order_type") or ""),
                str(payload.get("expiration") or ""),
            ]
        )

    def _parse_stream_fields(self, fields: dict[str, str]) -> SpineEvent:
        payload = fields.get("payload_json", "{}")
        if isinstance(payload, str):
            payload = json.loads(payload)
        return SpineEvent(
            event_id=fields["event_id"],
            event_type=fields["event_type"],
            ts_event=_parse_dt(fields["ts_event"]) or datetime.now(UTC),
            ts_ingest=_parse_dt(fields["ts_ingest"]) or datetime.now(UTC),
            controller=fields["controller"],
            strategy=fields["strategy"],
            session_id=fields["session_id"],
            git_sha=fields["git_sha"],
            config_hash=fields["config_hash"],
            run_stage=fields["run_stage"],
            condition_id=fields.get("condition_id") or None,
            token_id=fields.get("token_id") or None,
            order_id=fields.get("order_id") or None,
            payload_json=payload,
        )

    def _empty_record(self, event: SpineEvent) -> dict[str, Any]:
        return {
            "order_id": event.order_id,
            "client_order_id": "",
            "controller": event.controller,
            "strategy": event.strategy,
            "condition_id": event.condition_id or "",
            "token_id": event.token_id or "",
            "side": "",
            "submit_requested_at": None,
            "submit_ack_at": None,
            "live_at": None,
            "canceled_at": None,
            "filled_at": None,
            "terminal_status": "",
            "original_size": 0.0,
            "filled_size": 0.0,
            "remaining_size": 0.0,
            "price": 0.0,
            "post_only": True,
            "neg_risk": False,
            "order_type": "",
            "replacement_reason_json": [],
            "replaced_from_order_id": None,
            "replace_count": 0,
            "time_live_sec": None,
            "config_hash": event.config_hash,
            "git_sha": event.git_sha,
            "session_id": event.session_id,
            "run_stage": event.run_stage,
            "last_event_id": event.event_id,
            "updated_at": event.ts_event,
        }

    def _update_time_live(self, record: dict[str, Any]) -> None:
        live_at = _parse_dt(record.get("live_at"))
        terminal_at = (
            _parse_dt(record.get("filled_at"))
            or _parse_dt(record.get("canceled_at"))
            or _parse_dt(record.get("updated_at"))
        )
        terminal_status = str(record.get("terminal_status") or "")
        if (
            live_at and terminal_at and
            terminal_status in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
        ):
            record["time_live_sec"] = max(0.0, (terminal_at - live_at).total_seconds())

    async def _load_record(self, event: SpineEvent) -> dict[str, Any]:
        if not event.order_id:
            return {}
        existing = await self._postgres.get_order_fact(event.order_id)
        return existing or self._empty_record(event)

    async def apply_event(self, event: SpineEvent) -> bool:
        if event.event_type not in self.SUPPORTED_EVENTS:
            return False

        if event.event_type == "order_submit_requested":
            self._pending_requests[self._request_signature(event)] = event
            return True

        if not event.order_id:
            return False

        record = await self._load_record(event)
        if not record:
            return False

        payload = event.payload_json
        record["controller"] = event.controller
        record["strategy"] = event.strategy
        record["condition_id"] = event.condition_id or record.get("condition_id", "")
        record["token_id"] = event.token_id or record.get("token_id", "")
        record["config_hash"] = event.config_hash
        record["git_sha"] = event.git_sha
        record["session_id"] = event.session_id
        record["run_stage"] = event.run_stage
        record["last_event_id"] = event.event_id
        record["updated_at"] = event.ts_event

        side = payload.get("side")
        if side:
            record["side"] = side
        if "price" in payload:
            record["price"] = _to_float(
                payload.get("price"), record.get("price", 0.0),
            )
        if "post_only" in payload:
            record["post_only"] = bool(payload.get("post_only"))
        if "neg_risk" in payload:
            record["neg_risk"] = bool(payload.get("neg_risk"))
        if "order_type" in payload:
            record["order_type"] = str(payload.get("order_type") or "")

        if event.event_type == "order_submit_acknowledged":
            pending = self._pending_requests.pop(self._request_signature(event), None)
            record["submit_requested_at"] = (
                record.get("submit_requested_at")
                or (pending.ts_event if pending is not None else event.ts_event)
            )
            record["submit_ack_at"] = record.get("submit_ack_at") or event.ts_event
            record["original_size"] = _to_float(
                payload.get("size"), record.get("original_size", 0.0),
            )
            record["remaining_size"] = record["original_size"]
            reasons = list(payload.get("replacement_reasons", []))
            replaced = list(payload.get("replaced_order_ids", []))
            record["replacement_reason_json"] = reasons
            record["replaced_from_order_id"] = (
                replaced[0] if replaced
                else record.get("replaced_from_order_id")
            )
            record["replace_count"] = len(replaced)
            if not record.get("terminal_status"):
                record["terminal_status"] = "SUBMITTED"

        elif event.event_type == "order_live":
            record["live_at"] = record.get("live_at") or event.ts_event
            record["original_size"] = _to_float(
                payload.get("size"), record.get("original_size", 0.0),
            )
            if record.get("remaining_size", 0.0) <= 0.0:
                record["remaining_size"] = record["original_size"]
            record["terminal_status"] = "LIVE"

        elif event.event_type == "order_cancel_requested":
            reasons = list(payload.get("reasons", []))
            if reasons:
                record["replacement_reason_json"] = reasons

        elif event.event_type == "order_canceled":
            record["canceled_at"] = record.get("canceled_at") or event.ts_event
            record["terminal_status"] = "CANCELED"

        elif event.event_type == "order_rejected":
            record["terminal_status"] = "REJECTED"
            if not record.get("submit_ack_at"):
                record["submit_ack_at"] = event.ts_event

        elif event.event_type == "order_expired":
            record["terminal_status"] = "EXPIRED"

        elif event.event_type in {"order_partially_filled", "order_filled"}:
            fill_size = _to_float(payload.get("size"))
            record["filled_size"] = _to_float(record.get("filled_size")) + fill_size
            original_size = _to_float(record.get("original_size"))
            record["remaining_size"] = max(0.0, original_size - record["filled_size"])
            if event.event_type == "order_filled":
                record["filled_at"] = record.get("filled_at") or event.ts_event
                record["terminal_status"] = "FILLED"
            else:
                record["terminal_status"] = "PARTIAL"

        self._update_time_live(record)
        return await self._postgres.apply_order_fact_update(event.event_id, record)

    async def run_once(self) -> int:
        if self._stream is None:
            raise RuntimeError("order_fact stream consumer requires a Redis stream store")
        events = await self._stream.read_spine_stream(
            group_name=self._group_name,
            consumer_name=self._consumer_name,
            count=self._read_count,
            block_ms=self._block_ms,
        )
        processed = 0
        for stream_event_id, fields in events:
            try:
                event = self._parse_stream_fields(fields)
                await self.apply_event(event)
            except Exception as exc:
                logger.error(
                    "order_fact_materializer_event_failed",
                    stream_event_id=stream_event_id,
                    error=str(exc),
                    exc_info=True,
                )
                continue
            await self._stream.ack_spine_stream_event(
                stream_event_id,
                group_name=self._group_name,
            )
            processed += 1
        return processed

    async def rebuild_from_event_log(self, *, reset: bool = True) -> int:
        """Replay historical event_log rows into order_fact."""
        if reset:
            self._pending_requests.clear()
            await self._postgres.reset_order_fact_materialization()
        rows = await self._postgres.get_spine_events(
            event_types=sorted(self.SUPPORTED_EVENTS),
            order_by="ts_ingest ASC",
        )
        rebuilt = 0
        for row in rows:
            payload = row.get("payload_json")
            if isinstance(payload, str):
                payload = json.loads(payload)
            event = SpineEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                ts_event=_parse_dt(row["ts_event"]) or datetime.now(UTC),
                ts_ingest=_parse_dt(row["ts_ingest"]) or datetime.now(UTC),
                controller=row["controller"],
                strategy=row["strategy"],
                session_id=row["session_id"],
                git_sha=row["git_sha"],
                config_hash=row["config_hash"],
                run_stage=row["run_stage"],
                condition_id=row.get("condition_id") or None,
                token_id=row.get("token_id") or None,
                order_id=row.get("order_id") or None,
                payload_json=payload or {},
            )
            await self.apply_event(event)
            rebuilt += 1
        return rebuilt

    async def _run_loop(self) -> None:
        if self._stream is None:
            raise RuntimeError("order_fact materializer loop requires a Redis stream store")
        await self._stream.ensure_spine_consumer_group(self._group_name)
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:
                logger.error("order_fact_materializer_loop_error", error=str(exc), exc_info=True)
                await asyncio.sleep(1.0)

    async def start(self) -> asyncio.Task[None]:
        if self._running:
            return self._task  # type: ignore[return-value]
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "order_fact_materializer_started",
            group_name=self._group_name,
            consumer_name=self._consumer_name,
        )
        return self._task

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("order_fact_materializer_stopped")

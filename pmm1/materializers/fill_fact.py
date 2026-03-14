"""Redis stream consumer that materializes fill_fact from spine events."""

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

FILL_FACT_CONSUMER_GROUP = "fill-fact-materializer"


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


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class FillFactMaterializer:
    """Consumes the spine stream and maintains fill_fact."""

    SUPPORTED_EVENTS = {"order_partially_filled", "order_filled"}

    def __init__(
        self,
        postgres_store: PostgresStore,
        stream_store: RedisStateStore | None,
        *,
        consumer_name: str = "pmm1",
        group_name: str = FILL_FACT_CONSUMER_GROUP,
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

    def _build_fill_record(self, event: SpineEvent) -> dict[str, Any]:
        payload = event.payload_json
        return {
            "fill_id": event.event_id,
            "order_id": event.order_id or "",
            "condition_id": event.condition_id or "",
            "token_id": event.token_id or "",
            "controller": event.controller,
            "strategy": event.strategy,
            "side": str(payload.get("side") or ""),
            "fill_ts": event.ts_event,
            "fill_price": _to_float(payload.get("price"), 0.0) or 0.0,
            "fill_size": _to_float(payload.get("size"), 0.0) or 0.0,
            "quote_intent_id": str(payload.get("quote_intent_id") or ""),
            "queue_ahead_estimate": _to_float(payload.get("queue_ahead_estimate")),
            "fill_prob_estimate": _to_float(payload.get("fill_prob_estimate")),
            "expected_spread_ev_usdc": _to_float(payload.get("expected_spread_ev_usdc")),
            "expected_reward_ev_usdc": _to_float(payload.get("expected_reward_ev_usdc")),
            "expected_rebate_ev_usdc": _to_float(payload.get("expected_rebate_ev_usdc")),
            "markout_1s": _to_float(payload.get("markout_1s")),
            "markout_5s": _to_float(payload.get("markout_5s")),
            "markout_30s": _to_float(payload.get("markout_30s")),
            "markout_300s": _to_float(payload.get("markout_300s")),
            "resolution_markout": _to_float(payload.get("resolution_markout")),
            "realized_spread_capture": _to_float(payload.get("realized_spread_capture")),
            "adverse_selection_estimate": _to_float(payload.get("adverse_selection_estimate")),
            "reward_eligible": bool(payload.get("reward_eligible", False)),
            "scoring_flag": bool(payload.get("is_scoring", payload.get("scoring_flag", False))),
            "fee_usdc": _to_float(payload.get("fee_usdc", payload.get("fee"))),
            "dollar_value_usdc": _to_float(
                payload.get("dollar_value_usdc", payload.get("dollar_value")),
            ),
            "mid_at_fill": _to_float(payload.get("mid_at_fill")),
            "config_hash": event.config_hash,
            "git_sha": event.git_sha,
            "session_id": event.session_id,
            "run_stage": event.run_stage,
            "source_event_id": event.event_id,
            "updated_at": event.ts_ingest,
        }

    async def apply_event(self, event: SpineEvent) -> bool:
        if event.event_type not in self.SUPPORTED_EVENTS:
            return False
        await self._postgres.upsert_fill_fact(self._build_fill_record(event))
        return True

    async def run_once(self) -> int:
        if self._stream is None:
            raise RuntimeError("fill_fact stream consumer requires a Redis stream store")
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
                    "fill_fact_materializer_event_failed",
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
        """Replay historical event_log rows into fill_fact."""
        if reset:
            await self._postgres.reset_fill_fact_materialization()
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
            raise RuntimeError("fill_fact materializer loop requires a Redis stream store")
        await self._stream.ensure_spine_consumer_group(self._group_name)
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:
                logger.error("fill_fact_materializer_loop_error", error=str(exc), exc_info=True)
                await asyncio.sleep(1.0)

    async def start(self) -> asyncio.Task[None]:
        if self._running:
            return self._task  # type: ignore[return-value]
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "fill_fact_materializer_started",
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
        logger.info("fill_fact_materializer_stopped")

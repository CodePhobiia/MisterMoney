"""Redis stream consumer that materializes quote_fact from spine events."""

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

QUOTE_FACT_CONSUMER_GROUP = "quote-fact-materializer"


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


class QuoteFactMaterializer:
    """Consumes the spine stream and maintains quote_fact."""

    SUPPORTED_EVENTS = {
        "quote_intent_created",
        "quote_side_suppressed",
        "order_submit_requested",
        "order_submit_acknowledged",
        "order_live",
        "order_partially_filled",
        "order_filled",
        "order_canceled",
        "order_rejected",
        "order_expired",
    }

    def __init__(
        self,
        postgres_store: PostgresStore,
        stream_store: RedisStateStore | None,
        *,
        consumer_name: str = "pmm1",
        group_name: str = QUOTE_FACT_CONSUMER_GROUP,
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
        self._pending_quote_by_signature: dict[str, str] = {}
        self._quote_by_order_id: dict[str, str] = {}

    def _quote_signature(
        self,
        *,
        controller: str,
        strategy: str,
        session_id: str,
        condition_id: str,
        token_id: str,
        side: str,
        price: Any,
        size: Any,
    ) -> str:
        price_norm = "" if price in (None, "") else f"{float(price):.10f}"
        size_norm = "" if size in (None, "") else f"{float(size):.10f}"
        return "|".join(
            [
                controller,
                strategy,
                session_id,
                condition_id or "",
                token_id or "",
                side or "",
                price_norm,
                size_norm,
            ]
        )

    def _quote_signature_from_event(self, event: SpineEvent, payload: dict[str, Any]) -> str:
        side = str(payload.get("side") or "")
        if event.event_type == "quote_intent_created":
            price = payload.get("intended_price")
            size = payload.get("intended_size")
        else:
            price = payload.get("price")
            size = payload.get("size")
        return self._quote_signature(
            controller=event.controller,
            strategy=event.strategy,
            session_id=event.session_id,
            condition_id=event.condition_id or "",
            token_id=event.token_id or "",
            side=side,
            price=price,
            size=size,
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
            "quote_intent_id": event.event_id,
            "cycle_id": "",
            "controller": event.controller,
            "strategy": event.strategy,
            "condition_id": event.condition_id or "",
            "token_id": event.token_id or "",
            "order_id": "",
            "side": "",
            "intended_price": None,
            "intended_size": None,
            "submitted_price": None,
            "submitted_size": None,
            "suppression_reason_json": [],
            "risk_adjustment_reason_json": [],
            "book_quality_json": {},
            "expected_spread_ev_usdc": None,
            "expected_reward_ev_usdc": None,
            "expected_rebate_ev_usdc": None,
            "expected_total_ev_usdc": None,
            "fill_prob_30s": None,
            "became_live": False,
            "filled_any": False,
            "filled_full": False,
            "terminal_status": "",
            "config_hash": event.config_hash,
            "git_sha": event.git_sha,
            "session_id": event.session_id,
            "run_stage": event.run_stage,
            "last_event_id": event.event_id,
            "updated_at": event.ts_event,
        }

    async def _load_record(self, quote_intent_id: str) -> dict[str, Any] | None:
        return await self._postgres.get_quote_fact(quote_intent_id)

    async def apply_event(self, event: SpineEvent) -> bool:
        if event.event_type not in self.SUPPORTED_EVENTS:
            return False

        payload = event.payload_json

        if event.event_type == "quote_intent_created":
            side = str(payload.get("side") or "")
            record = self._empty_record(event)
            record["side"] = side
            record["intended_price"] = _to_float(payload.get("intended_price"))
            record["intended_size"] = _to_float(payload.get("intended_size"))
            record["book_quality_json"] = {
                "stage": payload.get("stage"),
                "question": payload.get("question"),
                "fair_value": payload.get("fair_value"),
                "confidence": payload.get("confidence"),
                "inventory": payload.get("inventory"),
                "reservation_price": payload.get("reservation_price"),
                "half_spread": payload.get("half_spread"),
                "neg_risk": payload.get("neg_risk"),
            }
            record["terminal_status"] = "INTENDED"
            signature = self._quote_signature_from_event(event, payload)
            self._pending_quote_by_signature[signature] = event.event_id
            return await self._postgres.apply_quote_fact_update(event.event_id, record)

        if event.event_type == "quote_side_suppressed":
            side = str(payload.get("side") or "")
            record = self._empty_record(event)
            record["side"] = side
            record["suppression_reason_json"] = list(payload.get("reasons", []))
            record["book_quality_json"] = {
                "stage": payload.get("stage"),
                "question": payload.get("question"),
                "fair_value": payload.get("fair_value"),
                "confidence": payload.get("confidence"),
                "inventory": payload.get("inventory"),
                "reservation_price": payload.get("reservation_price"),
                "half_spread": payload.get("half_spread"),
                "neg_risk": payload.get("neg_risk"),
            }
            record["terminal_status"] = "SUPPRESSED"
            return await self._postgres.apply_quote_fact_update(event.event_id, record)

        signature = self._quote_signature_from_event(event, payload)
        quote_intent_id = self._pending_quote_by_signature.get(signature)
        if event.order_id:
            quote_intent_id = quote_intent_id or self._quote_by_order_id.get(event.order_id)
        if not quote_intent_id:
            return False

        maybe_record = await self._load_record(quote_intent_id)
        if maybe_record is None:
            return False
        record = maybe_record

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
        if payload.get("side"):
            record["side"] = payload.get("side")

        if event.event_type == "order_submit_requested":
            record["submitted_price"] = _to_float(payload.get("price"))
            record["submitted_size"] = _to_float(payload.get("size"))
            record["terminal_status"] = record.get("terminal_status") or "SUBMIT_REQUESTED"

        elif event.event_type == "order_submit_acknowledged":
            record["order_id"] = event.order_id or record.get("order_id", "")
            record["submitted_price"] = _to_float(payload.get("price"))
            record["submitted_size"] = _to_float(payload.get("size"))
            record["terminal_status"] = "SUBMITTED"
            if event.order_id:
                self._quote_by_order_id[event.order_id] = quote_intent_id

        elif event.event_type == "order_live":
            record["order_id"] = event.order_id or record.get("order_id", "")
            record["became_live"] = True
            record["terminal_status"] = "LIVE"
            if event.order_id:
                self._quote_by_order_id[event.order_id] = quote_intent_id

        elif event.event_type == "order_partially_filled":
            record["order_id"] = event.order_id or record.get("order_id", "")
            record["filled_any"] = True
            record["terminal_status"] = "PARTIAL"
            if event.order_id:
                self._quote_by_order_id[event.order_id] = quote_intent_id

        elif event.event_type == "order_filled":
            record["order_id"] = event.order_id or record.get("order_id", "")
            record["filled_any"] = True
            record["filled_full"] = True
            record["terminal_status"] = "FILLED"
            if event.order_id:
                self._quote_by_order_id[event.order_id] = quote_intent_id

        elif event.event_type == "order_canceled":
            record["terminal_status"] = "CANCELED"

        elif event.event_type == "order_rejected":
            record["terminal_status"] = "REJECTED"

        elif event.event_type == "order_expired":
            record["terminal_status"] = "EXPIRED"

        return await self._postgres.apply_quote_fact_update(event.event_id, record)

    async def run_once(self) -> int:
        if self._stream is None:
            raise RuntimeError("quote_fact stream consumer requires a Redis stream store")
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
                    "quote_fact_materializer_event_failed",
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
        if reset:
            self._pending_quote_by_signature.clear()
            self._quote_by_order_id.clear()
            await self._postgres.reset_quote_fact_materialization()
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
            raise RuntimeError("quote_fact materializer loop requires a Redis stream store")
        await self._stream.ensure_spine_consumer_group(self._group_name)
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:
                logger.error("quote_fact_materializer_loop_error", error=str(exc), exc_info=True)
                await asyncio.sleep(1.0)

    async def start(self) -> asyncio.Task[None]:
        if self._running:
            return self._task  # type: ignore[return-value]
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "quote_fact_materializer_started",
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
        logger.info("quote_fact_materializer_stopped")

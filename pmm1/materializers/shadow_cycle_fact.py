"""Redis stream consumer that materializes shadow_cycle_fact from spine events."""

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

SHADOW_CYCLE_FACT_CONSUMER_GROUP = "shadow-cycle-fact-materializer"


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


class ShadowCycleFactMaterializer:
    """Consumes the spine stream and maintains shadow_cycle_fact."""

    SUPPORTED_EVENTS = {"pmm2_shadow_cycle"}

    def __init__(
        self,
        postgres_store: PostgresStore,
        stream_store: RedisStateStore | None,
        *,
        consumer_name: str = "pmm1",
        group_name: str = SHADOW_CYCLE_FACT_CONSUMER_GROUP,
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

    def _build_record(self, event: SpineEvent) -> dict[str, Any]:
        payload = event.payload_json
        comparison = payload.get("comparison", {})
        summary = payload.get("summary", {})
        gate_diagnostics = payload.get("gate_diagnostics", {})
        return {
            "source_event_id": event.event_id,
            "cycle_num": payload.get("cycle_num"),
            "ts": event.ts_event,
            "ready_for_live": bool(summary.get("ready_for_live", False)),
            "window_cycles": gate_diagnostics.get("window_cycles"),
            "ev_sample_count": gate_diagnostics.get("ev_sample_count"),
            "reward_sample_count": gate_diagnostics.get("reward_sample_count"),
            "churn_sample_count": gate_diagnostics.get("churn_sample_count"),
            "v1_market_count": len(comparison.get("v1_markets", [])),
            "pmm2_market_count": len(comparison.get("pmm2_markets", [])),
            "market_overlap_pct": comparison.get("market_overlap_pct"),
            "overlap_quote_distance_bps": comparison.get("overlap_quote_distance_bps"),
            "v1_total_ev_usdc": comparison.get("v1_total_ev_usdc"),
            "pmm2_total_ev_usdc": comparison.get("pmm2_total_ev_usdc"),
            "ev_delta_usdc": comparison.get("ev_delta_usdc"),
            "v1_reward_market_count": comparison.get("v1_reward_market_count"),
            "pmm2_reward_market_count": comparison.get("pmm2_reward_market_count"),
            "reward_market_delta": comparison.get("reward_market_delta"),
            "v1_reward_ev_usdc": comparison.get("v1_reward_ev_usdc"),
            "pmm2_reward_ev_usdc": comparison.get("pmm2_reward_ev_usdc"),
            "reward_ev_delta_usdc": comparison.get("reward_ev_delta_usdc"),
            "v1_cancel_rate_per_order_min": comparison.get("v1_cancel_rate_per_order_min"),
            "pmm2_cancel_rate_per_order_min": comparison.get("pmm2_cancel_rate_per_order_min"),
            "churn_delta_per_order_min": comparison.get("churn_delta_per_order_min"),
            "gate_blockers_json": list(gate_diagnostics.get("blocking_gates", [])),
            "gate_diagnostics_json": gate_diagnostics,
            "v1_state_json": comparison.get("v1_state_json", {}),
            "pmm2_plan_json": comparison.get("pmm2_plan_json", {}),
            "controller": event.controller,
            "strategy": event.strategy,
            "config_hash": event.config_hash,
            "git_sha": event.git_sha,
            "session_id": event.session_id,
            "run_stage": event.run_stage,
            "updated_at": event.ts_ingest,
        }

    async def apply_event(self, event: SpineEvent) -> bool:
        if event.event_type not in self.SUPPORTED_EVENTS:
            return False
        await self._postgres.upsert_shadow_cycle_fact(self._build_record(event))
        return True

    async def run_once(self) -> int:
        if self._stream is None:
            raise RuntimeError("shadow_cycle_fact stream consumer requires a Redis stream store")
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
                    "shadow_cycle_fact_materializer_event_failed",
                    stream_event_id=stream_event_id,
                    error=str(exc),
                    exc_info=True,
                )
                continue
            await self._stream.ack_spine_stream_event(stream_event_id, group_name=self._group_name)
            processed += 1
        return processed

    async def rebuild_from_event_log(self, *, reset: bool = True) -> int:
        if reset:
            await self._postgres.reset_shadow_cycle_fact_materialization()
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
            raise RuntimeError("shadow_cycle_fact materializer loop requires a Redis stream store")
        await self._stream.ensure_spine_consumer_group(self._group_name)
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:
                logger.error(
                    "shadow_cycle_fact_materializer_loop_error",
                    error=str(exc),
                    exc_info=True,
                )
                await asyncio.sleep(1.0)

    async def start(self) -> asyncio.Task[None]:
        if self._running:
            return self._task  # type: ignore[return-value]
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "shadow_cycle_fact_materializer_started",
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
        logger.info("shadow_cycle_fact_materializer_stopped")

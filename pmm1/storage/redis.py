"""Redis storage — hot state, books, live orders, heartbeat from §9."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, cast

import structlog

if TYPE_CHECKING:
    from pmm1.storage.spine import SpineEvent

logger = structlog.get_logger(__name__)

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None  # type: ignore


class RedisStateStore:
    """Redis-backed hot state store for PMM-1.

    Stores:
    - Order book snapshots (for fast recovery)
    - Live order state
    - Heartbeat state
    - Market metadata cache
    - Feature snapshots
    """

    # Key prefixes
    PREFIX_BOOK = "pmm1:book:"
    PREFIX_ORDER = "pmm1:order:"
    PREFIX_ORDERS_BY_TOKEN = "pmm1:orders_by_token:"
    PREFIX_HEARTBEAT = "pmm1:heartbeat"
    PREFIX_MARKET = "pmm1:market:"
    PREFIX_FEATURE = "pmm1:feature:"
    PREFIX_POSITION = "pmm1:position:"
    KEY_BOT_STATE = "pmm1:bot_state"
    KEY_NAV = "pmm1:nav"
    KEY_KILL_SWITCH = "pmm1:kill_switch"
    KEY_SPINE_STREAM = "pmm1:spine:events"

    def __init__(self, redis_url: str = "redis://localhost:6379/0") -> None:
        self._url = redis_url
        self._client: Any = None

    async def connect(self) -> None:
        """Connect to Redis."""
        if aioredis is None:
            raise ImportError("redis[hiredis] is not installed")
        self._client = aioredis.from_url(
            self._url,
            decode_responses=True,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
        )
        # Test connection
        await self._client.ping()
        logger.info("redis_connected", url=self._url.split("@")[-1])

    async def close(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.close()
            logger.info("redis_disconnected")

    async def _ensure_connected(self) -> Any:
        if self._client is None:
            await self.connect()
        return self._client

    # ── Book State ──

    async def save_book_snapshot(
        self,
        token_id: str,
        bids: list[dict[str, str]],
        asks: list[dict[str, str]],
        tick_size: str = "0.01",
    ) -> None:
        """Save order book snapshot for fast recovery."""
        client = await self._ensure_connected()
        data = json.dumps({
            "bids": bids,
            "asks": asks,
            "tick_size": tick_size,
            "timestamp": time.time(),
        })
        await client.set(f"{self.PREFIX_BOOK}{token_id}", data, ex=300)  # 5 min TTL

    async def get_book_snapshot(self, token_id: str) -> dict[str, Any] | None:
        """Retrieve cached book snapshot."""
        client = await self._ensure_connected()
        data = await client.get(f"{self.PREFIX_BOOK}{token_id}")
        if data:
            return cast(dict[str, Any], json.loads(data))
        return None

    # ── Order State ──

    async def save_order(self, order_id: str, order_data: dict[str, Any]) -> None:
        """Save order state."""
        client = await self._ensure_connected()
        order_data["updated_at"] = time.time()
        await client.hset(
            f"{self.PREFIX_ORDER}{order_id}",
            mapping={
                k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                for k, v in order_data.items()
            },
        )
        # Index by token
        token_id = order_data.get("token_id", "")
        if token_id:
            await client.sadd(f"{self.PREFIX_ORDERS_BY_TOKEN}{token_id}", order_id)

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        """Get order state."""
        client = await self._ensure_connected()
        data = await client.hgetall(f"{self.PREFIX_ORDER}{order_id}")
        return dict(data) if data else None

    async def get_orders_for_token(self, token_id: str) -> list[str]:
        """Get all order IDs for a token."""
        client = await self._ensure_connected()
        return list(await client.smembers(f"{self.PREFIX_ORDERS_BY_TOKEN}{token_id}"))

    async def remove_order(self, order_id: str, token_id: str = "") -> None:
        """Remove order state."""
        client = await self._ensure_connected()
        await client.delete(f"{self.PREFIX_ORDER}{order_id}")
        if token_id:
            await client.srem(f"{self.PREFIX_ORDERS_BY_TOKEN}{token_id}", order_id)

    # ── Heartbeat ──

    async def save_heartbeat(self, heartbeat_id: str) -> None:
        """Save last heartbeat ID and timestamp."""
        client = await self._ensure_connected()
        await client.hset(self.PREFIX_HEARTBEAT, mapping={
            "heartbeat_id": heartbeat_id,
            "timestamp": str(time.time()),
        })

    async def get_heartbeat(self) -> dict[str, str] | None:
        """Get last heartbeat info."""
        client = await self._ensure_connected()
        data = await client.hgetall(self.PREFIX_HEARTBEAT)
        return dict(data) if data else None

    # ── Market Metadata ──

    async def save_market_metadata(
        self,
        condition_id: str,
        metadata: dict[str, Any],
    ) -> None:
        """Cache market metadata."""
        client = await self._ensure_connected()
        await client.set(
            f"{self.PREFIX_MARKET}{condition_id}",
            json.dumps(metadata),
            ex=3600,  # 1 hour TTL
        )

    async def get_market_metadata(self, condition_id: str) -> dict[str, Any] | None:
        """Get cached market metadata."""
        client = await self._ensure_connected()
        data = await client.get(f"{self.PREFIX_MARKET}{condition_id}")
        if data:
            return cast(dict[str, Any], json.loads(data))
        return None

    # ── Feature Cache ──

    async def save_features(
        self,
        token_id: str,
        features: dict[str, Any],
    ) -> None:
        """Cache latest feature vector."""
        client = await self._ensure_connected()
        features["cached_at"] = time.time()
        await client.set(
            f"{self.PREFIX_FEATURE}{token_id}",
            json.dumps(features),
            ex=60,  # 1 min TTL
        )

    async def get_features(self, token_id: str) -> dict[str, Any] | None:
        """Get cached features."""
        client = await self._ensure_connected()
        data = await client.get(f"{self.PREFIX_FEATURE}{token_id}")
        if data:
            return cast(dict[str, Any], json.loads(data))
        return None

    # ── Position State ──

    async def save_position(
        self,
        condition_id: str,
        position_data: dict[str, Any],
    ) -> None:
        """Save position state."""
        client = await self._ensure_connected()
        await client.set(
            f"{self.PREFIX_POSITION}{condition_id}",
            json.dumps(position_data),
            ex=300,
        )

    async def get_position(self, condition_id: str) -> dict[str, Any] | None:
        """Get position state."""
        client = await self._ensure_connected()
        data = await client.get(f"{self.PREFIX_POSITION}{condition_id}")
        if data:
            return cast(dict[str, Any], json.loads(data))
        return None

    # ── Bot State ──

    async def save_bot_state(self, state: dict[str, Any]) -> None:
        """Save overall bot state."""
        client = await self._ensure_connected()
        state["updated_at"] = time.time()
        await client.set(self.KEY_BOT_STATE, json.dumps(state))

    async def get_bot_state(self) -> dict[str, Any] | None:
        """Get bot state."""
        client = await self._ensure_connected()
        data = await client.get(self.KEY_BOT_STATE)
        if data:
            return cast(dict[str, Any], json.loads(data))
        return None

    # ── NAV ──

    async def save_nav(self, nav: float) -> None:
        """Save current NAV."""
        client = await self._ensure_connected()
        await client.set(self.KEY_NAV, json.dumps({
            "nav": nav,
            "timestamp": time.time(),
        }))

    async def get_nav(self) -> float | None:
        """Get latest NAV."""
        client = await self._ensure_connected()
        data = await client.get(self.KEY_NAV)
        if data:
            return cast(float | None, json.loads(data).get("nav"))
        return None

    # ── Kill Switch ──

    async def save_kill_switch(self, is_triggered: bool, reasons: list[str]) -> None:
        """Persist kill switch state."""
        client = await self._ensure_connected()
        await client.set(self.KEY_KILL_SWITCH, json.dumps({
            "is_triggered": is_triggered,
            "reasons": reasons,
            "timestamp": time.time(),
        }))

    async def get_kill_switch(self) -> dict[str, Any] | None:
        """Get kill switch state."""
        client = await self._ensure_connected()
        data = await client.get(self.KEY_KILL_SWITCH)
        if data:
            return cast(dict[str, Any], json.loads(data))
        return None

    # Redis Streams - data spine transport

    async def append_spine_stream_event(
        self,
        event: SpineEvent | dict[str, Any],
        *,
        stream_key: str | None = None,
        maxlen: int | None = 100_000,
    ) -> str:
        """Append one canonical spine event to a Redis Stream."""
        from pmm1.storage.spine import SpineEvent as SpineEventModel

        payload = (
            event if isinstance(event, SpineEventModel)
            else SpineEventModel.model_validate(event)
        )
        client = await self._ensure_connected()
        fields = {
            "event_id": payload.event_id,
            "event_type": payload.event_type,
            "ts_event": payload.ts_event.isoformat(),
            "ts_ingest": payload.ts_ingest.isoformat(),
            "controller": payload.controller,
            "strategy": payload.strategy,
            "session_id": payload.session_id,
            "git_sha": payload.git_sha,
            "config_hash": payload.config_hash,
            "run_stage": payload.run_stage,
            "condition_id": payload.condition_id or "",
            "token_id": payload.token_id or "",
            "order_id": payload.order_id or "",
            "payload_json": json.dumps(payload.payload_json, sort_keys=True),
        }
        kwargs: dict[str, Any] = {}
        if maxlen is not None:
            kwargs = {"maxlen": maxlen, "approximate": True}
        return cast(str, await client.xadd(stream_key or self.KEY_SPINE_STREAM, fields, **kwargs))

    async def ensure_spine_consumer_group(
        self,
        group_name: str,
        *,
        stream_key: str | None = None,
        start_id: str = "0",
    ) -> None:
        """Create a Redis Streams consumer group if it does not already exist."""
        client = await self._ensure_connected()
        try:
            await client.xgroup_create(
                stream_key or self.KEY_SPINE_STREAM,
                group_name,
                id=start_id,
                mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read_spine_stream(
        self,
        *,
        stream_key: str | None = None,
        group_name: str | None = None,
        consumer_name: str | None = None,
        start_id: str = ">",
        count: int = 100,
        block_ms: int = 1000,
    ) -> list[tuple[str, dict[str, str]]]:
        """Read spine events from Redis Streams."""
        client = await self._ensure_connected()
        target_stream = stream_key or self.KEY_SPINE_STREAM
        if group_name and consumer_name:
            streams = await client.xreadgroup(
                group_name,
                consumer_name,
                {target_stream: start_id},
                count=count,
                block=block_ms,
            )
        else:
            streams = await client.xread(
                {target_stream: start_id},
                count=count,
                block=block_ms,
            )

        events: list[tuple[str, dict[str, str]]] = []
        for _, entries in streams:
            for event_id, fields in entries:
                events.append((event_id, fields))
        return events

    async def ack_spine_stream_event(
        self,
        event_id: str,
        *,
        group_name: str,
        stream_key: str | None = None,
    ) -> int:
        """Acknowledge one spine event in a consumer group."""
        client = await self._ensure_connected()
        return int(await client.xack(stream_key or self.KEY_SPINE_STREAM, group_name, event_id))

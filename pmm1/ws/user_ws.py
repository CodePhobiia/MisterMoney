"""User WebSocket client — authenticated order/trade updates.

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/user
Handles: order updates, trade updates, settlement status
Auto-reconnect + reconciliation trigger.

Order lifecycle from WS:
    MATCHED → MINED → CONFIRMED → RETRYING → FAILED
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any, Callable, Coroutine

import structlog
import websockets
from websockets.client import WebSocketClientProtocol

from pmm1.state.orders import OrderState, OrderTracker

logger = structlog.get_logger(__name__)


class UserWebSocket:
    """Authenticated WebSocket client for user-specific order/trade updates."""

    def __init__(
        self,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user",
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        wallet_address: str = "",
        order_tracker: OrderTracker | None = None,
        reconnect_delay_s: float = 1.0,
        max_reconnect_delay_s: float = 30.0,
        on_order_update: Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None = None,
        on_trade_update: Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None = None,
        on_settlement: Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None = None,
        on_reconnect: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._wallet_address = wallet_address
        self._orders = order_tracker or OrderTracker()
        self._reconnect_delay = reconnect_delay_s
        self._max_reconnect_delay = max_reconnect_delay_s

        # Callbacks
        self._on_order_update = on_order_update
        self._on_trade_update = on_trade_update
        self._on_settlement = on_settlement
        self._on_reconnect = on_reconnect

        self._ws: WebSocketClientProtocol | None = None
        self._is_running = False
        self._task: asyncio.Task | None = None
        self._last_message_ts: float = 0.0
        self._message_count: int = 0
        self._reconnect_count: int = 0

    def _ws_is_open(self) -> bool:
        """Check if the WebSocket connection is open (compatible with websockets 15.x+)."""
        if self._ws is None:
            return False
        try:
            from websockets.protocol import State
            return self._ws.state is State.OPEN
        except (ImportError, AttributeError):
            return getattr(self._ws, 'open', False)

    @property
    def is_connected(self) -> bool:
        return self._ws_is_open()

    def _build_auth_message(self, market_ids: list[str] | None = None) -> dict[str, Any]:
        """Build authentication message for the user WebSocket.

        Per Polymarket docs, the user channel auth uses raw credentials
        (apiKey, secret, passphrase) — NOT an HMAC signature.
        """
        return {
            "auth": {
                "apiKey": self._api_key,
                "secret": self._api_secret,
                "passphrase": self._api_passphrase,
            },
            "markets": market_ids or [],
            "type": "user",
        }

    async def connect(self) -> None:
        """Establish authenticated WebSocket connection."""
        logger.info("user_ws_connecting", url=self._ws_url)
        self._ws = await websockets.connect(
            self._ws_url,
            ping_interval=None,  # We handle PING/PONG at text level
            ping_timeout=None,
            close_timeout=5,
            max_size=5 * 1024 * 1024,
        )

        # Send authentication
        auth_msg = self._build_auth_message()
        await self._ws.send(json.dumps(auth_msg))
        logger.info("user_ws_connected_and_authenticated")

    async def _handle_message(self, raw: str) -> None:
        """Process a single WebSocket message."""
        self._last_message_ts = time.time()
        self._message_count += 1

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("user_ws_invalid_json", raw=raw[:200])
            return

        messages = data if isinstance(data, list) else [data]

        for msg in messages:
            msg_type = msg.get("event_type") or msg.get("type", "")

            if msg_type in ("order", "order_update"):
                await self._handle_order_update(msg)
            elif msg_type in ("trade", "trade_update"):
                await self._handle_trade_update(msg)
            elif msg_type in ("settlement", "resolution"):
                await self._handle_settlement(msg)
            elif msg_type in ("subscribed", "connected", "authenticated", "pong"):
                logger.debug("user_ws_control", type=msg_type)
            elif msg_type == "error":
                logger.error("user_ws_error_message", msg=msg)
            else:
                logger.debug("user_ws_unknown_type", type=msg_type)

    async def _handle_order_update(self, msg: dict[str, Any]) -> None:
        """Process order state transition from WS."""
        order_id = msg.get("orderID") or msg.get("order_id") or msg.get("id", "")
        status = msg.get("status", "").upper()

        # Map WS status to our OrderState
        state_map: dict[str, OrderState] = {
            "LIVE": OrderState.LIVE,
            "MATCHED": OrderState.MATCHED,
            "DELAYED": OrderState.DELAYED,
            "MINED": OrderState.MATCHED,  # MINED is a sub-state of matched
            "CONFIRMED": OrderState.FILLED,
            "RETRYING": OrderState.RETRYING,
            "FAILED": OrderState.FAILED,
            "CANCELED": OrderState.CANCELED,
            "CANCELLED": OrderState.CANCELED,
            "EXPIRED": OrderState.EXPIRED,
        }

        new_state = state_map.get(status)
        if new_state and order_id:
            self._orders.update_state(order_id, new_state)

        logger.info(
            "user_ws_order_update",
            order_id=order_id[:16] if order_id else "?",
            status=status,
        )

        if self._on_order_update:
            await self._on_order_update(msg)

    async def _handle_trade_update(self, msg: dict[str, Any]) -> None:
        """Process trade/fill notification from WS."""
        # Trade payloads can carry a generic `id` that is the match/trade UUID,
        # not our order ID. Only use explicit order-id fields for tracker updates.
        order_id = (
            msg.get("orderID")
            or msg.get("order_id")
            or msg.get("makerOrderID")
            or msg.get("maker_order_id")
            or msg.get("takerOrderID")
            or msg.get("taker_order_id")
            or ""
        )
        fill_size = msg.get("size") or msg.get("matchSize", "0")
        fill_price = msg.get("price") or msg.get("matchPrice")

        if order_id and fill_size:
            self._orders.apply_fill(order_id, str(fill_size), str(fill_price) if fill_price else None)

        logger.info(
            "user_ws_trade_update",
            order_id=order_id[:16] if order_id else "?",
            size=fill_size,
            price=fill_price,
        )

        if self._on_trade_update:
            await self._on_trade_update(msg)

    async def _handle_settlement(self, msg: dict[str, Any]) -> None:
        """Process settlement/resolution notification."""
        market = msg.get("market") or msg.get("conditionId", "")
        status = msg.get("status", "")

        logger.info(
            "user_ws_settlement",
            market=market[:16] if market else "?",
            status=status,
        )

        if self._on_settlement:
            await self._on_settlement(msg)

    async def _ping_loop(self) -> None:
        """Send PING text every 10s as required by Polymarket WS."""
        while self._is_running and self._ws_is_open():
            try:
                await self._ws.send("PING")
            except Exception:
                break
            await asyncio.sleep(10)

    async def _listen_loop(self) -> None:
        """Main message processing loop."""
        if not self._ws:
            return

        # Start PING heartbeat in parallel
        ping_task = asyncio.create_task(self._ping_loop())

        try:
            async for message in self._ws:
                if not self._is_running:
                    break
                if isinstance(message, bytes):
                    message = message.decode("utf-8")
                # Skip PONG responses
                if message.strip() == "PONG":
                    continue
                await self._handle_message(message)
        except websockets.ConnectionClosed as e:
            logger.warning("user_ws_closed", code=e.code, reason=e.reason)
        except Exception as e:
            logger.error("user_ws_error", error=str(e))
        finally:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

    async def _run_with_reconnect(self) -> None:
        """Run the WebSocket with automatic reconnection."""
        delay = self._reconnect_delay

        while self._is_running:
            try:
                await self.connect()
                delay = self._reconnect_delay

                # Trigger reconciliation after reconnect
                if self._reconnect_count > 0 and self._on_reconnect:
                    logger.info("user_ws_triggering_reconciliation")
                    await self._on_reconnect()

                self._reconnect_count += 1
                await self._listen_loop()

            except Exception as e:
                logger.error("user_ws_connection_failed", error=str(e))

            if not self._is_running:
                break

            logger.info("user_ws_reconnecting", delay_s=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_reconnect_delay)

    def start(self) -> asyncio.Task:
        """Start the authenticated WebSocket connection."""
        self._is_running = True
        self._task = asyncio.create_task(self._run_with_reconnect())
        return self._task

    async def stop(self) -> None:
        """Stop the WebSocket connection."""
        self._is_running = False
        if self._ws_is_open():
            await self._ws.close()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("user_ws_stopped")

    def get_stats(self) -> dict[str, Any]:
        return {
            "is_connected": self.is_connected,
            "message_count": self._message_count,
            "reconnect_count": self._reconnect_count,
            "seconds_since_last_message": (
                time.time() - self._last_message_ts if self._last_message_ts else float("inf")
            ),
        }

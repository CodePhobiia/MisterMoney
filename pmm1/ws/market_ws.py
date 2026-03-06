"""Market WebSocket client — book snapshots, price deltas, tick size changes.

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market
Handles: book snapshots, price_change deltas, last_trade_price, tick_size_change
Auto-reconnect with book rebuild.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any, Callable, Coroutine

import structlog
import websockets
from websockets.client import WebSocketClientProtocol

from pmm1.state.books import BookManager

logger = structlog.get_logger(__name__)

# Message types from the market WebSocket
MSG_BOOK = "book"
MSG_PRICE_CHANGE = "price_change"
MSG_LAST_TRADE_PRICE = "last_trade_price"
MSG_TICK_SIZE_CHANGE = "tick_size_change"
MSG_BEST_BID_ASK = "best_bid_ask"


class MarketWebSocket:
    """WebSocket client for market data (order books, trades, tick sizes).

    Subscribes to asset IDs and maintains local book state via BookManager.
    """

    def __init__(
        self,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        book_manager: BookManager | None = None,
        reconnect_delay_s: float = 1.0,
        max_reconnect_delay_s: float = 30.0,
        on_book_update: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_trade: Callable[[str, float], Coroutine[Any, Any, None]] | None = None,
        on_tick_change: Callable[[str, Decimal], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self._ws_url = ws_url
        # IMPORTANT: don't use `book_manager or BookManager()` because an empty
        # BookManager is falsy (__len__ == 0) and would incorrectly create a new instance.
        self._books = book_manager if book_manager is not None else BookManager()
        self._reconnect_delay = reconnect_delay_s
        self._max_reconnect_delay = max_reconnect_delay_s

        # Callbacks
        self._on_book_update = on_book_update
        self._on_trade = on_trade
        self._on_tick_change = on_tick_change

        self._ws: WebSocketClientProtocol | None = None
        self._subscribed_assets: set[str] = set()
        self._is_running = False
        self._task: asyncio.Task | None = None
        self._last_message_ts: float = 0.0
        self._message_count: int = 0

    @property
    def book_manager(self) -> BookManager:
        return self._books

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

    @property
    def seconds_since_last_message(self) -> float:
        if self._last_message_ts == 0:
            return float("inf")
        return time.time() - self._last_message_ts

    @property
    def is_stale(self) -> bool:
        """True if no message received in >2 seconds."""
        return self.seconds_since_last_message > 2.0

    async def connect(self) -> None:
        """Establish WebSocket connection."""
        logger.info("market_ws_connecting", url=self._ws_url)
        self._ws = await websockets.connect(
            self._ws_url,
            ping_interval=None,  # We handle PING/PONG at text level
            ping_timeout=None,
            close_timeout=5,
            max_size=10 * 1024 * 1024,  # 10 MB max message
        )
        logger.info("market_ws_connected")

    async def subscribe(self, asset_ids: list[str]) -> None:
        """Subscribe to asset IDs for book updates."""
        if not self._ws_is_open():
            raise ConnectionError("WebSocket not connected")

        new_assets = set(asset_ids) - self._subscribed_assets

        if not new_assets:
            return

        # Polymarket WS subscription message format (per docs)
        sub_msg = {
            "assets_ids": list(new_assets),  # Note: assets_ids (plural)
            "type": "market",
        }

        await self._ws.send(json.dumps(sub_msg))
        self._subscribed_assets.update(new_assets)
        logger.info("market_ws_subscribed", count=len(new_assets), total=len(self._subscribed_assets))

    async def unsubscribe(self, asset_ids: list[str]) -> None:
        """Unsubscribe from asset IDs."""
        if not self._ws_is_open():
            return

        unsub_msg = {
            "assets_ids": asset_ids,
            "operation": "unsubscribe",
        }
        await self._ws.send(json.dumps(unsub_msg))
        self._subscribed_assets -= set(asset_ids)
        logger.info("market_ws_unsubscribed", count=len(asset_ids))

    async def _handle_message(self, raw: str) -> None:
        """Process a single WebSocket message."""
        self._last_message_ts = time.time()
        self._message_count += 1

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("market_ws_invalid_json", raw=raw[:200])
            return

        # Handle array of messages
        messages = data if isinstance(data, list) else [data]

        for msg in messages:
            msg_type = msg.get("event_type") or msg.get("type", "")
            asset_id = msg.get("asset_id", "")

            if msg_type == MSG_BOOK:
                await self._handle_book_snapshot(asset_id, msg)
            elif msg_type == MSG_PRICE_CHANGE:
                await self._handle_price_change(asset_id, msg)
            elif msg_type == MSG_LAST_TRADE_PRICE:
                await self._handle_last_trade(asset_id, msg)
            elif msg_type == MSG_TICK_SIZE_CHANGE:
                await self._handle_tick_size_change(asset_id, msg)
            elif msg_type == MSG_BEST_BID_ASK:
                # Informational; our local book already has this
                pass
            elif msg_type in ("subscribed", "connected", "pong"):
                logger.debug("market_ws_control", type=msg_type)
            else:
                logger.debug("market_ws_unknown_type", type=msg_type, keys=list(msg.keys()))

    async def _handle_book_snapshot(self, asset_id: str, msg: dict[str, Any]) -> None:
        """Apply full book snapshot."""
        book = self._books.get_or_create(asset_id)

        bids = msg.get("bids", [])
        asks = msg.get("asks", [])

        # Handle various formats
        if isinstance(bids, list) and bids:
            if isinstance(bids[0], dict):
                book.apply_snapshot(bids, asks)
            elif isinstance(bids[0], list):
                # Format: [[price, size], ...]
                bids = [{"price": b[0], "size": b[1]} for b in bids]
                asks = [{"price": a[0], "size": a[1]} for a in asks]
                book.apply_snapshot(bids, asks)

        # Set tick size if provided
        tick_size = msg.get("min_tick_size") or msg.get("tick_size")
        if tick_size:
            book.set_tick_size(Decimal(str(tick_size)))

        if self._on_book_update:
            await self._on_book_update(asset_id)

    async def _handle_price_change(self, asset_id: str, msg: dict[str, Any]) -> None:
        """Apply incremental price delta."""
        book = self._books.get_or_create(asset_id)

        changes = msg.get("changes", [])
        for change in changes:
            # Format: {"side": "bids"|"asks", "price": "0.50", "size": "100"}
            # or: [side, price, size]
            if isinstance(change, dict):
                side = change.get("side", "")
                book.apply_delta([change], side)
            elif isinstance(change, list) and len(change) >= 3:
                side = change[0]  # "bids" or "asks"
                delta = {"price": change[1], "size": change[2]}
                book.apply_delta([delta], side)

        if self._on_book_update:
            await self._on_book_update(asset_id)

    async def _handle_last_trade(self, asset_id: str, msg: dict[str, Any]) -> None:
        """Handle last trade price update."""
        price = msg.get("price") or msg.get("last_trade_price")
        if price is not None:
            book = self._books.get_or_create(asset_id)
            book.last_trade_price = float(price)

            if self._on_trade:
                await self._on_trade(asset_id, float(price))

    async def _handle_tick_size_change(self, asset_id: str, msg: dict[str, Any]) -> None:
        """Handle tick size change (critical for price rounding)."""
        new_tick = msg.get("min_tick_size") or msg.get("tick_size")
        if new_tick is not None:
            new_tick_decimal = Decimal(str(new_tick))
            book = self._books.get_or_create(asset_id)
            book.set_tick_size(new_tick_decimal)

            if self._on_tick_change:
                await self._on_tick_change(asset_id, new_tick_decimal)

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
            logger.warning("market_ws_closed", code=e.code, reason=e.reason)
        except Exception as e:
            logger.error("market_ws_error", error=str(e))
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
                delay = self._reconnect_delay  # Reset on successful connect

                # Resubscribe to all assets
                if self._subscribed_assets:
                    assets = list(self._subscribed_assets)
                    self._subscribed_assets.clear()  # Will be re-added by subscribe()
                    await self.subscribe(assets)

                await self._listen_loop()

            except Exception as e:
                logger.error("market_ws_connection_failed", error=str(e))

            if not self._is_running:
                break

            logger.info("market_ws_reconnecting", delay_s=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_reconnect_delay)

    def start(self, asset_ids: list[str] | None = None) -> asyncio.Task:
        """Start the WebSocket connection and message loop."""
        self._is_running = True
        if asset_ids:
            self._subscribed_assets = set(asset_ids)

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
        logger.info("market_ws_stopped")

    def get_stats(self) -> dict[str, Any]:
        return {
            "is_connected": self.is_connected,
            "is_stale": self.is_stale,
            "subscribed_assets": len(self._subscribed_assets),
            "message_count": self._message_count,
            "seconds_since_last_message": self.seconds_since_last_message,
            "books_count": len(self._books),
        }

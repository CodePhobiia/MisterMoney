"""Live data recorder — records market data for backtest replay from §16.

"A serious Polymarket MM bot needs our own live recorder.
No public historical L2 order-book replay endpoint exists."

Records:
- Book snapshots and deltas
- Trades
- Tick size changes
- Our quote intents and results
- Feature vectors
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from pmm1.state.books import OrderBook
from pmm1.storage.parquet import ParquetWriter

logger = structlog.get_logger(__name__)


class LiveRecorder:
    """Records all market data needed for backtesting.

    Writes to both Parquet (structured) and JSONL (raw events) formats.
    """

    def __init__(
        self,
        base_dir: str = "./data/recordings",
        parquet_writer: ParquetWriter | None = None,
        flush_interval_s: float = 60.0,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._parquet = parquet_writer or ParquetWriter(str(self._base_dir / "parquet"))
        self._flush_interval = flush_interval_s

        # Raw event buffers (JSONL)
        self._event_buffers: dict[str, list[str]] = {}
        self._is_recording = False
        self._task: asyncio.Task | None = None
        self._record_count: int = 0
        self._session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    def _get_jsonl_path(self, event_type: str) -> Path:
        """Get JSONL file path for an event type."""
        now = datetime.now(timezone.utc)
        date_dir = now.strftime("%Y-%m-%d")
        path = self._base_dir / "jsonl" / event_type / date_dir / f"{self._session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _write_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Write a raw event to JSONL buffer."""
        data["_ts"] = time.time()
        data["_type"] = event_type
        line = json.dumps(data, default=str)

        if event_type not in self._event_buffers:
            self._event_buffers[event_type] = []
        self._event_buffers[event_type].append(line)
        self._record_count += 1

    def record_book_snapshot(
        self,
        token_id: str,
        book: OrderBook,
    ) -> None:
        """Record a full order book snapshot."""
        if not self._is_recording:
            return

        bids = book.get_bids(20)
        asks = book.get_asks(20)

        # Raw JSONL
        self._write_event("book_snapshot", {
            "token_id": token_id,
            "bids": [{"price": str(l.price), "size": str(l.size)} for l in bids],
            "asks": [{"price": str(l.price), "size": str(l.size)} for l in asks],
            "tick_size": str(book.tick_size),
            "sequence": book.sequence,
        })

        # Structured parquet
        mid = book.get_midpoint() or 0.0
        micro = book.get_microprice() or 0.0
        spread = book.get_spread() or 0.0

        self._parquet.record_book_snapshot(
            token_id=token_id,
            best_bid=bids[0].price_float if bids else 0.0,
            best_ask=asks[0].price_float if asks else 0.0,
            bid_depth_2c=book.get_depth_within(2.0, "bid"),
            ask_depth_2c=book.get_depth_within(2.0, "ask"),
            midpoint=mid,
            microprice=micro,
            spread=spread,
            imbalance=book.get_imbalance(),
            tick_size=float(book.tick_size),
        )

    def record_book_delta(
        self,
        token_id: str,
        changes: list[dict[str, Any]],
        side: str,
    ) -> None:
        """Record an incremental book delta."""
        if not self._is_recording:
            return

        self._write_event("book_delta", {
            "token_id": token_id,
            "changes": changes,
            "side": side,
        })

    def record_trade(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        is_ours: bool = False,
    ) -> None:
        """Record a trade."""
        if not self._is_recording:
            return

        self._write_event("trade", {
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side,
            "is_ours": is_ours,
        })

        self._parquet.record_trade(token_id, price, size, side, is_ours)

    def record_tick_size_change(
        self,
        token_id: str,
        old_tick: str,
        new_tick: str,
    ) -> None:
        """Record a tick size change event."""
        if not self._is_recording:
            return

        self._write_event("tick_size_change", {
            "token_id": token_id,
            "old_tick_size": old_tick,
            "new_tick_size": new_tick,
        })

    def record_quote_intent(
        self,
        token_id: str,
        bid_price: float | None,
        bid_size: float | None,
        ask_price: float | None,
        ask_size: float | None,
        reservation_price: float,
        fair_value: float,
        half_spread: float,
        inventory: float,
    ) -> None:
        """Record our quote intent."""
        if not self._is_recording:
            return

        self._write_event("quote_intent", {
            "token_id": token_id,
            "bid_price": bid_price,
            "bid_size": bid_size,
            "ask_price": ask_price,
            "ask_size": ask_size,
            "reservation_price": reservation_price,
            "fair_value": fair_value,
            "half_spread": half_spread,
            "inventory": inventory,
        })

        self._parquet.record_quote_intent(
            token_id, bid_price, bid_size, ask_price, ask_size,
            reservation_price, fair_value, half_spread, inventory,
        )

    def record_features(self, token_id: str, features: dict[str, float]) -> None:
        """Record a feature vector."""
        if not self._is_recording:
            return

        self._write_event("features", {"token_id": token_id, **features})
        self._parquet.record_features(token_id, features)

    def _flush_jsonl(self) -> int:
        """Flush JSONL buffers to disk."""
        total = 0
        for event_type, lines in self._event_buffers.items():
            if not lines:
                continue
            path = self._get_jsonl_path(event_type)
            with open(path, "a") as f:
                for line in lines:
                    f.write(line + "\n")
            total += len(lines)

        self._event_buffers.clear()
        return total

    def flush(self) -> int:
        """Flush all buffers."""
        jsonl_count = self._flush_jsonl()
        parquet_count = self._parquet.flush_all()
        total = jsonl_count + parquet_count
        if total > 0:
            logger.debug("recorder_flushed", jsonl=jsonl_count, parquet=parquet_count)
        return total

    async def _flush_loop(self) -> None:
        """Periodic flush loop."""
        while self._is_recording:
            await asyncio.sleep(self._flush_interval)
            self.flush()

    def start(self) -> asyncio.Task | None:
        """Start recording."""
        self._is_recording = True
        self._task = asyncio.create_task(self._flush_loop())
        logger.info("recorder_started", session=self._session_id, base_dir=str(self._base_dir))
        return self._task

    async def stop(self) -> None:
        """Stop recording and flush remaining data."""
        self._is_recording = False
        self.flush()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("recorder_stopped", total_records=self._record_count)

    def get_stats(self) -> dict[str, Any]:
        return {
            "is_recording": self._is_recording,
            "session_id": self._session_id,
            "total_records": self._record_count,
            "buffered_events": {k: len(v) for k, v in self._event_buffers.items()},
            "parquet_stats": self._parquet.get_stats(),
        }

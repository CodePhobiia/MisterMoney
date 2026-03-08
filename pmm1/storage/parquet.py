"""Parquet writer — research data storage for replay/backtest from §9."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

try:
    import polars as pl
except ImportError:
    pl = None  # type: ignore


class ParquetWriter:
    """Writes structured data to Parquet files for research and backtesting.

    Organizes files by type and date:
        {base_dir}/{data_type}/{YYYY-MM-DD}/{HH}.parquet

    Data types:
    - book_snapshots: Order book snapshots
    - trades: All trades observed
    - quotes: Our quote intents and outcomes
    - features: Feature vectors
    - orders: Our order lifecycle events
    - fills: Our fills
    """

    def __init__(self, base_dir: str = "./data/parquet") -> None:
        self._base_dir = Path(base_dir)
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._flush_pending: set[str] = set()
        self._buffer_limits: dict[str, int] = {
            "book_snapshots": 1000,
            "trades": 500,
            "quotes": 200,
            "features": 500,
            "orders": 100,
            "fills": 100,
        }
        self._total_written: dict[str, int] = {}

    def _get_path(self, data_type: str, ts: float | None = None) -> Path:
        """Get file path for a data type at a given timestamp."""
        now = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
        date_dir = now.strftime("%Y-%m-%d")
        hour_file = now.strftime("%H")
        path = self._base_dir / data_type / date_dir / f"{hour_file}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def buffer(self, data_type: str, record: dict[str, Any]) -> None:
        """Buffer a record. Auto-flushes when buffer limit is reached."""
        if data_type not in self._buffers:
            self._buffers[data_type] = []

        # Add timestamp if not present
        if "timestamp" not in record:
            record["timestamp"] = time.time()

        self._buffers[data_type].append(record)

        limit = self._buffer_limits.get(data_type, 500)
        if len(self._buffers[data_type]) >= limit:
            self._flush_pending.add(data_type)  # Mark for async flush

    def flush(self, data_type: str | None = None) -> int:
        """Flush buffered records to Parquet files.

        Args:
            data_type: Specific type to flush, or None for all.

        Returns:
            Number of records written.
        """
        if pl is None:
            logger.warning("polars_not_installed_cannot_flush")
            return 0

        types_to_flush = [data_type] if data_type else list(self._buffers.keys())
        total = 0

        for dt in types_to_flush:
            records = self._buffers.get(dt, [])
            if not records:
                continue

            try:
                df = pl.DataFrame(records)
                path = self._get_path(dt)

                # Append to existing file if it exists
                if path.exists():
                    existing = pl.read_parquet(path)
                    df = pl.concat([existing, df])

                df.write_parquet(path, compression="zstd")
                count = len(records)
                total += count
                self._total_written[dt] = self._total_written.get(dt, 0) + count
                self._buffers[dt] = []

                logger.debug(
                    "parquet_flushed",
                    data_type=dt,
                    records=count,
                    path=str(path),
                )
            except Exception as e:
                logger.error("parquet_flush_error", data_type=dt, error=str(e))

        return total

    def flush_all(self) -> int:
        """Flush all buffers."""
        return self.flush()

    # ── Convenience methods ──

    def record_book_snapshot(
        self,
        token_id: str,
        best_bid: float,
        best_ask: float,
        bid_depth_2c: float,
        ask_depth_2c: float,
        midpoint: float,
        microprice: float,
        spread: float,
        imbalance: float,
        tick_size: float,
    ) -> None:
        """Record an order book snapshot."""
        self.buffer("book_snapshots", {
            "token_id": token_id,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_depth_2c": bid_depth_2c,
            "ask_depth_2c": ask_depth_2c,
            "midpoint": midpoint,
            "microprice": microprice,
            "spread": spread,
            "imbalance": imbalance,
            "tick_size": tick_size,
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
        self.buffer("trades", {
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side,
            "is_ours": is_ours,
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
        """Record a quote intent."""
        self.buffer("quotes", {
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

    def record_features(
        self,
        token_id: str,
        features: dict[str, float],
    ) -> None:
        """Record a feature vector."""
        record = {"token_id": token_id, **features}
        self.buffer("features", record)

    def record_order_event(
        self,
        order_id: str,
        token_id: str,
        event: str,  # created, submitted, filled, canceled, etc.
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record an order lifecycle event."""
        self.buffer("orders", {
            "order_id": order_id,
            "token_id": token_id,
            "event": event,
            **(details or {}),
        })

    def record_fill(
        self,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        fee: float = 0.0,
    ) -> None:
        """Record a fill."""
        self.buffer("fills", {
            "order_id": order_id,
            "token_id": token_id,
            "side": side,
            "price": price,
            "size": size,
            "fee": fee,
        })

    async def flush_async(self, data_type: str | None = None) -> int:
        """Async wrapper — runs disk I/O in thread pool."""
        import asyncio
        return await asyncio.to_thread(self.flush, data_type)

    async def flush_pending(self) -> int:
        """Flush any pending buffers asynchronously."""
        if not self._flush_pending:
            return 0
        pending = list(self._flush_pending)
        self._flush_pending.clear()
        total = 0
        for dt in pending:
            total += await self.flush_async(dt)
        return total

    def get_stats(self) -> dict[str, Any]:
        return {
            "buffered": {k: len(v) for k, v in self._buffers.items()},
            "total_written": self._total_written,
            "base_dir": str(self._base_dir),
        }

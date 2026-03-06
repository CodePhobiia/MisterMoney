"""PostgreSQL storage — durable events, orders, fills, positions, PnL from §9."""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore


# Database schema
SCHEMA_SQL = """
-- Orders table
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    order_id TEXT UNIQUE NOT NULL,
    token_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    event_id TEXT DEFAULT '',
    side TEXT NOT NULL,
    price NUMERIC NOT NULL,
    original_size NUMERIC NOT NULL,
    filled_size NUMERIC DEFAULT 0,
    status TEXT NOT NULL,
    order_type TEXT DEFAULT 'GTC',
    strategy TEXT DEFAULT 'mm',
    neg_risk BOOLEAN DEFAULT FALSE,
    post_only BOOLEAN DEFAULT TRUE,
    expiration BIGINT DEFAULT 0,
    error_msg TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    submitted_at TIMESTAMPTZ,
    filled_at TIMESTAMPTZ,
    canceled_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_orders_token_id ON orders(token_id);
CREATE INDEX IF NOT EXISTS idx_orders_condition_id ON orders(condition_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);

-- Fills / trades table
CREATE TABLE IF NOT EXISTS fills (
    id SERIAL PRIMARY KEY,
    order_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price NUMERIC NOT NULL,
    size NUMERIC NOT NULL,
    fee NUMERIC DEFAULT 0,
    transaction_hash TEXT DEFAULT '',
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_fills_token_id ON fills(token_id);
CREATE INDEX IF NOT EXISTS idx_fills_timestamp ON fills(timestamp);

-- Positions snapshot table
CREATE TABLE IF NOT EXISTS positions (
    id SERIAL PRIMARY KEY,
    condition_id TEXT NOT NULL,
    token_id_yes TEXT NOT NULL,
    token_id_no TEXT NOT NULL,
    event_id TEXT DEFAULT '',
    yes_size NUMERIC DEFAULT 0,
    no_size NUMERIC DEFAULT 0,
    yes_avg_price NUMERIC DEFAULT 0,
    no_avg_price NUMERIC DEFAULT 0,
    yes_cost_basis NUMERIC DEFAULT 0,
    no_cost_basis NUMERIC DEFAULT 0,
    realized_pnl NUMERIC DEFAULT 0,
    neg_risk BOOLEAN DEFAULT FALSE,
    snapshot_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_positions_condition_id ON positions(condition_id);
CREATE INDEX IF NOT EXISTS idx_positions_snapshot_at ON positions(snapshot_at);

-- PnL ledger
CREATE TABLE IF NOT EXISTS pnl_ledger (
    id SERIAL PRIMARY KEY,
    condition_id TEXT NOT NULL,
    event_id TEXT DEFAULT '',
    pnl_type TEXT NOT NULL,  -- spread_capture, adverse_selection, inventory_carry, arb, rebate, reward, slippage
    amount NUMERIC NOT NULL,
    details JSONB DEFAULT '{}',
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pnl_type ON pnl_ledger(pnl_type);
CREATE INDEX IF NOT EXISTS idx_pnl_timestamp ON pnl_ledger(timestamp);

-- Bot events / audit log
CREATE TABLE IF NOT EXISTS bot_events (
    id SERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    details JSONB DEFAULT '{}',
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bot_events_type ON bot_events(event_type);
CREATE INDEX IF NOT EXISTS idx_bot_events_timestamp ON bot_events(timestamp);

-- Market metadata cache
CREATE TABLE IF NOT EXISTS market_metadata (
    condition_id TEXT PRIMARY KEY,
    event_id TEXT DEFAULT '',
    question TEXT DEFAULT '',
    slug TEXT DEFAULT '',
    token_id_yes TEXT DEFAULT '',
    token_id_no TEXT DEFAULT '',
    neg_risk BOOLEAN DEFAULT FALSE,
    end_date TIMESTAMPTZ,
    metadata JSONB DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Daily NAV snapshots
CREATE TABLE IF NOT EXISTS daily_nav (
    id SERIAL PRIMARY KEY,
    nav NUMERIC NOT NULL,
    usdc_balance NUMERIC DEFAULT 0,
    total_position_value NUMERIC DEFAULT 0,
    daily_pnl NUMERIC DEFAULT 0,
    drawdown_pct NUMERIC DEFAULT 0,
    snapshot_date DATE NOT NULL,
    snapshot_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_daily_nav_date ON daily_nav(snapshot_date);
"""


class PostgresStore:
    """PostgreSQL-backed durable storage for PMM-1."""

    def __init__(self, dsn: str = "postgresql://pmm1:pmm1@localhost:5432/pmm1") -> None:
        self._dsn = dsn
        self._pool: Any = None

    async def connect(self) -> None:
        """Connect to PostgreSQL and initialize schema."""
        if asyncpg is None:
            raise ImportError("asyncpg is not installed")
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        # Create schema
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        logger.info("postgres_connected", dsn=self._dsn.split("@")[-1])

    async def close(self) -> None:
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            logger.info("postgres_disconnected")

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            await self.connect()
        return self._pool

    # ── Orders ──

    async def insert_order(self, order: dict[str, Any]) -> None:
        """Insert a new order record."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO orders (order_id, token_id, condition_id, event_id, side,
                    price, original_size, filled_size, status, order_type, strategy,
                    neg_risk, post_only, expiration)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                ON CONFLICT (order_id) DO UPDATE SET
                    filled_size = EXCLUDED.filled_size,
                    status = EXCLUDED.status,
                    updated_at = NOW()
                """,
                order.get("order_id", ""),
                order.get("token_id", ""),
                order.get("condition_id", ""),
                order.get("event_id", ""),
                order.get("side", ""),
                float(order.get("price", 0)),
                float(order.get("original_size", 0)),
                float(order.get("filled_size", 0)),
                order.get("status", "SUBMITTED"),
                order.get("order_type", "GTC"),
                order.get("strategy", "mm"),
                order.get("neg_risk", False),
                order.get("post_only", True),
                order.get("expiration", 0),
            )

    async def update_order_status(
        self, order_id: str, status: str, filled_size: float | None = None
    ) -> None:
        """Update order status."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            if filled_size is not None:
                await conn.execute(
                    """
                    UPDATE orders SET status = $1, filled_size = $2, updated_at = NOW()
                    WHERE order_id = $3
                    """,
                    status, filled_size, order_id,
                )
            else:
                await conn.execute(
                    "UPDATE orders SET status = $1, updated_at = NOW() WHERE order_id = $2",
                    status, order_id,
                )

    async def get_recent_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get recent orders."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT $1", limit
            )
            return [dict(r) for r in rows]

    # ── Fills ──

    async def insert_fill(self, fill: dict[str, Any]) -> None:
        """Record a fill/trade."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO fills (order_id, token_id, condition_id, side, price, size, fee, transaction_hash)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                fill.get("order_id", ""),
                fill.get("token_id", ""),
                fill.get("condition_id", ""),
                fill.get("side", ""),
                float(fill.get("price", 0)),
                float(fill.get("size", 0)),
                float(fill.get("fee", 0)),
                fill.get("transaction_hash", ""),
            )

    async def get_fills(
        self, condition_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Get recent fills."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            if condition_id:
                rows = await conn.fetch(
                    "SELECT * FROM fills WHERE condition_id = $1 ORDER BY timestamp DESC LIMIT $2",
                    condition_id, limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM fills ORDER BY timestamp DESC LIMIT $1", limit
                )
            return [dict(r) for r in rows]

    # ── Positions ──

    async def snapshot_position(self, position: dict[str, Any]) -> None:
        """Save a position snapshot."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO positions (condition_id, token_id_yes, token_id_no, event_id,
                    yes_size, no_size, yes_avg_price, no_avg_price,
                    yes_cost_basis, no_cost_basis, realized_pnl, neg_risk)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                position.get("condition_id", ""),
                position.get("token_id_yes", ""),
                position.get("token_id_no", ""),
                position.get("event_id", ""),
                float(position.get("yes_size", 0)),
                float(position.get("no_size", 0)),
                float(position.get("yes_avg_price", 0)),
                float(position.get("no_avg_price", 0)),
                float(position.get("yes_cost_basis", 0)),
                float(position.get("no_cost_basis", 0)),
                float(position.get("realized_pnl", 0)),
                position.get("neg_risk", False),
            )

    # ── PnL Ledger ──

    async def record_pnl(
        self,
        condition_id: str,
        pnl_type: str,
        amount: float,
        event_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record a PnL entry."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pnl_ledger (condition_id, event_id, pnl_type, amount, details)
                VALUES ($1, $2, $3, $4, $5)
                """,
                condition_id, event_id, pnl_type, amount,
                json.dumps(details or {}),
            )

    async def get_daily_pnl(self, date: str | None = None) -> float:
        """Get total PnL for a day."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            if date:
                row = await conn.fetchrow(
                    "SELECT COALESCE(SUM(amount), 0) as total FROM pnl_ledger WHERE timestamp::date = $1::date",
                    date,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT COALESCE(SUM(amount), 0) as total FROM pnl_ledger WHERE timestamp::date = CURRENT_DATE"
                )
            return float(row["total"]) if row else 0.0

    # ── Bot Events ──

    async def log_event(self, event_type: str, details: dict[str, Any] | None = None) -> None:
        """Log a bot event."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO bot_events (event_type, details) VALUES ($1, $2)",
                event_type, json.dumps(details or {}),
            )

    # ── Daily NAV ──

    async def save_daily_nav(
        self,
        nav: float,
        usdc_balance: float = 0,
        position_value: float = 0,
        daily_pnl: float = 0,
        drawdown_pct: float = 0,
    ) -> None:
        """Save daily NAV snapshot."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO daily_nav (nav, usdc_balance, total_position_value, daily_pnl, drawdown_pct, snapshot_date)
                VALUES ($1, $2, $3, $4, $5, CURRENT_DATE)
                """,
                nav, usdc_balance, position_value, daily_pnl, drawdown_pct,
            )

    # ── Market Metadata ──

    async def upsert_market_metadata(self, metadata: dict[str, Any]) -> None:
        """Insert or update market metadata."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO market_metadata (condition_id, event_id, question, slug,
                    token_id_yes, token_id_no, neg_risk, end_date, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (condition_id) DO UPDATE SET
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                metadata.get("condition_id", ""),
                metadata.get("event_id", ""),
                metadata.get("question", ""),
                metadata.get("slug", ""),
                metadata.get("token_id_yes", ""),
                metadata.get("token_id_no", ""),
                metadata.get("neg_risk", False),
                metadata.get("end_date"),
                json.dumps(metadata),
            )

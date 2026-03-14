"""PostgreSQL storage — durable events, orders, fills, positions, PnL from §9."""

from __future__ import annotations

import json
from typing import Any

import structlog

from pmm1.storage.spine import (
    SPINE_SCHEMA_SQL,
    ConfigSnapshotRecord,
    ModelSnapshotRecord,
    SpineEvent,
)

logger = structlog.get_logger(__name__)

try:
    import asyncpg
except ImportError:
    asyncpg = None


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
    pnl_type TEXT NOT NULL,  -- spread/adverse/inventory/arb/rebate/reward/slippage
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
        # Create legacy and spine schemas.
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
            await conn.execute(SPINE_SCHEMA_SQL)
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
                INSERT INTO fills (
                    order_id, token_id, condition_id,
                    side, price, size, fee, transaction_hash
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
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
                    "SELECT COALESCE(SUM(amount), 0) as total "
                    "FROM pnl_ledger "
                    "WHERE timestamp::date = $1::date",
                    date,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT COALESCE(SUM(amount), 0) as total "
                    "FROM pnl_ledger "
                    "WHERE timestamp::date = CURRENT_DATE"
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

    # Data spine

    async def append_spine_event(self, event: SpineEvent | dict[str, Any]) -> None:
        """Append a canonical data spine event.

        Duplicate delivery is tolerated by ignoring repeated event IDs.
        """
        payload = event if isinstance(event, SpineEvent) else SpineEvent.model_validate(event)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO event_log (
                    event_id, ts_event, ts_ingest, event_type, controller, strategy,
                    session_id, git_sha, config_hash, run_stage,
                    condition_id, token_id, order_id, payload_json
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, $8, $9, $10,
                    $11, $12, $13, $14
                )
                ON CONFLICT (event_id) DO NOTHING
                """,
                payload.event_id,
                payload.ts_event,
                payload.ts_ingest,
                payload.event_type,
                payload.controller,
                payload.strategy,
                payload.session_id,
                payload.git_sha,
                payload.config_hash,
                payload.run_stage,
                payload.condition_id,
                payload.token_id,
                payload.order_id,
                json.dumps(payload.payload_json),
            )

    async def count_spine_events(
        self,
        *,
        event_type: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """Count canonical spine events, optionally filtered by type or session."""
        pool = await self._ensure_pool()
        clauses: list[str] = []
        params: list[Any] = []
        if event_type is not None:
            clauses.append(f"event_type = ${len(params) + 1}")
            params.append(event_type)
        if session_id is not None:
            clauses.append(f"session_id = ${len(params) + 1}")
            params.append(session_id)
        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT COUNT(*) AS count FROM event_log{where_sql}"
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
            return int(row["count"]) if row else 0

    async def get_recent_spine_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch recent spine events for validation and diagnostics."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM event_log
                ORDER BY ts_ingest DESC
                LIMIT $1
                """,
                limit,
            )
            return [dict(row) for row in rows]

    async def get_spine_events(
        self,
        *,
        event_types: list[str] | None = None,
        order_by: str = "ts_ingest ASC",
    ) -> list[dict[str, Any]]:
        """Fetch canonical spine events ordered for historical rebuilds."""
        pool = await self._ensure_pool()
        clauses: list[str] = []
        params: list[Any] = []
        if event_types:
            clauses.append(f"event_type = ANY(${len(params) + 1}::text[])")
            params.append(event_types)
        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM event_log{where_sql} ORDER BY {order_by}"
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]

    async def get_legacy_orders(self) -> list[dict[str, Any]]:
        """Fetch legacy order rows for backfill."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM orders ORDER BY created_at ASC")
            return [dict(row) for row in rows]

    async def get_legacy_fills(self) -> list[dict[str, Any]]:
        """Fetch legacy fill rows for backfill."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM fills ORDER BY timestamp ASC")
            return [dict(row) for row in rows]

    async def get_legacy_bot_events(self) -> list[dict[str, Any]]:
        """Fetch legacy bot event rows for backfill."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM bot_events ORDER BY timestamp ASC")
            return [dict(row) for row in rows]

    async def mark_order_fact_event_applied(self, event_id: str) -> bool:
        """Record that an event has been applied to order_fact once."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO order_fact_applied_event (event_id)
                VALUES ($1)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                event_id,
            )
            return row is not None

    async def get_order_fact(self, order_id: str) -> dict[str, Any] | None:
        """Fetch one materialized order_fact row."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM order_fact
                WHERE order_id = $1
                """,
                order_id,
            )
            return dict(row) if row else None

    async def _upsert_order_fact_with_conn(self, conn: Any, record: dict[str, Any]) -> None:
        payload = dict(record)
        await conn.execute(
            """
            INSERT INTO order_fact (
                order_id, client_order_id, controller, strategy, condition_id, token_id, side,
                submit_requested_at, submit_ack_at, live_at, canceled_at, filled_at,
                terminal_status, original_size, filled_size, remaining_size, price,
                post_only, neg_risk, order_type, replacement_reason_json,
                replaced_from_order_id, replace_count, time_live_sec,
                config_hash, git_sha, session_id, run_stage, last_event_id, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7,
                $8, $9, $10, $11, $12,
                $13, $14, $15, $16, $17,
                $18, $19, $20, $21,
                $22, $23, $24,
                $25, $26, $27, $28, $29, $30
            )
            ON CONFLICT (order_id) DO UPDATE SET
                client_order_id = EXCLUDED.client_order_id,
                controller = EXCLUDED.controller,
                strategy = EXCLUDED.strategy,
                condition_id = EXCLUDED.condition_id,
                token_id = EXCLUDED.token_id,
                side = EXCLUDED.side,
                submit_requested_at = EXCLUDED.submit_requested_at,
                submit_ack_at = EXCLUDED.submit_ack_at,
                live_at = EXCLUDED.live_at,
                canceled_at = EXCLUDED.canceled_at,
                filled_at = EXCLUDED.filled_at,
                terminal_status = EXCLUDED.terminal_status,
                original_size = EXCLUDED.original_size,
                filled_size = EXCLUDED.filled_size,
                remaining_size = EXCLUDED.remaining_size,
                price = EXCLUDED.price,
                post_only = EXCLUDED.post_only,
                neg_risk = EXCLUDED.neg_risk,
                order_type = EXCLUDED.order_type,
                replacement_reason_json = EXCLUDED.replacement_reason_json,
                replaced_from_order_id = EXCLUDED.replaced_from_order_id,
                replace_count = EXCLUDED.replace_count,
                time_live_sec = EXCLUDED.time_live_sec,
                config_hash = EXCLUDED.config_hash,
                git_sha = EXCLUDED.git_sha,
                session_id = EXCLUDED.session_id,
                run_stage = EXCLUDED.run_stage,
                last_event_id = EXCLUDED.last_event_id,
                updated_at = EXCLUDED.updated_at
            """,
            payload["order_id"],
            payload.get("client_order_id", ""),
            payload.get("controller", ""),
            payload.get("strategy", ""),
            payload.get("condition_id", ""),
            payload.get("token_id", ""),
            payload.get("side", ""),
            payload.get("submit_requested_at"),
            payload.get("submit_ack_at"),
            payload.get("live_at"),
            payload.get("canceled_at"),
            payload.get("filled_at"),
            payload.get("terminal_status", ""),
            float(payload.get("original_size", 0.0) or 0.0),
            float(payload.get("filled_size", 0.0) or 0.0),
            float(payload.get("remaining_size", 0.0) or 0.0),
            float(payload.get("price", 0.0) or 0.0),
            bool(payload.get("post_only", True)),
            bool(payload.get("neg_risk", False)),
            payload.get("order_type", ""),
            json.dumps(payload.get("replacement_reason_json", [])),
            payload.get("replaced_from_order_id"),
            int(payload.get("replace_count", 0) or 0),
            payload.get("time_live_sec"),
            payload.get("config_hash", ""),
            payload.get("git_sha", ""),
            payload.get("session_id", ""),
            payload.get("run_stage", ""),
            payload.get("last_event_id", ""),
            payload.get("updated_at"),
        )

    async def upsert_order_fact(self, record: dict[str, Any]) -> None:
        """Insert or update one materialized order_fact row."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await self._upsert_order_fact_with_conn(conn, record)

    async def apply_order_fact_update(self, event_id: str, record: dict[str, Any]) -> bool:
        """Atomically mark an event applied and upsert the derived order_fact row."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO order_fact_applied_event (event_id)
                    VALUES ($1)
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING event_id
                    """,
                    event_id,
                )
                if row is None:
                    return False
                await self._upsert_order_fact_with_conn(conn, record)
                return True

    async def reset_order_fact_materialization(self) -> None:
        """Clear order_fact and its applied-event guard table for a full rebuild."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE order_fact, order_fact_applied_event")

    async def get_recent_order_facts(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch recent order_fact rows for validation and diagnostics."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM order_fact
                ORDER BY updated_at DESC
                LIMIT $1
                """,
                limit,
            )
            return [dict(row) for row in rows]

    async def reset_quote_fact_materialization(self) -> None:
        """Clear quote_fact and its applied-event guard table for a full rebuild."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE quote_fact, quote_fact_applied_event")

    async def get_quote_fact(self, quote_intent_id: str) -> dict[str, Any] | None:
        """Fetch one materialized quote_fact row."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM quote_fact
                WHERE quote_intent_id = $1
                """,
                quote_intent_id,
            )
            return dict(row) if row else None

    async def _upsert_quote_fact_with_conn(self, conn: Any, record: dict[str, Any]) -> None:
        payload = dict(record)
        await conn.execute(
            """
            INSERT INTO quote_fact (
                quote_intent_id, cycle_id, controller, strategy, condition_id, token_id, order_id,
                side, intended_price, intended_size, submitted_price, submitted_size,
                suppression_reason_json, risk_adjustment_reason_json, book_quality_json,
                expected_spread_ev_usdc, expected_reward_ev_usdc, expected_rebate_ev_usdc,
                expected_total_ev_usdc, fill_prob_30s, became_live, filled_any, filled_full,
                terminal_status, config_hash, git_sha,
                session_id, run_stage, last_event_id, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7,
                $8, $9, $10, $11, $12,
                $13, $14, $15,
                $16, $17, $18,
                $19, $20, $21, $22, $23,
                $24, $25, $26, $27, $28, $29, $30
            )
            ON CONFLICT (quote_intent_id) DO UPDATE SET
                cycle_id = EXCLUDED.cycle_id,
                controller = EXCLUDED.controller,
                strategy = EXCLUDED.strategy,
                condition_id = EXCLUDED.condition_id,
                token_id = EXCLUDED.token_id,
                order_id = EXCLUDED.order_id,
                side = EXCLUDED.side,
                intended_price = EXCLUDED.intended_price,
                intended_size = EXCLUDED.intended_size,
                submitted_price = EXCLUDED.submitted_price,
                submitted_size = EXCLUDED.submitted_size,
                suppression_reason_json = EXCLUDED.suppression_reason_json,
                risk_adjustment_reason_json = EXCLUDED.risk_adjustment_reason_json,
                book_quality_json = EXCLUDED.book_quality_json,
                expected_spread_ev_usdc = EXCLUDED.expected_spread_ev_usdc,
                expected_reward_ev_usdc = EXCLUDED.expected_reward_ev_usdc,
                expected_rebate_ev_usdc = EXCLUDED.expected_rebate_ev_usdc,
                expected_total_ev_usdc = EXCLUDED.expected_total_ev_usdc,
                fill_prob_30s = EXCLUDED.fill_prob_30s,
                became_live = EXCLUDED.became_live,
                filled_any = EXCLUDED.filled_any,
                filled_full = EXCLUDED.filled_full,
                terminal_status = EXCLUDED.terminal_status,
                config_hash = EXCLUDED.config_hash,
                git_sha = EXCLUDED.git_sha,
                session_id = EXCLUDED.session_id,
                run_stage = EXCLUDED.run_stage,
                last_event_id = EXCLUDED.last_event_id,
                updated_at = EXCLUDED.updated_at
            """,
            payload["quote_intent_id"],
            payload.get("cycle_id", ""),
            payload.get("controller", ""),
            payload.get("strategy", ""),
            payload.get("condition_id", ""),
            payload.get("token_id", ""),
            payload.get("order_id", ""),
            payload.get("side", ""),
            payload.get("intended_price"),
            payload.get("intended_size"),
            payload.get("submitted_price"),
            payload.get("submitted_size"),
            json.dumps(payload.get("suppression_reason_json", [])),
            json.dumps(payload.get("risk_adjustment_reason_json", [])),
            json.dumps(payload.get("book_quality_json", {})),
            payload.get("expected_spread_ev_usdc"),
            payload.get("expected_reward_ev_usdc"),
            payload.get("expected_rebate_ev_usdc"),
            payload.get("expected_total_ev_usdc"),
            payload.get("fill_prob_30s"),
            bool(payload.get("became_live", False)),
            bool(payload.get("filled_any", False)),
            bool(payload.get("filled_full", False)),
            payload.get("terminal_status", ""),
            payload.get("config_hash", ""),
            payload.get("git_sha", ""),
            payload.get("session_id", ""),
            payload.get("run_stage", ""),
            payload.get("last_event_id", ""),
            payload.get("updated_at"),
        )

    async def upsert_quote_fact(self, record: dict[str, Any]) -> None:
        """Insert or update one materialized quote_fact row."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await self._upsert_quote_fact_with_conn(conn, record)

    async def apply_quote_fact_update(self, event_id: str, record: dict[str, Any]) -> bool:
        """Atomically mark an event applied and upsert the derived quote_fact row."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO quote_fact_applied_event (event_id)
                    VALUES ($1)
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING event_id
                    """,
                    event_id,
                )
                if row is None:
                    return False
                await self._upsert_quote_fact_with_conn(conn, record)
                return True

    async def get_recent_quote_facts(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch recent quote_fact rows for validation and diagnostics."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM quote_fact
                ORDER BY updated_at DESC
                LIMIT $1
                """,
                limit,
            )
            return [dict(row) for row in rows]

    async def upsert_shadow_cycle_fact(self, record: dict[str, Any]) -> None:
        """Insert or update one derived shadow_cycle_fact row."""
        payload = dict(record)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO shadow_cycle_fact (
                    source_event_id, cycle_num, ts, ready_for_live, window_cycles,
                    ev_sample_count, reward_sample_count, churn_sample_count,
                    v1_market_count, pmm2_market_count, market_overlap_pct,
                    overlap_quote_distance_bps, v1_total_ev_usdc, pmm2_total_ev_usdc,
                    ev_delta_usdc, v1_reward_market_count, pmm2_reward_market_count,
                    reward_market_delta, v1_reward_ev_usdc, pmm2_reward_ev_usdc,
                    reward_ev_delta_usdc, v1_cancel_rate_per_order_min,
                    pmm2_cancel_rate_per_order_min, churn_delta_per_order_min,
                    gate_blockers_json, gate_diagnostics_json, v1_state_json, pmm2_plan_json,
                    controller, strategy, config_hash, git_sha, session_id, run_stage, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8,
                    $9, $10, $11,
                    $12, $13, $14,
                    $15, $16, $17,
                    $18, $19, $20,
                    $21, $22,
                    $23, $24,
                    $25, $26, $27, $28,
                    $29, $30, $31, $32, $33, $34, $35
                )
                ON CONFLICT (source_event_id) DO UPDATE SET
                    cycle_num = EXCLUDED.cycle_num,
                    ts = EXCLUDED.ts,
                    ready_for_live = EXCLUDED.ready_for_live,
                    window_cycles = EXCLUDED.window_cycles,
                    ev_sample_count = EXCLUDED.ev_sample_count,
                    reward_sample_count = EXCLUDED.reward_sample_count,
                    churn_sample_count = EXCLUDED.churn_sample_count,
                    v1_market_count = EXCLUDED.v1_market_count,
                    pmm2_market_count = EXCLUDED.pmm2_market_count,
                    market_overlap_pct = EXCLUDED.market_overlap_pct,
                    overlap_quote_distance_bps = EXCLUDED.overlap_quote_distance_bps,
                    v1_total_ev_usdc = EXCLUDED.v1_total_ev_usdc,
                    pmm2_total_ev_usdc = EXCLUDED.pmm2_total_ev_usdc,
                    ev_delta_usdc = EXCLUDED.ev_delta_usdc,
                    v1_reward_market_count = EXCLUDED.v1_reward_market_count,
                    pmm2_reward_market_count = EXCLUDED.pmm2_reward_market_count,
                    reward_market_delta = EXCLUDED.reward_market_delta,
                    v1_reward_ev_usdc = EXCLUDED.v1_reward_ev_usdc,
                    pmm2_reward_ev_usdc = EXCLUDED.pmm2_reward_ev_usdc,
                    reward_ev_delta_usdc = EXCLUDED.reward_ev_delta_usdc,
                    v1_cancel_rate_per_order_min = EXCLUDED.v1_cancel_rate_per_order_min,
                    pmm2_cancel_rate_per_order_min = EXCLUDED.pmm2_cancel_rate_per_order_min,
                    churn_delta_per_order_min = EXCLUDED.churn_delta_per_order_min,
                    gate_blockers_json = EXCLUDED.gate_blockers_json,
                    gate_diagnostics_json = EXCLUDED.gate_diagnostics_json,
                    v1_state_json = EXCLUDED.v1_state_json,
                    pmm2_plan_json = EXCLUDED.pmm2_plan_json,
                    controller = EXCLUDED.controller,
                    strategy = EXCLUDED.strategy,
                    config_hash = EXCLUDED.config_hash,
                    git_sha = EXCLUDED.git_sha,
                    session_id = EXCLUDED.session_id,
                    run_stage = EXCLUDED.run_stage,
                    updated_at = EXCLUDED.updated_at
                """,
                payload["source_event_id"],
                payload.get("cycle_num"),
                payload.get("ts"),
                bool(payload.get("ready_for_live", False)),
                payload.get("window_cycles"),
                payload.get("ev_sample_count"),
                payload.get("reward_sample_count"),
                payload.get("churn_sample_count"),
                payload.get("v1_market_count"),
                payload.get("pmm2_market_count"),
                payload.get("market_overlap_pct"),
                payload.get("overlap_quote_distance_bps"),
                payload.get("v1_total_ev_usdc"),
                payload.get("pmm2_total_ev_usdc"),
                payload.get("ev_delta_usdc"),
                payload.get("v1_reward_market_count"),
                payload.get("pmm2_reward_market_count"),
                payload.get("reward_market_delta"),
                payload.get("v1_reward_ev_usdc"),
                payload.get("pmm2_reward_ev_usdc"),
                payload.get("reward_ev_delta_usdc"),
                payload.get("v1_cancel_rate_per_order_min"),
                payload.get("pmm2_cancel_rate_per_order_min"),
                payload.get("churn_delta_per_order_min"),
                json.dumps(payload.get("gate_blockers_json", [])),
                json.dumps(payload.get("gate_diagnostics_json", {})),
                json.dumps(payload.get("v1_state_json", {})),
                json.dumps(payload.get("pmm2_plan_json", {})),
                payload.get("controller", ""),
                payload.get("strategy", ""),
                payload.get("config_hash", ""),
                payload.get("git_sha", ""),
                payload.get("session_id", ""),
                payload.get("run_stage", ""),
                payload.get("updated_at"),
            )

    async def reset_shadow_cycle_fact_materialization(self) -> None:
        """Clear shadow_cycle_fact for a full rebuild."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE shadow_cycle_fact")

    async def get_recent_shadow_cycle_facts(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch recent shadow_cycle_fact rows for validation and diagnostics."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM shadow_cycle_fact
                ORDER BY ts DESC
                LIMIT $1
                """,
                limit,
            )
            return [dict(row) for row in rows]

    async def upsert_canary_cycle_fact(self, record: dict[str, Any]) -> None:
        """Insert or update one derived canary_cycle_fact row."""
        payload = dict(record)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO canary_cycle_fact (
                    source_event_id, cycle_num, ts, canary_stage, live_capital_pct,
                    controlled_market_count, controlled_order_count,
                    realized_fills, realized_cancels,
                    realized_markout_1s, realized_markout_5s, reward_capture_count, rollback_flag,
                    promotion_eligible, incident_flag, summary_json,
                    controller, strategy, config_hash, git_sha, session_id, run_stage, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12, $13,
                    $14, $15, $16,
                    $17, $18, $19, $20, $21, $22, $23
                )
                ON CONFLICT (source_event_id) DO UPDATE SET
                    cycle_num = EXCLUDED.cycle_num,
                    ts = EXCLUDED.ts,
                    canary_stage = EXCLUDED.canary_stage,
                    live_capital_pct = EXCLUDED.live_capital_pct,
                    controlled_market_count = EXCLUDED.controlled_market_count,
                    controlled_order_count = EXCLUDED.controlled_order_count,
                    realized_fills = EXCLUDED.realized_fills,
                    realized_cancels = EXCLUDED.realized_cancels,
                    realized_markout_1s = EXCLUDED.realized_markout_1s,
                    realized_markout_5s = EXCLUDED.realized_markout_5s,
                    reward_capture_count = EXCLUDED.reward_capture_count,
                    rollback_flag = EXCLUDED.rollback_flag,
                    promotion_eligible = EXCLUDED.promotion_eligible,
                    incident_flag = EXCLUDED.incident_flag,
                    summary_json = EXCLUDED.summary_json,
                    controller = EXCLUDED.controller,
                    strategy = EXCLUDED.strategy,
                    config_hash = EXCLUDED.config_hash,
                    git_sha = EXCLUDED.git_sha,
                    session_id = EXCLUDED.session_id,
                    run_stage = EXCLUDED.run_stage,
                    updated_at = EXCLUDED.updated_at
                """,
                payload["source_event_id"],
                payload.get("cycle_num"),
                payload.get("ts"),
                payload.get("canary_stage", ""),
                payload.get("live_capital_pct", 0.0),
                payload.get("controlled_market_count"),
                payload.get("controlled_order_count"),
                payload.get("realized_fills"),
                payload.get("realized_cancels"),
                payload.get("realized_markout_1s"),
                payload.get("realized_markout_5s"),
                payload.get("reward_capture_count"),
                bool(payload.get("rollback_flag", False)),
                bool(payload.get("promotion_eligible", False)),
                bool(payload.get("incident_flag", False)),
                json.dumps(payload.get("summary_json", {})),
                payload.get("controller", ""),
                payload.get("strategy", ""),
                payload.get("config_hash", ""),
                payload.get("git_sha", ""),
                payload.get("session_id", ""),
                payload.get("run_stage", ""),
                payload.get("updated_at"),
            )

    async def reset_canary_cycle_fact_materialization(self) -> None:
        """Clear canary_cycle_fact for a full rebuild."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE canary_cycle_fact")

    async def get_recent_canary_cycle_facts(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch recent canary_cycle_fact rows for validation and diagnostics."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM canary_cycle_fact
                ORDER BY ts DESC
                LIMIT $1
                """,
                limit,
            )
            return [dict(row) for row in rows]

    async def upsert_fill_fact(self, record: dict[str, Any]) -> None:
        """Insert or update one derived fill_fact row."""
        payload = dict(record)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO fill_fact (
                    fill_id, order_id, condition_id, token_id, controller, strategy, side,
                    fill_ts, fill_price, fill_size, quote_intent_id, queue_ahead_estimate,
                    fill_prob_estimate, expected_spread_ev_usdc, expected_reward_ev_usdc,
                    expected_rebate_ev_usdc, markout_1s, markout_5s, markout_30s, markout_300s,
                    resolution_markout, realized_spread_capture, adverse_selection_estimate,
                    reward_eligible, scoring_flag, fee_usdc, dollar_value_usdc, mid_at_fill,
                    config_hash, git_sha, session_id, run_stage, source_event_id, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7,
                    $8, $9, $10, $11, $12,
                    $13, $14, $15, $16, $17, $18, $19, $20,
                    $21, $22, $23, $24, $25, $26, $27, $28, $29,
                    $30, $31, $32, $33, $34
                )
                ON CONFLICT (fill_id) DO UPDATE SET
                    order_id = EXCLUDED.order_id,
                    condition_id = EXCLUDED.condition_id,
                    token_id = EXCLUDED.token_id,
                    controller = EXCLUDED.controller,
                    strategy = EXCLUDED.strategy,
                    side = EXCLUDED.side,
                    fill_ts = EXCLUDED.fill_ts,
                    fill_price = EXCLUDED.fill_price,
                    fill_size = EXCLUDED.fill_size,
                    quote_intent_id = EXCLUDED.quote_intent_id,
                    queue_ahead_estimate = EXCLUDED.queue_ahead_estimate,
                    fill_prob_estimate = EXCLUDED.fill_prob_estimate,
                    expected_spread_ev_usdc = EXCLUDED.expected_spread_ev_usdc,
                    expected_reward_ev_usdc = EXCLUDED.expected_reward_ev_usdc,
                    expected_rebate_ev_usdc = EXCLUDED.expected_rebate_ev_usdc,
                    markout_1s = EXCLUDED.markout_1s,
                    markout_5s = EXCLUDED.markout_5s,
                    markout_30s = EXCLUDED.markout_30s,
                    markout_300s = EXCLUDED.markout_300s,
                    resolution_markout = EXCLUDED.resolution_markout,
                    realized_spread_capture = EXCLUDED.realized_spread_capture,
                    adverse_selection_estimate = EXCLUDED.adverse_selection_estimate,
                    reward_eligible = EXCLUDED.reward_eligible,
                    scoring_flag = EXCLUDED.scoring_flag,
                    fee_usdc = EXCLUDED.fee_usdc,
                    dollar_value_usdc = EXCLUDED.dollar_value_usdc,
                    mid_at_fill = EXCLUDED.mid_at_fill,
                    config_hash = EXCLUDED.config_hash,
                    git_sha = EXCLUDED.git_sha,
                    session_id = EXCLUDED.session_id,
                    run_stage = EXCLUDED.run_stage,
                    source_event_id = EXCLUDED.source_event_id,
                    updated_at = EXCLUDED.updated_at
                """,
                payload["fill_id"],
                payload.get("order_id", ""),
                payload.get("condition_id", ""),
                payload.get("token_id", ""),
                payload.get("controller", ""),
                payload.get("strategy", ""),
                payload.get("side", ""),
                payload.get("fill_ts"),
                float(payload.get("fill_price", 0.0) or 0.0),
                float(payload.get("fill_size", 0.0) or 0.0),
                payload.get("quote_intent_id", ""),
                payload.get("queue_ahead_estimate"),
                payload.get("fill_prob_estimate"),
                payload.get("expected_spread_ev_usdc"),
                payload.get("expected_reward_ev_usdc"),
                payload.get("expected_rebate_ev_usdc"),
                payload.get("markout_1s"),
                payload.get("markout_5s"),
                payload.get("markout_30s"),
                payload.get("markout_300s"),
                payload.get("resolution_markout"),
                payload.get("realized_spread_capture"),
                payload.get("adverse_selection_estimate"),
                bool(payload.get("reward_eligible", False)),
                bool(payload.get("scoring_flag", False)),
                payload.get("fee_usdc"),
                payload.get("dollar_value_usdc"),
                payload.get("mid_at_fill"),
                payload.get("config_hash", ""),
                payload.get("git_sha", ""),
                payload.get("session_id", ""),
                payload.get("run_stage", ""),
                payload.get("source_event_id", ""),
                payload.get("updated_at"),
            )

    async def reset_fill_fact_materialization(self) -> None:
        """Clear fill_fact for a full rebuild."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE fill_fact")

    async def get_recent_fill_facts(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch recent fill_fact rows for validation and diagnostics."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM fill_fact
                ORDER BY fill_ts DESC
                LIMIT $1
                """,
                limit,
            )
            return [dict(row) for row in rows]

    async def upsert_book_snapshot_fact(self, record: dict[str, Any]) -> None:
        """Insert or update one derived book_snapshot_fact row."""
        payload = dict(record)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO book_snapshot_fact (
                    snapshot_id, token_id, condition_id, ts, best_bid, best_ask, mid, spread,
                    spread_cents, depth_best_bid, depth_best_ask, depth_within_1c,
                    depth_within_2c, depth_within_5c, is_stale, tick_size,
                    controller, strategy, config_hash, git_sha, session_id,
                    run_stage, source_event_id, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8,
                    $9, $10, $11, $12,
                    $13, $14, $15, $16,
                    $17, $18, $19, $20, $21,
                    $22, $23, $24
                )
                ON CONFLICT (snapshot_id) DO UPDATE SET
                    token_id = EXCLUDED.token_id,
                    condition_id = EXCLUDED.condition_id,
                    ts = EXCLUDED.ts,
                    best_bid = EXCLUDED.best_bid,
                    best_ask = EXCLUDED.best_ask,
                    mid = EXCLUDED.mid,
                    spread = EXCLUDED.spread,
                    spread_cents = EXCLUDED.spread_cents,
                    depth_best_bid = EXCLUDED.depth_best_bid,
                    depth_best_ask = EXCLUDED.depth_best_ask,
                    depth_within_1c = EXCLUDED.depth_within_1c,
                    depth_within_2c = EXCLUDED.depth_within_2c,
                    depth_within_5c = EXCLUDED.depth_within_5c,
                    is_stale = EXCLUDED.is_stale,
                    tick_size = EXCLUDED.tick_size,
                    controller = EXCLUDED.controller,
                    strategy = EXCLUDED.strategy,
                    config_hash = EXCLUDED.config_hash,
                    git_sha = EXCLUDED.git_sha,
                    session_id = EXCLUDED.session_id,
                    run_stage = EXCLUDED.run_stage,
                    source_event_id = EXCLUDED.source_event_id,
                    updated_at = EXCLUDED.updated_at
                """,
                payload["snapshot_id"],
                payload.get("token_id", ""),
                payload.get("condition_id", ""),
                payload.get("ts"),
                payload.get("best_bid"),
                payload.get("best_ask"),
                payload.get("mid"),
                payload.get("spread"),
                payload.get("spread_cents"),
                payload.get("depth_best_bid"),
                payload.get("depth_best_ask"),
                payload.get("depth_within_1c"),
                payload.get("depth_within_2c"),
                payload.get("depth_within_5c"),
                bool(payload.get("is_stale", False)),
                payload.get("tick_size"),
                payload.get("controller", ""),
                payload.get("strategy", ""),
                payload.get("config_hash", ""),
                payload.get("git_sha", ""),
                payload.get("session_id", ""),
                payload.get("run_stage", ""),
                payload.get("source_event_id", ""),
                payload.get("updated_at"),
            )

    async def reset_book_snapshot_fact_materialization(self) -> None:
        """Clear book_snapshot_fact for a full rebuild."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE book_snapshot_fact")

    async def get_recent_book_snapshot_facts(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch recent book_snapshot_fact rows for validation and diagnostics."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM book_snapshot_fact
                ORDER BY ts DESC
                LIMIT $1
                """,
                limit,
            )
            return [dict(row) for row in rows]

    async def upsert_config_snapshot(
        self, snapshot: ConfigSnapshotRecord | dict[str, Any]
    ) -> None:
        """Persist a redacted config snapshot for replay lineage."""
        payload = (
            snapshot
            if isinstance(snapshot, ConfigSnapshotRecord)
            else ConfigSnapshotRecord.model_validate(snapshot)
        )
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO config_snapshot (config_hash, created_at, git_sha, config_json)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (config_hash) DO UPDATE SET
                    created_at = EXCLUDED.created_at,
                    git_sha = EXCLUDED.git_sha,
                    config_json = EXCLUDED.config_json
                """,
                payload.config_hash,
                payload.created_at,
                payload.git_sha,
                json.dumps(payload.config_json),
            )

    async def upsert_model_snapshot(
        self, snapshot: ModelSnapshotRecord | dict[str, Any]
    ) -> None:
        """Persist model and scorer lineage metadata."""
        payload = (
            snapshot
            if isinstance(snapshot, ModelSnapshotRecord)
            else ModelSnapshotRecord.model_validate(snapshot)
        )
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO model_snapshot (
                    model_id, component, version, params_json, trained_on_window, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (model_id) DO UPDATE SET
                    component = EXCLUDED.component,
                    version = EXCLUDED.version,
                    params_json = EXCLUDED.params_json,
                    trained_on_window = EXCLUDED.trained_on_window,
                    created_at = EXCLUDED.created_at
                """,
                payload.model_id,
                payload.component,
                payload.version,
                json.dumps(payload.params_json),
                json.dumps(payload.trained_on_window),
                payload.created_at,
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
                INSERT INTO daily_nav (
                    nav, usdc_balance, total_position_value,
                    daily_pnl, drawdown_pct, snapshot_date
                )
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

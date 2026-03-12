"""SQLite database module for PMM-2 data collection.

Async SQLite using aiosqlite for storing fills, book snapshots, scoring history, etc.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)


class Database:
    """Async SQLite database wrapper for PMM-2 data collection."""

    def __init__(self, db_path: str | Path) -> None:
        """Initialize database.

        Args:
            db_path: Path to SQLite database file (will be created if doesn't exist)
        """
        self.db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Initialize database connection and create tables from schema."""
        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Connect to database
        self._conn = await aiosqlite.connect(str(self.db_path))
        
        # Enable WAL mode for better concurrency
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.commit()

        # Load and execute schema
        schema_path = Path(__file__).parent / "schema.sql"
        if not schema_path.exists():
            logger.error("schema_file_not_found", path=str(schema_path))
            raise FileNotFoundError(f"Schema file not found: {schema_path}")

        with open(schema_path, "r") as f:
            schema_sql = f.read()

        # Execute schema (creates tables if they don't exist)
        await self._conn.executescript(schema_sql)
        await self._conn.commit()

        await self._run_migrations()

        logger.info("database_initialized", db_path=str(self.db_path))

    async def _run_migrations(self) -> None:
        """Apply lightweight schema migrations for existing SQLite databases."""
        if not self._conn:
            raise RuntimeError("Database not initialized. Call init() first.")

        await self._migrate_allocation_decision_table()
        await self._conn.commit()

    async def _migrate_allocation_decision_table(self) -> None:
        """Bring allocation_decision in line with the PMM-2 allocator writer."""
        if not self._conn:
            raise RuntimeError("Database not initialized. Call init() first.")

        cursor = await self._conn.execute("PRAGMA table_info(allocation_decision)")
        rows = await cursor.fetchall()
        await cursor.close()

        if not rows:
            return

        existing = {row[1] for row in rows}
        required_columns = {
            "bundle": "TEXT DEFAULT 'B1'",
            "capital_usdc": "REAL",
            "slots": "INTEGER",
            "marginal_return_bps": "REAL",
            "status": "TEXT",
        }

        added_columns: list[str] = []
        for column, column_type in required_columns.items():
            if column in existing:
                continue
            await self._conn.execute(
                f"ALTER TABLE allocation_decision ADD COLUMN {column} {column_type}"
            )
            added_columns.append(column)

        if added_columns:
            logger.info(
                "database_schema_migrated",
                table="allocation_decision",
                added_columns=added_columns,
            )

    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("database_closed")

    async def execute(
        self,
        sql: str,
        parameters: tuple[Any, ...] | dict[str, Any] | None = None,
    ) -> None:
        """Execute a SQL statement (INSERT, UPDATE, DELETE).

        Args:
            sql: SQL statement
            parameters: Query parameters (tuple or dict)
        """
        if not self._conn:
            raise RuntimeError("Database not initialized. Call init() first.")

        try:
            if parameters:
                await self._conn.execute(sql, parameters)
            else:
                await self._conn.execute(sql)
            await self._conn.commit()
        except Exception as e:
            logger.error("sql_execute_failed", sql=sql[:100], error=str(e))
            raise

    async def executemany(
        self,
        sql: str,
        parameters_list: list[tuple[Any, ...]] | list[dict[str, Any]],
    ) -> None:
        """Alias for execute_many (backwards compat)."""
        return await self.execute_many(sql, parameters_list)

    async def execute_many(
        self,
        sql: str,
        parameters_list: list[tuple[Any, ...]] | list[dict[str, Any]],
    ) -> None:
        """Execute a SQL statement multiple times with different parameters.

        Args:
            sql: SQL statement
            parameters_list: List of parameter tuples/dicts
        """
        if not self._conn:
            raise RuntimeError("Database not initialized. Call init() first.")

        try:
            await self._conn.executemany(sql, parameters_list)
            await self._conn.commit()
        except Exception as e:
            logger.error("sql_executemany_failed", sql=sql[:100], error=str(e))
            raise

    async def fetch_all(
        self,
        sql: str,
        parameters: tuple[Any, ...] | dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all rows from a SELECT query.

        Args:
            sql: SELECT statement
            parameters: Query parameters

        Returns:
            List of row dicts
        """
        if not self._conn:
            raise RuntimeError("Database not initialized. Call init() first.")

        try:
            if parameters:
                cursor = await self._conn.execute(sql, parameters)
            else:
                cursor = await self._conn.execute(sql)

            rows = await cursor.fetchall()
            
            # Convert rows to dicts using column names
            columns = [desc[0] for desc in cursor.description]
            result = [dict(zip(columns, row)) for row in rows]
            
            await cursor.close()
            return result
        except Exception as e:
            logger.error("sql_fetch_all_failed", sql=sql[:100], error=str(e))
            raise

    async def fetch_one(
        self,
        sql: str,
        parameters: tuple[Any, ...] | dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Fetch one row from a SELECT query.

        Args:
            sql: SELECT statement
            parameters: Query parameters

        Returns:
            Row dict or None if no rows
        """
        if not self._conn:
            raise RuntimeError("Database not initialized. Call init() first.")

        try:
            if parameters:
                cursor = await self._conn.execute(sql, parameters)
            else:
                cursor = await self._conn.execute(sql)

            row = await cursor.fetchone()
            
            if row is None:
                await cursor.close()
                return None

            # Convert row to dict using column names
            columns = [desc[0] for desc in cursor.description]
            result = dict(zip(columns, row))
            
            await cursor.close()
            return result
        except Exception as e:
            logger.error("sql_fetch_one_failed", sql=sql[:100], error=str(e))
            raise

    async def get_last_insert_rowid(self) -> int:
        """Get the rowid of the last INSERT."""
        if not self._conn:
            raise RuntimeError("Database not initialized. Call init() first.")
        
        cursor = await self._conn.execute("SELECT last_insert_rowid()")
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else 0

    def __repr__(self) -> str:
        return f"Database({self.db_path})"

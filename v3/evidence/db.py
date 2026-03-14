"""
V3 Evidence Layer Database Interface
Async Postgres connection and migration runner
"""

from pathlib import Path

import asyncpg
import structlog

log = structlog.get_logger()


class Database:
    """Async Postgres database connection manager"""

    def __init__(self, dsn: str):
        """
        Initialize database connection manager

        Args:
            dsn: PostgreSQL connection string (e.g., 'postgresql://user:pass@host/db')
        """
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Establish connection pool to database"""
        if self.pool is not None:
            log.warning("database_already_connected")
            return

        log.info("database_connecting", dsn=self.dsn.split('@')[-1])  # hide credentials
        self.pool = await asyncpg.create_pool(
            self.dsn,
            min_size=2,
            max_size=10,
            command_timeout=60
        )
        log.info("database_connected")

    async def close(self) -> None:
        """Close database connection pool"""
        if self.pool is None:
            return

        log.info("database_closing")
        await self.pool.close()
        self.pool = None
        log.info("database_closed")

    async def execute(self, query: str, *args) -> str:
        """
        Execute a query that doesn't return results (INSERT, UPDATE, DELETE)

        Args:
            query: SQL query string
            *args: Query parameters

        Returns:
            Status string from database
        """
        if self.pool is None:
            raise RuntimeError("Database not connected. Call connect() first.")

        async with self.pool.acquire() as conn:
            result = await conn.execute(query, *args)
            return result

    async def fetch(self, query: str, *args) -> list[dict]:
        """
        Fetch multiple rows as list of dicts

        Args:
            query: SQL query string
            *args: Query parameters

        Returns:
            List of row dictionaries
        """
        if self.pool is None:
            raise RuntimeError("Database not connected. Call connect() first.")

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return [dict(row) for row in rows]

    async def fetchrow(self, query: str, *args) -> dict | None:
        """
        Fetch a single row as dict

        Args:
            query: SQL query string
            *args: Query parameters

        Returns:
            Row dictionary or None if no results
        """
        if self.pool is None:
            raise RuntimeError("Database not connected. Call connect() first.")

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *args)
            return dict(row) if row else None

    async def run_migrations(self) -> None:
        """
        Run all SQL migration files in migrations/ directory
        Executes files in sorted order (001_initial.sql, 002_..., etc.)
        """
        if self.pool is None:
            raise RuntimeError("Database not connected. Call connect() first.")

        migrations_dir = Path(__file__).parent / "migrations"

        if not migrations_dir.exists():
            log.warning("migrations_dir_not_found", path=str(migrations_dir))
            return

        # Get all .sql files sorted by name
        migration_files = sorted(migrations_dir.glob("*.sql"))

        if not migration_files:
            log.warning("no_migration_files_found")
            return

        log.info("running_migrations", count=len(migration_files))

        async with self.pool.acquire() as conn:
            for migration_file in migration_files:
                log.info("executing_migration", file=migration_file.name)

                sql = migration_file.read_text()

                try:
                    await conn.execute(sql)
                    log.info("migration_completed", file=migration_file.name)
                except Exception as e:
                    log.error("migration_failed", file=migration_file.name, error=str(e))
                    raise

        log.info("all_migrations_completed")

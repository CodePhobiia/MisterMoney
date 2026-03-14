"""Data export CLI for PMM-2.

Export data from SQLite to CSV for analysis.

Usage:
    python -m pmm1.tools.export_data --table fill_record --since 24h --format csv
    python -m pmm1.tools.export_data --table book_snapshot --since 7d --format csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pmm1.storage.database import Database


def parse_since(since_str: str) -> datetime:
    """Parse a relative time string like '1h', '24h', '7d' into a datetime.

    Args:
        since_str: String like '1h', '24h', '7d'

    Returns:
        Datetime representing the cutoff (now - duration)
    """
    match = re.match(r"^(\d+)(h|d)$", since_str.lower())
    if not match:
        raise ValueError(f"Invalid --since format: {since_str}. Use format like '1h', '24h', '7d'")

    amount = int(match.group(1))
    unit = match.group(2)

    now = datetime.now(UTC)

    if unit == "h":
        return now - timedelta(hours=amount)
    elif unit == "d":
        return now - timedelta(days=amount)
    else:
        raise ValueError(f"Unknown time unit: {unit}")


async def export_table(
    db: Database,
    table: str,
    since: datetime | None = None,
    format: str = "csv",
) -> None:
    """Export a table to stdout in the specified format.

    Args:
        db: Database instance
        table: Table name to export
        since: Optional cutoff datetime (only export rows >= this time)
        format: Output format ('csv')
    """
    # Build query
    if since:
        since_iso = since.isoformat()
        # Most tables use 'ts', but some use 'date'
        ts_column = "date" if table in ("reward_actual", "rebate_actual") else "ts"
        sql = f"SELECT * FROM {table} WHERE {ts_column} >= ? ORDER BY {ts_column}"
        rows = await db.fetch_all(sql, (since_iso,))
    else:
        sql = f"SELECT * FROM {table}"
        rows = await db.fetch_all(sql)

    if not rows:
        print(f"# No data found in {table}", file=sys.stderr)
        return

    # Export as CSV
    if format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    else:
        raise ValueError(f"Unknown format: {format}")

    print(f"# Exported {len(rows)} rows from {table}", file=sys.stderr)


async def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Export PMM-2 data to CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--table",
        required=True,
        choices=[
            "fill_record",
            "book_snapshot",
            "market_score",
            "queue_state",
            "allocation_decision",
            "reward_actual",
            "rebate_actual",
            "scoring_history",
        ],
        help="Table to export",
    )
    parser.add_argument(
        "--since",
        help="Export data since this time (e.g., '1h', '24h', '7d')",
    )
    parser.add_argument(
        "--format",
        default="csv",
        choices=["csv"],
        help="Output format (default: csv)",
    )
    parser.add_argument(
        "--db",
        default="data/pmm1.db",
        help="Path to database file (default: data/pmm1.db)",
    )

    args = parser.parse_args()

    # Parse --since
    since: datetime | None = None
    if args.since:
        since = parse_since(args.since)
        print(f"# Exporting data since: {since.isoformat()}", file=sys.stderr)

    # Initialize database
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    db = Database(db_path)
    await db.init()

    try:
        await export_table(db, args.table, since, args.format)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

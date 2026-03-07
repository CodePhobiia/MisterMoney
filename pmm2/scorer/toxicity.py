"""Toxicity cost computation — adverse selection from recent fills.

Query fill_record table for recent fills with markouts.
Weighted markout: 0.5 * avg(markout_1s) + 0.3 * avg(markout_5s) + 0.2 * avg(markout_30s)

Sign convention: positive = adverse (we lost), negative = favorable
"""

from __future__ import annotations

import time

import structlog

from pmm1.storage.database import Database

logger = structlog.get_logger(__name__)


async def compute_toxicity(
    db: Database, condition_id: str, lookback_hours: int = 24
) -> float:
    """Compute toxicity cost from recent fill markouts.

    Args:
        db: database connection
        condition_id: market condition ID
        lookback_hours: how far back to look for fills (default 24h)

    Returns:
        Weighted average markout (positive = adverse selection)
    """
    # Compute lookback timestamp
    lookback_sec = lookback_hours * 3600
    cutoff_ts = time.time() - lookback_sec

    # Convert to ISO8601 for query
    from datetime import datetime, timezone

    cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()

    # Query recent fills with markouts
    sql = """
        SELECT markout_1s, markout_5s, markout_30s
        FROM fill_record
        WHERE condition_id = ?
          AND ts >= ?
          AND markout_1s IS NOT NULL
          AND markout_5s IS NOT NULL
          AND markout_30s IS NOT NULL
    """

    rows = await db.fetch_all(sql, (condition_id, cutoff_iso))

    if not rows:
        # No fills yet (cold start) — return conservative default
        logger.debug(
            "toxicity_no_fills",
            condition_id=condition_id,
            default_tox=0.001,
        )
        return 0.001  # 1bp default toxicity

    # Compute weighted average
    # Weighted markout: 0.5 * avg(markout_1s) + 0.3 * avg(markout_5s) + 0.2 * avg(markout_30s)
    markout_1s_values = [row["markout_1s"] for row in rows]
    markout_5s_values = [row["markout_5s"] for row in rows]
    markout_30s_values = [row["markout_30s"] for row in rows]

    avg_1s = sum(markout_1s_values) / len(markout_1s_values)
    avg_5s = sum(markout_5s_values) / len(markout_5s_values)
    avg_30s = sum(markout_30s_values) / len(markout_30s_values)

    weighted_markout = 0.5 * avg_1s + 0.3 * avg_5s + 0.2 * avg_30s

    logger.debug(
        "toxicity_computed",
        condition_id=condition_id,
        fill_count=len(rows),
        avg_1s=avg_1s,
        avg_5s=avg_5s,
        avg_30s=avg_30s,
        weighted=weighted_markout,
    )

    return weighted_markout

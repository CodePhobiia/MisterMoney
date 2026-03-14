"""
V3 Change Detector
Decides whether a market needs a fresh V3 evaluation
"""

from datetime import datetime

import structlog

from v3.evidence.db import Database
from v3.evidence.entities import ChangeEvent
from v3.intake.schemas import MarketMeta

log = structlog.get_logger()


# Route-specific TTL (time-to-live) for signals
ROUTE_TTL_SECONDS = {
    "numeric": 60,          # 1 minute (fast-moving numerical markets)
    "simple": 900,          # 15 minutes (simple YES/NO events)
    "rule": 1800,           # 30 minutes (rule-based markets)
    "dossier": 7200,        # 2 hours (complex multi-factor markets)
}

# Threshold for market mid movement (in cents)
MID_MOVE_THRESHOLD_CENTS = 5

# Volume spike multiplier (>3x = spike)
VOLUME_SPIKE_MULTIPLIER = 3.0

# Time window for "approaching resolution"
APPROACHING_RESOLUTION_HOURS = 6


class ChangeDetector:
    """Decides whether a market needs a fresh V3 evaluation"""

    def __init__(self, db: Database):
        """
        Initialize change detector

        Args:
            db: Database instance
        """
        self.db = db

    async def needs_refresh(self,
                           condition_id: str,
                           market: MarketMeta) -> ChangeEvent | None:
        """
        Returns ChangeEvent if refresh needed, None if current signal is still valid.

        Triggers:
        - No existing signal (first time)
        - Signal expired (past TTL by route: numeric 60s, simple 15min, rule 30min, dossier 2h)
        - Market mid moved >5¢ since last signal
        - Volume spike (>3x 24h average)
        - New evidence added since last signal
        - Approaching resolution (<6h to end_date)

        Args:
            condition_id: Market condition ID
            market: Current market metadata

        Returns:
            ChangeEvent if refresh needed, None otherwise
        """
        # Check for existing signal
        existing_signal = await self._get_latest_signal(condition_id)

        # Trigger 1: No existing signal (first time)
        if existing_signal is None:
            log.info("change_detected_no_signal", condition_id=condition_id)
            return ChangeEvent(
                event_type="no_existing_signal",
                condition_id=condition_id,
                payload={"reason": "First evaluation for this market"},
            )

        signal_ts = existing_signal["generated_at"]
        signal_route = existing_signal["route"]
        signal_mid = existing_signal.get("last_mid")  # We'll store this

        # Trigger 2: Signal expired
        ttl_seconds = ROUTE_TTL_SECONDS.get(signal_route, 900)
        age_seconds = (datetime.utcnow() - signal_ts).total_seconds()

        if age_seconds > ttl_seconds:
            log.info("change_detected_expired",
                    condition_id=condition_id,
                    age_seconds=age_seconds,
                    ttl_seconds=ttl_seconds)
            return ChangeEvent(
                event_type="signal_expired",
                condition_id=condition_id,
                payload={
                    "reason": f"Signal expired (age={age_seconds:.0f}s, ttl={ttl_seconds}s)",
                    "age_seconds": age_seconds,
                    "ttl_seconds": ttl_seconds,
                },
            )

        # Trigger 3: Market mid moved >5¢
        if signal_mid is not None:
            mid_move_cents = abs(market.current_mid - signal_mid) * 100
            if mid_move_cents > MID_MOVE_THRESHOLD_CENTS:
                log.info("change_detected_price_move",
                        condition_id=condition_id,
                        mid_move_cents=mid_move_cents)
                return ChangeEvent(
                    event_type="price_movement",
                    condition_id=condition_id,
                    payload={
                        "reason": f"Market mid moved {mid_move_cents:.1f}¢",
                        "old_mid": signal_mid,
                        "new_mid": market.current_mid,
                        "move_cents": mid_move_cents,
                    },
                )

        # Trigger 4: Volume spike
        avg_volume = await self._get_avg_volume(condition_id)
        if avg_volume > 0 and market.volume_24h > avg_volume * VOLUME_SPIKE_MULTIPLIER:
            log.info("change_detected_volume_spike",
                    condition_id=condition_id,
                    volume_24h=market.volume_24h,
                    avg_volume=avg_volume)
            return ChangeEvent(
                event_type="volume_spike",
                condition_id=condition_id,
                payload={
                    "reason": (
                        f"Volume spike detected"
                        f" ({market.volume_24h / avg_volume:.1f}x"
                        f" average)"
                    ),
                    "volume_24h": market.volume_24h,
                    "avg_volume": avg_volume,
                },
            )

        # Trigger 5: New evidence
        new_evidence_count = await self._count_new_evidence(condition_id, signal_ts)
        if new_evidence_count > 0:
            log.info("change_detected_new_evidence",
                    condition_id=condition_id,
                    count=new_evidence_count)
            return ChangeEvent(
                event_type="new_evidence",
                condition_id=condition_id,
                payload={
                    "reason": f"{new_evidence_count} new evidence items since last signal",
                    "count": new_evidence_count,
                },
            )

        # Trigger 6: Approaching resolution
        time_to_end = market.end_date - datetime.utcnow()
        if time_to_end.total_seconds() < APPROACHING_RESOLUTION_HOURS * 3600:
            # Only trigger if we haven't checked recently (avoid spam near deadline)
            if age_seconds > 600:  # 10 minutes
                log.info("change_detected_approaching_resolution",
                        condition_id=condition_id,
                        hours_remaining=time_to_end.total_seconds() / 3600)
                return ChangeEvent(
                    event_type="approaching_resolution",
                    condition_id=condition_id,
                    payload={
                        "reason": (
                            f"Approaching resolution"
                            f" ({time_to_end.total_seconds() / 3600:.1f}h"
                            f" remaining)"
                        ),
                        "hours_remaining": time_to_end.total_seconds() / 3600,
                    },
                )

        # No triggers hit — signal is still fresh
        log.debug("no_change_detected", condition_id=condition_id)
        return None

    async def _get_latest_signal(self, condition_id: str) -> dict | None:
        """Get most recent signal for a condition"""
        query = """
            SELECT
                condition_id,
                generated_at,
                route,
                p_calibrated as last_mid,
                expires_at
            FROM fair_value_signals
            WHERE condition_id = $1
            ORDER BY generated_at DESC
            LIMIT 1
        """

        result = await self.db.fetchrow(query, condition_id)
        return dict(result) if result else None

    async def _get_avg_volume(self, condition_id: str) -> float:
        """
        Get 7-day average volume for a condition

        For now, returns 0 (no historical volume tracking yet).
        In production, this would query a time-series table.
        """
        # TODO: Implement historical volume tracking
        return 0.0

    async def _count_new_evidence(self, condition_id: str, since: datetime) -> int:
        """Count evidence items added since timestamp"""
        query = """
            SELECT COUNT(*) as count
            FROM evidence_items
            WHERE condition_id = $1
              AND created_at > $2
        """

        result = await self.db.fetchrow(query, condition_id, since)
        return result["count"] if result else 0

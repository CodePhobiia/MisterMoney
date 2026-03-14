"""
Signal Publisher
Publishes calibrated signals to Postgres + Redis for V2 consumption
"""

import json

import redis.asyncio as redis
import structlog

from v3.evidence.db import Database
from v3.evidence.entities import FairValueSignal

log = structlog.get_logger()


class SignalPublisher:
    """Publishes calibrated signals to DB + Redis for V2 consumption"""

    # Redis TTL per route (seconds)
    REDIS_TTL = {
        'numeric': 300,     # 5 minutes
        'simple': 1800,     # 30 minutes
        'rule': 3600,       # 1 hour
        'dossier': 14400,   # 4 hours
    }

    def __init__(self, db: Database, redis_url: str = "redis://localhost:6379"):
        """
        Initialize signal publisher

        Args:
            db: Database instance for Postgres writes
            redis_url: Redis connection URL
        """
        self.db = db
        self.redis_url = redis_url
        self.redis_client: redis.Redis | None = None

    async def connect(self) -> None:
        """Connect to Redis"""
        if self.redis_client is not None:
            return

        log.info("redis_connecting", url=self.redis_url)
        self.redis_client = await redis.from_url(
            self.redis_url,
            decode_responses=True,
            encoding='utf-8'
        )
        log.info("redis_connected")

    async def close(self) -> None:
        """Close Redis connection"""
        if self.redis_client is None:
            return

        log.info("redis_closing")
        await self.redis_client.close()
        self.redis_client = None
        log.info("redis_closed")

    async def publish(self, signal: FairValueSignal) -> None:
        """
        Publish signal to both Postgres and Redis

        1. Write to fair_value_signals table (Postgres)
        2. Push to Redis key 'v3:signal:{condition_id}' with TTL
        3. Log for observability

        Args:
            signal: FairValueSignal to publish
        """
        # Ensure Redis is connected
        if self.redis_client is None:
            await self.connect()

        # 1. Write to Postgres
        # Note: We just INSERT new signals (no ON CONFLICT) since each has unique timestamp
        query = """
            INSERT INTO fair_value_signals (
                condition_id, generated_at, p_calibrated, p_low, p_high,
                uncertainty, skew_cents, hurdle_cents, hurdle_met,
                route, evidence_ids, counterevidence_ids, models_used,
                expires_at, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
            )
        """

        await self.db.execute(
            query,
            signal.condition_id,
            signal.generated_at,
            signal.p_calibrated,
            signal.p_low,
            signal.p_high,
            signal.uncertainty,
            signal.skew_cents,
            signal.hurdle_cents,
            signal.hurdle_met,
            signal.route,
            json.dumps(signal.evidence_ids),  # Convert list to JSON string
            json.dumps(signal.counterevidence_ids),  # Convert list to JSON string
            json.dumps(signal.models_used),  # Convert list to JSON string
            signal.expires_at,
            signal.created_at
        )

        # 2. Push to Redis with TTL
        redis_key = f"v3:signal:{signal.condition_id}"
        signal_json = signal.model_dump_json()

        ttl = self.REDIS_TTL.get(signal.route, 3600)

        await self.redis_client.set(
            redis_key,
            signal_json,
            ex=ttl
        )

        # 3. Log
        log.info(
            "signal_published",
            condition_id=signal.condition_id,
            route=signal.route,
            p_calibrated=round(signal.p_calibrated, 4),
            hurdle_met=signal.hurdle_met,
            ttl=ttl
        )

    async def get_latest(self, condition_id: str) -> FairValueSignal | None:
        """
        Get latest signal from Redis (fast) or DB (fallback)

        Args:
            condition_id: Condition ID to fetch

        Returns:
            FairValueSignal if found, None otherwise
        """
        # Ensure Redis is connected
        if self.redis_client is None:
            await self.connect()

        # Try Redis first
        redis_key = f"v3:signal:{condition_id}"
        signal_json = await self.redis_client.get(redis_key)

        if signal_json:
            log.debug("signal_from_redis", condition_id=condition_id)
            return FairValueSignal.model_validate_json(signal_json)

        # Fallback to DB
        query = """
            SELECT
                condition_id, generated_at, p_calibrated, p_low, p_high,
                uncertainty, skew_cents, hurdle_cents, hurdle_met,
                route, evidence_ids, counterevidence_ids, models_used,
                expires_at, created_at
            FROM fair_value_signals
            WHERE condition_id = $1
            ORDER BY generated_at DESC
            LIMIT 1
        """

        row = await self.db.fetchrow(query, condition_id)

        if row:
            log.debug("signal_from_db", condition_id=condition_id)
            return FairValueSignal(**row)

        log.debug("signal_not_found", condition_id=condition_id)
        return None

    async def get_cached_or_neutral(self, condition_id: str) -> FairValueSignal:
        """
        Get cached signal, or return neutral signal

        Neutral signal: p=0.5, hurdle_met=False

        Args:
            condition_id: Condition ID to fetch

        Returns:
            FairValueSignal (either cached or neutral)
        """
        signal = await self.get_latest(condition_id)

        if signal:
            return signal

        # Return neutral signal
        log.debug("returning_neutral_signal", condition_id=condition_id)
        return FairValueSignal(
            condition_id=condition_id,
            p_calibrated=0.5,
            uncertainty=1.0,
            hurdle_met=False,
            route='simple',
            evidence_ids=[],
            counterevidence_ids=[],
            models_used=[]
        )

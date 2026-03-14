"""
V3 Fair Value Integrator
Blends V3 calibrated probability signals with V1 book midpoint
"""

from __future__ import annotations

from datetime import UTC, datetime

import redis.asyncio as redis
import structlog

from v3.evidence.entities import FairValueSignal

log = structlog.get_logger()


class V3Integrator:
    """
    Integrates V3 fair value signals into V1 quote engine.

    V1 fair value = book midpoint (m_t)
    V3 fair value = calibrated probability (p_t) from V3Consumer

    Blended fair value:
        fv_blended = clamp(p_t, m_t - max_skew, m_t + max_skew)

    If V3 signal unavailable/expired/low-confidence/disabled:
        fv_blended = m_t  (no change)

    Configuration:
        - max_skew_cents: Maximum V3 can move fair value (in cents)
        - min_confidence: Minimum confidence (1 - uncertainty) to use V3 signal
        - max_age_seconds: Maximum signal age before falling back to midpoint
        - enabled: Master kill switch
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        max_skew_cents: float = 1.0,
        min_confidence: float = 0.70,
        max_age_seconds: float = 300.0,
        blend_weight: float = 0.0,
        enabled: bool = False,
    ):
        """
        Initialize V3 integrator.

        Args:
            redis_url: Redis connection URL
            max_skew_cents: Max V3 can move fair value (in cents, as decimal)
            min_confidence: Minimum confidence to use V3 signal (1 - uncertainty)
            max_age_seconds: Max age before falling back to midpoint
            blend_weight: Paper 1 weighted blend (0.0 = disabled, 0.33 = 33% AI)
            enabled: Master kill switch
        """
        self.redis_url = redis_url
        self.max_skew_cents = max_skew_cents
        self.min_confidence = min_confidence
        self.max_age_seconds = max_age_seconds
        self.blend_weight = blend_weight
        self._enabled = enabled
        self._redis: redis.Redis | None = None

    async def connect(self) -> None:
        """Connect to Redis."""
        self._redis = await redis.from_url(self.redis_url, decode_responses=True)
        log.info("v3_integrator_connected", redis_url=self.redis_url)

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
            log.info("v3_integrator_closed")

    @property
    def enabled(self) -> bool:
        """Return master kill switch status."""
        return self._enabled

    async def get_blended_fair_value(
        self,
        condition_id: str,
        book_midpoint: float
    ) -> tuple[float, dict]:
        """
        Get blended fair value combining V3 signal and book midpoint.

        Args:
            condition_id: Polymarket condition ID
            book_midpoint: Current book midpoint from V1

        Returns:
            (blended_fair_value, metadata_dict)

            metadata_dict includes:
            - v3_used: bool - Whether V3 signal was used
            - v3_raw: float | None - Raw V3 signal (p_calibrated)
            - v3_clamped: float | None - Clamped V3 value
            - v3_route: str | None - V3 route name
            - v3_age_seconds: float | None - Signal age in seconds
            - skew_applied_cents: float - Actual skew applied (in cents)
            - miss_reason: str | None - Why V3 wasn't used (if applicable)
        """
        metadata = {
            "v3_used": False,
            "v3_raw": None,
            "v3_clamped": None,
            "v3_route": None,
            "v3_age_seconds": None,
            "skew_applied_cents": 0.0,
            "miss_reason": None,
        }

        # Kill switch check
        if not self._enabled:
            metadata["miss_reason"] = "disabled"
            return book_midpoint, metadata

        # Redis connection check
        if not self._redis:
            metadata["miss_reason"] = "not_connected"
            log.warning("v3_integrator_not_connected", condition_id=condition_id)
            return book_midpoint, metadata

        # Fetch V3 signal from Redis (publisher uses redis.set with JSON)
        try:
            signal_json = await self._redis.get(f"v3:signal:{condition_id}")
        except Exception as e:
            metadata["miss_reason"] = "redis_error"
            log.error("v3_redis_fetch_error", condition_id=condition_id, error=str(e))
            return book_midpoint, metadata

        if not signal_json:
            metadata["miss_reason"] = "not_available"
            return book_midpoint, metadata

        # Parse signal from JSON (matches publisher's SET format)
        try:
            signal = FairValueSignal.model_validate_json(signal_json)
        except Exception as e:
            metadata["miss_reason"] = "parse_error"
            log.error("v3_signal_parse_error", condition_id=condition_id, error=str(e))
            return book_midpoint, metadata

        # Check signal age
        age_seconds = (
            datetime.now(UTC)
            - signal.generated_at.replace(tzinfo=UTC)
        ).total_seconds()

        metadata["v3_age_seconds"] = age_seconds
        metadata["v3_route"] = signal.route

        if age_seconds > self.max_age_seconds:
            metadata["miss_reason"] = "expired"
            log.debug(
                "v3_signal_expired",
                condition_id=condition_id,
                age_seconds=round(age_seconds, 1),
                max_age=self.max_age_seconds,
            )
            return book_midpoint, metadata

        # Check confidence (1 - uncertainty)
        confidence = 1.0 - signal.uncertainty
        if confidence < self.min_confidence:
            metadata["miss_reason"] = "low_confidence"
            log.debug(
                "v3_signal_low_confidence",
                condition_id=condition_id,
                confidence=round(confidence, 4),
                min_confidence=self.min_confidence,
            )
            return book_midpoint, metadata

        # Check hurdle_met
        if not signal.hurdle_met:
            metadata["miss_reason"] = "hurdle_not_met"
            log.debug(
                "v3_signal_hurdle_not_met",
                condition_id=condition_id,
                p_calibrated=round(signal.p_calibrated, 4),
            )
            return book_midpoint, metadata

        # V3 signal is valid — apply blending + clamping
        metadata["v3_raw"] = signal.p_calibrated

        # Paper 1: weighted blend (67% market / 33% AI at full ramp)
        if self.blend_weight > 0:
            blended = (
                (1.0 - self.blend_weight) * book_midpoint
                + self.blend_weight * signal.p_calibrated
            )
        else:
            blended = signal.p_calibrated

        # Safety clamp: don't let V3 move more than max_skew
        max_skew_decimal = self.max_skew_cents / 100.0
        lower_bound = book_midpoint - max_skew_decimal
        upper_bound = book_midpoint + max_skew_decimal
        v3_clamped = max(lower_bound, min(upper_bound, blended))
        metadata["v3_clamped"] = v3_clamped

        # Calculate actual skew applied (in cents)
        skew_applied_decimal = v3_clamped - book_midpoint
        metadata["skew_applied_cents"] = skew_applied_decimal * 100.0

        metadata["v3_used"] = True

        log.info(
            "v3_signal_blended",
            condition_id=condition_id,
            book_midpoint=round(book_midpoint, 4),
            v3_raw=round(signal.p_calibrated, 4),
            v3_clamped=round(v3_clamped, 4),
            skew_cents=round(metadata["skew_applied_cents"], 2),
            route=signal.route,
            confidence=round(confidence, 4),
        )

        return v3_clamped, metadata

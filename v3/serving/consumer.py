"""
V2 Consumer Integration
Interface for V2 scorer to consume V3 fair value signals
"""

from datetime import UTC, datetime

import structlog

from v3.calibration.decay import is_signal_expired
from v3.serving.publisher import SignalPublisher

log = structlog.get_logger()


class V3Consumer:
    """Called by V2 scorer to get V3 fair value signal"""

    MAX_UNCERTAINTY = 0.30  # Don't use signals with uncertainty > 30%

    def __init__(self, publisher: SignalPublisher):
        """
        Initialize V3 consumer

        Args:
            publisher: SignalPublisher instance for reading signals
        """
        self.publisher = publisher

    async def get_fair_value(self, condition_id: str) -> float | None:
        """
        Get calibrated fair value for condition

        Returns p_calibrated if:
        1. Signal exists and not expired
        2. hurdle_met is True
        3. uncertainty < 0.30

        Returns None otherwise (V2 uses book midpoint as before).

        Args:
            condition_id: Condition ID to fetch

        Returns:
            Calibrated probability or None
        """
        signal = await self.publisher.get_latest(condition_id)

        if signal is None:
            log.debug("no_signal", condition_id=condition_id)
            return None

        # Check if expired
        age_seconds = (datetime.now(UTC) - signal.generated_at.replace(tzinfo=UTC)).total_seconds()

        if is_signal_expired(age_seconds, signal.route):
            log.debug(
                "signal_expired",
                condition_id=condition_id,
                age_seconds=round(age_seconds, 1),
                route=signal.route
            )
            return None

        # Check hurdle_met
        if not signal.hurdle_met:
            log.debug(
                "hurdle_not_met",
                condition_id=condition_id,
                p_calibrated=round(signal.p_calibrated, 4)
            )
            return None

        # Check uncertainty
        if signal.uncertainty > self.MAX_UNCERTAINTY:
            log.debug(
                "uncertainty_too_high",
                condition_id=condition_id,
                uncertainty=round(signal.uncertainty, 4),
                max_uncertainty=self.MAX_UNCERTAINTY
            )
            return None

        log.info(
            "fair_value_signal",
            condition_id=condition_id,
            p_calibrated=round(signal.p_calibrated, 4),
            uncertainty=round(signal.uncertainty, 4),
            route=signal.route
        )

        return signal.p_calibrated

    async def get_signal_detail(self, condition_id: str) -> dict | None:
        """
        Get full signal with metadata for dashboard/logging

        Args:
            condition_id: Condition ID to fetch

        Returns:
            Dict with signal metadata or None
        """
        signal = await self.publisher.get_latest(condition_id)

        if signal is None:
            return None

        # Compute age
        age_seconds = (datetime.now(UTC) - signal.generated_at.replace(tzinfo=UTC)).total_seconds()

        return {
            'condition_id': signal.condition_id,
            'p_calibrated': signal.p_calibrated,
            'p_low': signal.p_low,
            'p_high': signal.p_high,
            'uncertainty': signal.uncertainty,
            'skew_cents': signal.skew_cents,
            'hurdle_cents': signal.hurdle_cents,
            'hurdle_met': signal.hurdle_met,
            'route': signal.route,
            'evidence_count': len(signal.evidence_ids),
            'counterevidence_count': len(signal.counterevidence_ids),
            'models_used': signal.models_used,
            'age_seconds': round(age_seconds, 1),
            'expired': is_signal_expired(age_seconds, signal.route),
            'generated_at': signal.generated_at.isoformat(),
        }

"""Warmup loss estimator.

Estimates the cost of resetting a near-scoring order.

Polymarket rewards depend on orders being:
- Live (on the book)
- Valid (at reasonable prices)
- Old enough (warmup period)

Resetting an order that's 90% through warmup is much more expensive than
resetting one that just posted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pmm2.persistence.state_machine import PersistenceOrder


class WarmupEstimator:
    """Estimate reward loss from resetting a near-scoring order."""

    def __init__(self, warmup_seconds: float = 60.0) -> None:
        """Initialize warmup estimator.

        Args:
            warmup_seconds: estimated time for a new order to start scoring.
                Polymarket docs say scoring depends on being live, valid, and old enough.
        """
        self.warmup_seconds = warmup_seconds

    def warmup_progress(self, order: PersistenceOrder) -> float:
        """Fraction of warmup completed [0, 1].

        Args:
            order: order to evaluate

        Returns:
            Progress toward scoring eligibility (0.0 = just posted, 1.0 = fully warmed)
        """
        if order.is_scoring:
            return 1.0

        progress = order.age_sec / self.warmup_seconds
        return min(1.0, progress)

    def warmup_loss(
        self, order: PersistenceOrder, expected_reward_per_sec: float
    ) -> float:
        """Calculate warmup loss from resetting this order.

        If we cancel and repost, we lose:
        - Remaining warmup time * expected reward rate
        - More if we're 90% through warmup vs 10%

        The loss is proportional to how far we've progressed.

        Args:
            order: order being reset
            expected_reward_per_sec: expected rewards per second once scoring

        Returns:
            Warmup loss in USDC
        """
        if not order.is_scoring and order.age_sec < 5.0:
            # Order is brand new, minimal warmup loss
            return 0.0

        progress = self.warmup_progress(order)

        # Loss = progress * warmup_time * reward_rate
        # This represents the opportunity cost of throwing away warmup progress
        loss = progress * self.warmup_seconds * expected_reward_per_sec

        return loss

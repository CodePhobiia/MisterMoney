"""Hysteresis layer to prevent unnecessary order moves.

Only allows action != HOLD if improvement exceeds a dynamic threshold.

The threshold increases when:
- Order is currently scoring (losing warmup progress is expensive)
- Order is close to filling (losing queue position is expensive)
- Inventory skew is high (don't thrash positions when leaning)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from pmm2.persistence.state_machine import PersistenceOrder


class HysteresisConfig(BaseModel):
    """Configuration for hysteresis thresholds."""

    base_usdc: float = 0.25  # ξ₀ — minimum improvement needed
    scoring_extra: float = 0.40  # ξ₁ — extra bar if currently scoring
    eta_extra: float = 0.30  # ξ₂ — extra bar if ETA < 15s (close to fill)
    skew_factor: float = 0.10  # ξ₃ — scales with inventory skew


class HysteresisGate:
    """Only allow action != HOLD if improvement exceeds threshold."""

    def __init__(self, config: HysteresisConfig | None = None) -> None:
        """Initialize hysteresis gate.

        Args:
            config: hysteresis configuration (uses defaults if None)
        """
        self.config = config or HysteresisConfig()

    def threshold(self, order: PersistenceOrder, inventory_skew: float = 0.0) -> float:
        """Compute dynamic threshold for this order.

        ξ = ξ₀ + ξ₁·𝟙(scoring) + ξ₂·𝟙(ETA<15s) + ξ₃·|skew|

        Scoring/entrenched orders need much bigger reason to move.

        Args:
            order: order being evaluated
            inventory_skew: current inventory skew in USDC (absolute value)

        Returns:
            Threshold in USDC that best_action must beat HOLD by
        """
        threshold = self.config.base_usdc

        # Extra threshold if scoring (don't lose warmup progress)
        if order.is_scoring:
            threshold += self.config.scoring_extra

        # Extra threshold if close to filling (don't lose queue position)
        if order.eta_sec < 15.0:
            threshold += self.config.eta_extra

        # Scale with inventory skew (don't thrash when leaning)
        threshold += self.config.skew_factor * abs(inventory_skew)

        return threshold

    def should_act(
        self,
        order: PersistenceOrder,
        hold_ev: float,
        best_action_ev: float,
        inventory_skew: float = 0.0,
    ) -> bool:
        """Return True if best_action_ev exceeds HOLD + threshold.

        Args:
            order: order being evaluated
            hold_ev: expected value of HOLD action
            best_action_ev: expected value of best alternative action
            inventory_skew: current inventory skew in USDC

        Returns:
            True if improvement justifies taking action
        """
        improvement = best_action_ev - hold_ev
        threshold = self.threshold(order, inventory_skew)
        return improvement > threshold

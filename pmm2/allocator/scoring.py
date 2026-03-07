"""Adjusted score computation — apply penalties to raw marginal returns.

R̃ = R - λ·CorrPenalty - φ·ChurnPenalty - ψ·QueueUncertainty - μ·InventoryPenalty

Penalties:
- CorrPenalty: same-event correlation (avoid double exposure)
- ChurnPenalty: cost of entering/exiting markets
- QueueUncertainty: penalty for uncertain queue position
- InventoryPenalty: penalty for directional exposure
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from pmm2.scorer.bundles import QuoteBundle

logger = structlog.get_logger(__name__)


class AdjustedScore(BaseModel):
    """Bundle with penalties applied."""

    bundle: QuoteBundle
    raw_return: float = 0.0  # R = V / Cap (from scorer)
    corr_penalty: float = 0.0  # λ · correlation with existing positions
    churn_penalty: float = 0.0  # φ · cost of entering/exiting this market
    queue_penalty: float = 0.0  # ψ · queue uncertainty
    inventory_penalty: float = 0.0  # μ · |net_exposure| / cap
    adjusted_return: float = 0.0  # R̃ = R - all penalties


class AdjustedScorer:
    """Apply penalties to raw marginal returns."""

    def __init__(
        self,
        corr_lambda: float = 0.0020,  # 20 bps
        churn_phi: float = 0.0015,  # 15 bps
        queue_psi: float = 0.0010,  # 10 bps
        inventory_mu: float = 0.0005,  # 5 bps
    ):
        """Initialize adjusted scorer.

        Args:
            corr_lambda: correlation penalty coefficient (0.0020 = 20 bps per correlated position)
            churn_phi: churn penalty coefficient (0.0015 = 15 bps for entering/exiting)
            queue_psi: queue uncertainty penalty coefficient (0.0010 = 10 bps per unit uncertainty)
            inventory_mu: inventory penalty coefficient (0.0005 = 5 bps per unit exposure)
        """
        self.corr_lambda = corr_lambda
        self.churn_phi = churn_phi
        self.queue_psi = queue_psi
        self.inventory_mu = inventory_mu

    def score(
        self,
        bundle: QuoteBundle,
        current_markets: set[str],  # condition_ids we're already in
        event_clusters: dict[str, str],  # condition_id → event_id
        active_events: dict[str, float],  # event_id → current capital
        queue_uncertainty: float = 0.0,
        net_exposure: float = 0.0,
    ) -> AdjustedScore:
        """
        Compute adjusted return R̃ = R - penalties.

        Args:
            bundle: quote bundle to score
            current_markets: set of condition_ids we're already quoting
            event_clusters: map of condition_id → event_id
            active_events: map of event_id → current capital allocated
            queue_uncertainty: uncertainty in queue position (0-1)
            net_exposure: net exposure in this market (-1 to 1, negative = long)

        Returns:
            AdjustedScore with penalties applied
        """
        condition_id = bundle.market_condition_id
        raw_return = bundle.marginal_return

        # --- Correlation Penalty ---
        # If we're already in another market in the same event, penalize
        corr_penalty = 0.0
        event_id = event_clusters.get(condition_id, "")
        if event_id and event_id in active_events:
            # Already have capital in this event
            # Penalty scales with capital already committed (rough proxy for correlation)
            event_cap = active_events[event_id]
            if event_cap > 0:
                # Normalize by bundle capital: more penalty if we're doubling down
                corr_penalty = self.corr_lambda * (event_cap / max(bundle.capital_usdc, 1.0))

        # --- Churn Penalty ---
        # If this market is new (not in current_markets), penalize
        churn_penalty = 0.0
        if condition_id not in current_markets:
            # Entering a new market: fixed cost
            churn_penalty = self.churn_phi

        # --- Queue Uncertainty Penalty ---
        # Scale by queue_uncertainty (0-1)
        queue_penalty = self.queue_psi * queue_uncertainty

        # --- Inventory Penalty ---
        # Penalize directional exposure
        # net_exposure: negative = long, positive = short
        # We want to avoid large |exposure|
        inventory_penalty = self.inventory_mu * abs(net_exposure)

        # --- Adjusted Return ---
        adjusted_return = (
            raw_return - corr_penalty - churn_penalty - queue_penalty - inventory_penalty
        )

        result = AdjustedScore(
            bundle=bundle,
            raw_return=raw_return,
            corr_penalty=corr_penalty,
            churn_penalty=churn_penalty,
            queue_penalty=queue_penalty,
            inventory_penalty=inventory_penalty,
            adjusted_return=adjusted_return,
        )

        logger.debug(
            "adjusted_score_computed",
            condition_id=condition_id,
            bundle=bundle.bundle_type,
            raw_return=raw_return,
            corr_penalty=corr_penalty,
            churn_penalty=churn_penalty,
            queue_penalty=queue_penalty,
            inventory_penalty=inventory_penalty,
            adjusted_return=adjusted_return,
        )

        return result

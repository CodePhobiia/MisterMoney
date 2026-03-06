"""Directional overlay — DISABLED by default in v1.

From §4D: When enabled, only crosses spread when edge survives
fees + slippage + model haircut + resolution risk + correlation caps.

"We will not ship a 'forecasting hero bot' first."
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from pmm1.strategy.features import FeatureVector

logger = structlog.get_logger(__name__)


class DirectionalSignal(BaseModel):
    """A directional trade signal (disabled by default)."""

    token_id: str
    condition_id: str = ""
    side: str = ""  # BUY or SELL
    edge: float = 0.0
    confidence: float = 0.0
    source: str = ""  # Source of signal
    is_actionable: bool = False


class DirectionalOverlay:
    """Directional trading overlay — disabled in v1.

    When enabled (v2+), only takes directional bets when:
    - Edge survives fees + slippage + model haircut
    - Resolution risk is acceptable
    - Correlation caps are not breached
    - Cluster exposure limits are respected
    """

    def __init__(self, enabled: bool = False) -> None:
        self._enabled = enabled

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def evaluate(
        self,
        features: FeatureVector,
        fair_value: float,
        haircut: float,
    ) -> DirectionalSignal | None:
        """Evaluate directional signal.

        Returns None when disabled (v1 default).
        """
        if not self._enabled:
            return None

        # Placeholder for v2: directional logic would go here
        logger.debug("directional_overlay_disabled")
        return None

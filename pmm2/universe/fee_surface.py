"""Fee surface — track which markets have fees enabled and their fee rates.

Polymarket markets may charge maker fees. Fee-enabled markets can be more
profitable for market makers due to taker fee rebates.
"""

from __future__ import annotations

import time

import structlog

logger = structlog.get_logger(__name__)


class FeeSurface:
    """Track fee-enabled markets and their rates.

    Markets with fees_enabled=True charge taker fees and may provide
    maker rebates, improving MM profitability.
    """

    def __init__(self, ttl_seconds: int = 300) -> None:
        """Initialize fee surface with TTL cache.

        Args:
            ttl_seconds: Time-to-live for cached data (default 5 minutes).
        """
        self.ttl_seconds = ttl_seconds
        self._fee_enabled: set[str] = set()
        self._fee_rates: dict[str, float] = {}
        self._last_refresh: float = 0.0

    def is_stale(self) -> bool:
        """Check if cached data is stale and needs refresh."""
        return time.time() - self._last_refresh >= self.ttl_seconds

    def update_from_markets(self, markets: list[dict]) -> None:
        """Update fee surface from Gamma market data.

        Args:
            markets: List of market dicts from GammaClient.get_markets().
                     Expected to have 'condition_id', 'fees_enabled', and optionally 'fee_rate'.
        """
        self._fee_enabled.clear()
        self._fee_rates.clear()

        for market in markets:
            condition_id = market.get("condition_id", "")
            if not condition_id:
                # Skip markets without condition_id
                continue

            fees_enabled = market.get("fees_enabled", False) or market.get("feesEnabled", False)
            if fees_enabled:
                self._fee_enabled.add(condition_id)

                # Try to extract fee rate (not always provided by Gamma)
                fee_rate = market.get("fee_rate", 0.0) or market.get("feeRate", 0.0)
                if fee_rate > 0:
                    self._fee_rates[condition_id] = fee_rate

        self._last_refresh = time.time()

        logger.info(
            "fee_surface_updated",
            fee_enabled_markets=len(self._fee_enabled),
            markets_with_rates=len(self._fee_rates),
        )

    def is_fee_enabled(self, condition_id: str) -> bool:
        """Check if a market has fees enabled.

        Args:
            condition_id: Market condition ID.

        Returns:
            True if fees are enabled, False otherwise.
        """
        return condition_id in self._fee_enabled

    def get_fee_rate(self, condition_id: str) -> float:
        """Get the fee rate for a market.

        Args:
            condition_id: Market condition ID.

        Returns:
            Fee rate (e.g., 0.01 for 1%), or 0.0 if not available.
        """
        return self._fee_rates.get(condition_id, 0.0)

    def get_fee_info(self, condition_id: str) -> tuple[bool, float]:
        """Get fee info for a market.

        Returns:
            Tuple of (fees_enabled, fee_rate).
        """
        return (
            self.is_fee_enabled(condition_id),
            self.get_fee_rate(condition_id),
        )

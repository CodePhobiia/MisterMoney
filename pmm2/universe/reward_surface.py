"""Reward surface — track reward-eligible markets indexed by condition_id and token_id.

Wraps RewardsClient.fetch_sampling_markets() with dual indexing to support
matching by condition_id OR token_id (since Gamma /markets may have empty condition_id).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pmm1.api.rewards import RewardInfo, RewardsClient

logger = structlog.get_logger(__name__)


class RewardSurface:
    """Track reward-eligible markets with dual index (condition_id + token_id).

    Because Gamma /markets sometimes returns empty condition_id fields,
    we need to match markets by token_id as a fallback.
    """

    def __init__(self, ttl_seconds: int = 300) -> None:
        """Initialize reward surface with TTL cache.

        Args:
            ttl_seconds: Time-to-live for cached data (default 5 minutes).
        """
        self.ttl_seconds = ttl_seconds
        self.by_condition: dict[str, RewardInfo] = {}
        self.by_token: dict[str, RewardInfo] = {}
        self._last_refresh: float = 0.0

    def is_stale(self) -> bool:
        """Check if cached data is stale and needs refresh."""
        return time.time() - self._last_refresh >= self.ttl_seconds

    async def refresh(self, rewards_client: RewardsClient) -> None:
        """Refresh reward surface from rewards API.

        Builds two indexes:
        - by_condition: {condition_id: RewardInfo}
        - by_token: {token_id: RewardInfo} (one entry per token_id in RewardInfo.token_ids)

        Args:
            rewards_client: RewardsClient instance with configured API credentials.
        """
        logger.debug("reward_surface_refresh_start")

        # Fetch from API (uses internal cache with same 5-min TTL)
        data = await rewards_client.fetch_sampling_markets()

        # Build indexes
        self.by_condition = data.copy()
        self.by_token = {}

        for condition_id, info in data.items():
            # Index each token_id separately
            for token_id in info.token_ids:
                if token_id:
                    self.by_token[token_id] = info

        self._last_refresh = time.time()

        logger.info(
            "reward_surface_refreshed",
            conditions=len(self.by_condition),
            tokens=len(self.by_token),
        )

    def is_eligible(
        self,
        condition_id: str = "",
        token_id_yes: str = "",
        token_id_no: str = "",
    ) -> RewardInfo | None:
        """Check if a market is reward-eligible.

        Tries matching in order:
        1. condition_id
        2. token_id_yes
        3. token_id_no

        Returns RewardInfo if eligible, None otherwise.

        Args:
            condition_id: Market condition ID.
            token_id_yes: Yes outcome token ID.
            token_id_no: No outcome token ID.

        Returns:
            RewardInfo if market is eligible, None otherwise.
        """
        # Try condition_id first
        if condition_id and condition_id in self.by_condition:
            return self.by_condition[condition_id]

        # Fallback to token_id_yes
        if token_id_yes and token_id_yes in self.by_token:
            return self.by_token[token_id_yes]

        # Fallback to token_id_no
        if token_id_no and token_id_no in self.by_token:
            return self.by_token[token_id_no]

        return None

    def get_reward_info(
        self,
        condition_id: str = "",
        token_id_yes: str = "",
        token_id_no: str = "",
    ) -> tuple[bool, float, float, float]:
        """Get reward eligibility and parameters.

        Returns:
            Tuple of (eligible, daily_rate, min_size, max_spread).
            Returns (False, 0.0, 0.0, 0.0) if not eligible.
        """
        info = self.is_eligible(condition_id, token_id_yes, token_id_no)
        if info:
            return (True, info.daily_rate, info.min_size, info.max_spread)
        return (False, 0.0, 0.0, 0.0)

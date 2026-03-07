"""Rewards API client — sampling markets and rebate tracking.

Endpoints:
- GET /sampling-simplified-markets — returns reward-eligible markets with condition_id, rates, constraints
- GET /rebates/current?date=YYYY-MM-DD&maker_address=0x... — returns rebate data (no auth)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class RewardRate(BaseModel):
    """Reward rate info from sampling-simplified-markets."""

    asset_address: str = ""
    rewards_daily_rate: float = 0.0


class RewardInfo(BaseModel):
    """Reward info for a single market."""

    condition_id: str
    daily_rate: float = 0.0
    min_size: float = 0.0
    max_spread: float = 0.0
    token_ids: list[str] = Field(default_factory=list)


class RewardsCache:
    """Simple TTL cache for sampling markets."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl_seconds = ttl_seconds
        self._data: dict[str, RewardInfo] = {}
        self._last_fetch: float = 0.0

    def get(self) -> dict[str, RewardInfo] | None:
        """Return cached data if fresh, else None."""
        if time.time() - self._last_fetch < self.ttl_seconds:
            return self._data
        return None

    def set(self, data: dict[str, RewardInfo]) -> None:
        """Update cache."""
        self._data = data
        self._last_fetch = time.time()


def _build_hmac_signature_key_auth(
    api_secret: str,
    timestamp: str,
    method: str,
    path: str,
    body: str = "",
) -> str:
    """Build HMAC-SHA256 signature for Key auth (simple Authorization: Key {api_key}).

    For /sampling-simplified-markets, we only need the api_key in the header,
    but keeping consistent with the existing HMAC pattern from clob_private.py.
    """
    import base64

    base64_secret = base64.urlsafe_b64decode(api_secret)
    message = str(timestamp) + method.upper() + path
    if body:
        message += body.replace("'", '"')
    h = hmac.new(base64_secret, message.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(h.digest()).decode("utf-8")


class RewardsClient:
    """Client for Polymarket rewards and rebate endpoints."""

    def __init__(
        self,
        base_url: str = "https://clob.polymarket.com",
        api_key: str = "",
        api_secret: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self._session: aiohttp.ClientSession | None = None
        self._cache = RewardsCache(ttl_seconds=300)  # 5-minute TTL

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"Accept": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_sampling_markets(self) -> dict[str, RewardInfo]:
        """Fetch reward-eligible markets from /sampling-simplified-markets.

        Returns dict of {condition_id: RewardInfo}.
        Uses Authorization: Key {api_key} header (simple auth, no HMAC).
        Response structure:
        [
          {
            "condition_id": "...",
            "rewards": {
              "min_size": 10.0,
              "max_spread": 0.05,
              "rates": [
                {"asset_address": "...", "rewards_daily_rate": 0.15}
              ]
            },
            "tokens": [{"token_id": "..."}]
          }
        ]
        """
        # Check cache first
        cached = self._cache.get()
        if cached is not None:
            logger.debug("sampling_markets_cache_hit", count=len(cached))
            return cached

        session = await self._get_session()
        url = f"{self.base_url}/sampling-simplified-markets"

        headers = {
            "Authorization": f"Key {self.api_key}",
            "Accept": "application/json",
        }

        logger.debug("fetching_sampling_markets", url=url)

        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                error_body = await resp.text()
                logger.error(
                    "sampling_markets_fetch_failed",
                    status=resp.status,
                    body=error_body,
                )
                return {}

            raw = await resp.json()

        # Response may be wrapped: {"data": [...]} or just [...]
        if isinstance(raw, dict) and "data" in raw:
            data = raw["data"]
        elif isinstance(raw, list):
            data = raw
        else:
            logger.error("sampling_markets_unexpected_format", type=type(raw).__name__)
            return {}

        # Parse response into RewardInfo dict
        result: dict[str, RewardInfo] = {}
        for item in data:
            condition_id = item.get("condition_id", "")
            if not condition_id:
                continue

            rewards = item.get("rewards", {})
            rates = rewards.get("rates", [])

            # Sum daily rates across all asset addresses (usually just one)
            total_daily_rate = sum(r.get("rewards_daily_rate", 0.0) for r in rates)

            token_ids = [t.get("token_id", "") for t in item.get("tokens", [])]

            result[condition_id] = RewardInfo(
                condition_id=condition_id,
                daily_rate=total_daily_rate,
                min_size=rewards.get("min_size", 0.0),
                max_spread=rewards.get("max_spread", 0.0),
                token_ids=[tid for tid in token_ids if tid],
            )

        logger.info("sampling_markets_fetched", count=len(result))
        self._cache.set(result)
        return result

    async def fetch_rebates(
        self,
        maker_address: str,
        date: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch rebate data from /rebates/current.

        GET /rebates/current?date=YYYY-MM-DD&maker_address=0x...
        No authentication required.

        Args:
            maker_address: Wallet address (0x...)
            date: Date in YYYY-MM-DD format (defaults to today)

        Returns:
            Rebate data dict, or None if no rebates or error.
        """
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        session = await self._get_session()
        url = f"{self.base_url}/rebates/current"
        params = {
            "date": date,
            "maker_address": maker_address,
        }

        logger.debug("fetching_rebates", maker_address=maker_address, date=date)

        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 404:
                    # No rebates found
                    logger.debug("no_rebates_found", maker_address=maker_address, date=date)
                    return None
                if resp.status != 200:
                    error_body = await resp.text()
                    logger.warning(
                        "rebates_fetch_failed",
                        status=resp.status,
                        body=error_body,
                    )
                    return None

                data = await resp.json()
                logger.info("rebates_fetched", maker_address=maker_address, date=date, data=data)
                return data

        except Exception as e:
            logger.error("rebates_fetch_exception", error=str(e))
            return None


async def fetch_sampling_markets(
    base_url: str = "https://clob.polymarket.com",
    api_key: str = "",
    api_secret: str = "",
) -> dict[str, RewardInfo]:
    """Convenience function to fetch sampling markets.

    Returns dict of {condition_id: RewardInfo}.
    """
    client = RewardsClient(base_url=base_url, api_key=api_key, api_secret=api_secret)
    try:
        return await client.fetch_sampling_markets()
    finally:
        await client.close()


async def fetch_rebates(
    maker_address: str,
    date: str | None = None,
    base_url: str = "https://clob.polymarket.com",
) -> dict[str, Any] | None:
    """Convenience function to fetch rebates.

    Args:
        maker_address: Wallet address (0x...)
        date: Date in YYYY-MM-DD format (defaults to today)
        base_url: CLOB API base URL

    Returns:
        Rebate data dict, or None if no rebates or error.
    """
    client = RewardsClient(base_url=base_url)
    try:
        return await client.fetch_rebates(maker_address, date)
    finally:
        await client.close()

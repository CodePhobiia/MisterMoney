"""CLOB Public API client — unauthenticated endpoints for order books, midpoints, tick sizes."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import aiohttp
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class OrderBookLevel(BaseModel):
    """A single price level in the order book."""

    price: str
    size: str

    @property
    def price_float(self) -> float:
        return float(self.price)

    @property
    def size_float(self) -> float:
        return float(self.size)


class OrderBookResponse(BaseModel):
    """Response from GET /book endpoint."""

    market: str = ""
    asset_id: str = ""
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)
    timestamp: str = ""
    hash: str = ""


class MidpointResponse(BaseModel):
    """Response from GET /midpoint endpoint."""

    mid: str = ""

    @property
    def midpoint(self) -> float:
        return float(self.mid) if self.mid else 0.0


class TickSizeResponse(BaseModel):
    """Response from GET /tick-size endpoint."""

    minimum_tick_size: str = Field(alias="minimum_tick_size", default="0.01")

    model_config = {"populate_by_name": True}

    @property
    def tick_size(self) -> Decimal:
        return Decimal(self.minimum_tick_size)


class SamplingMarket(BaseModel):
    """A reward-eligible market from sampling endpoint."""

    condition_id: str = Field(alias="conditionId", default="")
    tokens: list[dict[str, Any]] = Field(default_factory=list)
    rewards: dict[str, Any] = Field(default_factory=dict)
    min_incentive_size: str = Field(alias="minIncentiveSize", default="0")
    max_incentive_spread: str = Field(alias="maxIncentiveSpread", default="0")
    event_slug: str = Field(alias="eventSlug", default="")

    model_config = {"populate_by_name": True}

    @property
    def token_ids(self) -> list[str]:
        return [t.get("token_id", "") for t in self.tokens if t.get("token_id")]


class SamplingSimplifiedMarket(BaseModel):
    """Simplified sampling market response."""

    condition_id: str = Field(alias="conditionId", default="")
    tokens: list[dict[str, Any]] = Field(default_factory=list)
    reward_epoch: int = Field(alias="rewardEpoch", default=0)
    reward_rates: dict[str, Any] = Field(alias="rewardRates", default_factory=dict)
    spread_data: dict[str, Any] = Field(alias="spreadData", default_factory=dict)

    model_config = {"populate_by_name": True}


class ClobPublicClient:
    """Async HTTP client for CLOB public (unauthenticated) endpoints."""

    def __init__(self, base_url: str = "https://clob.polymarket.com") -> None:
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

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

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        logger.debug("clob_public_request", url=url, params=params)
        async with session.get(url, params=params) as resp:
            if resp.status == 425:
                logger.warning("clob_restart_window", status=425)
                raise ClobRestartError("Matching engine restarting (425)")
            if resp.status == 429:
                logger.warning("clob_rate_limit", status=429)
                raise ClobRateLimitError("Rate limited (429)")
            if resp.status == 503:
                logger.warning("clob_paused", status=503)
                raise ClobPausedError("Exchange paused / cancel-only (503)")
            resp.raise_for_status()
            return await resp.json()

    async def get_server_time(self) -> int:
        """Get server time in epoch seconds."""
        data = await self._get("/time")
        return int(data)

    async def get_order_book(self, token_id: str) -> OrderBookResponse:
        """Fetch full order book for a token (asset) ID."""
        data = await self._get("/book", params={"token_id": token_id})
        return OrderBookResponse.model_validate(data)

    async def get_midpoint(self, token_id: str) -> float:
        """Get midpoint price for a token ID."""
        data = await self._get("/midpoint", params={"token_id": token_id})
        resp = MidpointResponse.model_validate(data)
        return resp.midpoint

    async def get_tick_size(self, token_id: str) -> Decimal:
        """Get minimum tick size for a token ID."""
        data = await self._get("/tick-size", params={"token_id": token_id})
        resp = TickSizeResponse.model_validate(data)
        return resp.tick_size

    async def get_sampling_markets(self) -> list[SamplingMarket]:
        """Fetch reward-eligible sampling markets."""
        data = await self._get("/sampling-markets")
        if isinstance(data, list):
            return [SamplingMarket.model_validate(m) for m in data]
        # Sometimes wrapped in {"data": [...]}
        items = data.get("data", data) if isinstance(data, dict) else []
        return [SamplingMarket.model_validate(m) for m in items]

    async def get_sampling_simplified_markets(self) -> list[SamplingSimplifiedMarket]:
        """Fetch simplified sampling markets with reward metadata."""
        data = await self._get("/sampling-simplified-markets")
        if isinstance(data, list):
            return [SamplingSimplifiedMarket.model_validate(m) for m in data]
        items = data.get("data", data) if isinstance(data, dict) else []
        return [SamplingSimplifiedMarket.model_validate(m) for m in items]

    async def get_spread(self, token_id: str) -> dict[str, Any]:
        """Get spread for a token ID."""
        data = await self._get("/spread", params={"token_id": token_id})
        return data

    async def get_price(self, token_id: str, side: str = "BUY") -> float:
        """Get price for a token ID and side."""
        data = await self._get("/price", params={"token_id": token_id, "side": side})
        return float(data.get("price", 0.0))


class ClobRestartError(Exception):
    """Raised when matching engine is restarting (HTTP 425)."""


class ClobRateLimitError(Exception):
    """Raised on rate limit (HTTP 429)."""


class ClobPausedError(Exception):
    """Raised when exchange is paused / cancel-only (HTTP 503)."""

"""Gamma API client — market/event discovery for Polymarket."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import aiohttp
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class GammaMarket(BaseModel):
    """A market (condition) as returned by the Gamma API."""

    condition_id: str = Field(alias="conditionId", default="")
    question: str = ""
    description: str = ""
    market_slug: str = Field(alias="slug", default="")
    end_date_iso: str = Field(alias="endDateIso", default="")
    game_start_time: str | None = Field(alias="gameStartTime", default=None)
    active: bool = True
    closed: bool = False
    archived: bool = False
    accepting_orders: bool = Field(alias="acceptingOrders", default=True)
    enable_order_book: bool = Field(alias="enableOrderBook", default=True)
    neg_risk: bool = Field(alias="negRisk", default=False)
    neg_risk_market_id: str = Field(alias="negRiskMarketID", default="")
    neg_risk_request_id: str = Field(alias="negRiskRequestID", default="")
    volume_24hr: float = Field(alias="volume24hr", default=0.0)
    volume: float = 0.0
    liquidity: float = 0.0
    spread: float = 0.0
    best_bid: float = Field(alias="bestBid", default=0.0)
    best_ask: float = Field(alias="bestAsk", default=0.0)
    last_trade_price: float = Field(alias="lastTradePrice", default=0.0)
    outcome_prices: str = Field(alias="outcomePrices", default="")
    clob_token_ids: str = Field(alias="clobTokenIds", default="")
    rewards_min_size: float = Field(alias="rewardsMinSize", default=0.0)
    rewards_max_spread: float = Field(alias="rewardsMaxSpread", default=0.0)
    rewards_daily_rate: float = Field(alias="rewardsDailyRate", default=0.0)
    fees_enabled: bool = Field(alias="feesEnabled", default=False)
    fee_rate: float = Field(alias="feeRate", default=0.0)
    events: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def token_ids(self) -> list[str]:
        """Parse clobTokenIds JSON string into list."""
        if not self.clob_token_ids:
            return []
        import json
        try:
            result: list[str] = json.loads(self.clob_token_ids)
            return result
        except (json.JSONDecodeError, TypeError):
            return []

    @property
    def outcome_prices_list(self) -> list[float]:
        """Parse outcomePrices JSON string into list of floats."""
        if not self.outcome_prices:
            return []
        import json
        try:
            return [float(p) for p in json.loads(self.outcome_prices)]
        except (json.JSONDecodeError, TypeError, ValueError):
            return []

    @property
    def primary_event_id(self) -> str:
        """Best-effort event id from nested Gamma market metadata."""
        if not self.events:
            return ""
        first_event = self.events[0] or {}
        return str(first_event.get("id", "") or "")

    @property
    def end_date(self) -> datetime | None:
        """Parse end_date_iso into datetime."""
        if not self.end_date_iso:
            return None
        try:
            return datetime.fromisoformat(self.end_date_iso.replace("Z", "+00:00"))
        except ValueError:
            return None


class GammaEvent(BaseModel):
    """An event as returned by the Gamma API."""

    id: str = ""
    slug: str = ""
    title: str = ""
    description: str = ""
    active: bool = True
    closed: bool = False
    archived: bool = False
    neg_risk: bool = Field(alias="negRisk", default=False)
    neg_risk_market_id: str = Field(alias="negRiskMarketID", default="")
    volume: float = 0.0
    liquidity: float = 0.0
    start_date: str = Field(alias="startDate", default="")
    end_date: str = Field(alias="endDate", default="")
    markets: list[GammaMarket] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class GammaClient:
    """Async HTTP client for the Gamma API (market/event discovery)."""

    def __init__(self, base_url: str = "https://gamma-api.polymarket.com") -> None:
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"Accept": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        logger.debug("gamma_request", url=url, params=params)
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data

    async def get_active_events(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[GammaEvent]:
        """Fetch active, non-closed events with their markets.

        Paginates automatically if needed.
        """
        all_events: list[GammaEvent] = []
        current_offset = offset

        while True:
            params: dict[str, Any] = {
                "active": "true",
                "closed": "false",
                "limit": str(limit),
                "offset": str(current_offset),
            }
            data = await self._get("/events", params=params)

            if not data:
                break

            batch = [GammaEvent.model_validate(e) for e in data]
            all_events.extend(batch)

            if len(batch) < limit:
                break
            current_offset += limit

        logger.info("gamma_events_fetched", count=len(all_events))
        return all_events

    async def get_event_markets(self, event_id: str) -> list[GammaMarket]:
        """Fetch all markets (conditions) for a specific event."""
        params: dict[str, Any] = {"id": event_id}
        data = await self._get("/events", params=params)

        if not data:
            return []

        event = GammaEvent.model_validate(data[0])
        logger.info("gamma_event_markets", event_id=event_id, count=len(event.markets))
        return event.markets

    async def get_markets(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "",
        ascending: bool = False,
        max_pages: int = 0,
    ) -> list[GammaMarket]:
        """Fetch markets directly (not via events).

        Args:
            max_pages: Maximum number of pages to fetch (0 = unlimited).
        """
        all_markets: list[GammaMarket] = []
        current_offset = offset
        pages_fetched = 0

        while True:
            params: dict[str, Any] = {
                "active": str(active).lower(),
                "closed": str(closed).lower(),
                "limit": str(limit),
                "offset": str(current_offset),
            }
            if order_by:
                params["order"] = order_by
                params["ascending"] = str(ascending).lower()
            data = await self._get("/markets", params=params)

            if not data:
                break

            batch = [GammaMarket.model_validate(m) for m in data]
            all_markets.extend(batch)
            pages_fetched += 1

            if len(batch) < limit:
                break
            if max_pages > 0 and pages_fetched >= max_pages:
                break
            current_offset += limit

        logger.info("gamma_markets_fetched", count=len(all_markets))
        return all_markets

    async def get_market_by_condition(self, condition_id: str) -> GammaMarket | None:
        """Fetch a single market by condition ID."""
        params = {"conditionId": condition_id}
        data = await self._get("/markets", params=params)
        if data:
            return GammaMarket.model_validate(data[0])
        return None

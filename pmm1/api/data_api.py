"""Data API client — positions, trades, price history from Polymarket Data API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import aiohttp
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class Position(BaseModel):
    """A position (conditional token holding)."""

    asset: str = ""
    condition_id: str = Field(alias="conditionId", default="")
    market_slug: str = Field(alias="marketSlug", default="")
    title: str = ""
    outcome: str = ""
    size: float = 0.0
    avg_price: float = Field(alias="avgPrice", default=0.0)
    cur_price: float = Field(alias="curPrice", default=0.0)
    initial_value: float = Field(alias="initialValue", default=0.0)
    current_value: float = Field(alias="currentValue", default=0.0)
    pnl: float = 0.0
    realized_pnl: float = Field(alias="realizedPnl", default=0.0)
    percent_pnl: float = Field(alias="percentPnl", default=0.0)
    cashflow: float = 0.0
    redeemed: bool = False
    merge_order_id: str = Field(alias="mergeOrderId", default="")
    neg_risk: bool = Field(alias="negRisk", default=False)

    model_config = {"populate_by_name": True}


class Trade(BaseModel):
    """A completed trade."""

    id: str = ""
    taker_order_id: str = Field(alias="takerOrderId", default="")
    market: str = ""
    asset_id: str = Field(alias="asset_id", default="")
    side: str = ""
    type: str = ""  # TRADE, MERGE, SPLIT
    price: str = ""
    size: str = ""
    fee: str = "0"
    timestamp: str = ""
    status: str = ""
    match_time: str = Field(alias="matchTime", default="")
    outcome: str = ""
    owner: str = ""
    maker_address: str = Field(alias="makerAddress", default="")
    trader_side: str = Field(alias="traderSide", default="")
    transaction_hash: str = Field(alias="transactionHash", default="")
    bucket_index: int = Field(alias="bucketIndex", default=0)

    model_config = {"populate_by_name": True}

    @property
    def price_float(self) -> float:
        return float(self.price) if self.price else 0.0

    @property
    def size_float(self) -> float:
        return float(self.size) if self.size else 0.0

    @property
    def fee_float(self) -> float:
        return float(self.fee) if self.fee else 0.0

    @property
    def timestamp_dt(self) -> datetime | None:
        if not self.timestamp:
            return None
        try:
            return datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None


class PricePoint(BaseModel):
    """A single price history point."""

    t: int = 0  # Unix timestamp
    p: float = 0.0  # Price

    @property
    def timestamp(self) -> datetime:
        return datetime.utcfromtimestamp(self.t)


class DataApiClient:
    """Async client for Polymarket Data API (positions, trades, price history)."""

    def __init__(
        self,
        base_url: str = "https://data-api.polymarket.com",
    ) -> None:
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
        logger.debug("data_api_request", url=url, params=params)
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_positions(
        self,
        address: str,
        market: str | None = None,
        redeemed: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Position]:
        """Fetch positions for an address, optionally filtered.

        Args:
            address: Wallet address to query.
            market: Optional condition_id filter.
            redeemed: If set, filter by redemption status.
            limit: Max results per page.
            offset: Pagination offset.
        """
        all_positions: list[Position] = []
        current_offset = offset

        while True:
            params: dict[str, Any] = {
                "user": address,  # API uses "user" not "address"
                "limit": str(limit),
                "offset": str(current_offset),
            }
            if market:
                params["market"] = market
            if redeemed is not None:
                params["redeemable"] = str(redeemed).lower()  # API uses "redeemable"

            data = await self._get("/positions", params=params)
            items = data if isinstance(data, list) else data.get("positions", [])

            if not items:
                break

            batch = [Position.model_validate(p) for p in items]
            all_positions.extend(batch)

            if len(batch) < limit:
                break
            current_offset += limit

        logger.info("positions_fetched", count=len(all_positions), address=address[:10])
        return all_positions

    async def get_trades(
        self,
        address: str | None = None,
        market: str | None = None,
        asset_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        before: str | None = None,
        after: str | None = None,
    ) -> list[Trade]:
        """Fetch trades with optional filters.

        Args:
            address: Wallet address.
            market: Condition ID.
            asset_id: Token ID.
            limit: Max results per page.
            offset: Pagination offset.
            before: ISO timestamp upper bound.
            after: ISO timestamp lower bound.
        """
        all_trades: list[Trade] = []
        current_offset = offset

        while True:
            params: dict[str, Any] = {
                "limit": str(limit),
                "offset": str(current_offset),
            }
            if address:
                params["address"] = address
            if market:
                params["market"] = market
            if asset_id:
                params["asset_id"] = asset_id
            if before:
                params["before"] = before
            if after:
                params["after"] = after

            data = await self._get("/trades", params=params)
            items = data if isinstance(data, list) else data.get("trades", [])

            if not items:
                break

            batch = [Trade.model_validate(t) for t in items]
            all_trades.extend(batch)

            if len(batch) < limit:
                break
            current_offset += limit

        logger.info("trades_fetched", count=len(all_trades))
        return all_trades

    async def get_prices_history(
        self,
        token_id: str,
        interval: str = "1h",
        fidelity: int = 60,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[PricePoint]:
        """Fetch historical prices for a token.

        Args:
            token_id: The CLOB token ID.
            interval: Time interval (1m, 5m, 1h, 1d, etc.).
            fidelity: Number of data points.
            start_ts: Start unix timestamp.
            end_ts: End unix timestamp.
        """
        params: dict[str, Any] = {
            "market": token_id,
            "interval": interval,
            "fidelity": str(fidelity),
        }
        if start_ts:
            params["startTs"] = str(start_ts)
        if end_ts:
            params["endTs"] = str(end_ts)

        data = await self._get("/prices-history", params=params)
        history = data if isinstance(data, list) else data.get("history", [])

        points = [PricePoint.model_validate(p) for p in history]
        logger.debug("prices_history_fetched", token_id=token_id[:16], count=len(points))
        return points

    async def get_market_trades_history(
        self,
        condition_id: str,
        limit: int = 100,
    ) -> list[Trade]:
        """Fetch recent trade history for a market (not user-specific)."""
        params: dict[str, Any] = {
            "market": condition_id,
            "limit": str(limit),
        }
        data = await self._get("/trades", params=params)
        items = data if isinstance(data, list) else data.get("trades", [])
        return [Trade.model_validate(t) for t in items]

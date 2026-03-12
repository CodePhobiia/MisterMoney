"""CLOB Private API client — authenticated endpoints for order management.

Uses the official py-clob-client SDK for EIP-712 order signing and submission.
Our class stays async but delegates to the synchronous SDK via asyncio.to_thread().
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from decimal import Decimal
from enum import Enum
from typing import Any

import aiohttp
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    GTC = "GTC"
    GTD = "GTD"
    FOK = "FOK"
    FAK = "FAK"


class OrderStatus(str, Enum):
    LIVE = "LIVE"
    MATCHED = "MATCHED"
    DELAYED = "DELAYED"
    MINED = "MINED"
    CONFIRMED = "CONFIRMED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


class CreateOrderRequest(BaseModel):
    """Request to create an order."""

    token_id: str
    price: str
    size: str
    side: OrderSide
    order_type: OrderType = OrderType.GTC
    expiration: int = 0  # Unix timestamp for GTD; 0 for GTC
    neg_risk: bool = False
    post_only: bool = True
    fee_rate_bps: int = 0
    tick_size: str = "0.01"  # Tick size for SDK signing

    @property
    def price_decimal(self) -> Decimal:
        return Decimal(self.price)

    @property
    def size_decimal(self) -> Decimal:
        return Decimal(self.size)


class OrderResponse(BaseModel):
    """Response from order creation."""

    id: str = ""
    status: str = ""
    order_id: str = Field(alias="orderID", default="")
    success: bool = False
    error_msg: str = Field(alias="errorMsg", default="")
    transaction_hashes: list[str] = Field(alias="transactionsHashes", default_factory=list)

    model_config = {"populate_by_name": True}


class OpenOrder(BaseModel):
    """An open order on the book."""

    id: str = ""
    order_id: str = Field(alias="orderID", default="")
    market: str = ""
    asset_id: str = Field(alias="asset_id", default="")
    side: str = ""
    price: str = ""
    size_matched: str = Field(alias="sizeMatched", default="0")
    original_size: str = Field(alias="originalSize", default="0")
    status: str = ""
    outcome: str = ""
    owner: str = ""
    expiration: str = "0"
    created_at: str | int = Field(alias="createdAt", default="")
    associate_trades: list[dict[str, Any]] = Field(alias="associateTrades", default_factory=list)

    model_config = {"populate_by_name": True}

    @property
    def remaining_size(self) -> float:
        return float(self.original_size) - float(self.size_matched)

    @property
    def price_float(self) -> float:
        return float(self.price)


class BalanceResponse(BaseModel):
    """Account balance information."""

    # USDC balance fields vary; parse flexibly
    allowance: str = "0"
    balance: str = "0"

    @property
    def balance_float(self) -> float:
        return float(self.balance) if self.balance else 0.0

    @property
    def allowance_float(self) -> float:
        return float(self.allowance) if self.allowance else 0.0


class HeartbeatResponse(BaseModel):
    """Heartbeat response from server."""

    heartbeat_id: str = Field(alias="heartbeatID", default="")
    success: bool = True

    model_config = {"populate_by_name": True}


def _build_hmac_signature(
    api_secret: str,
    timestamp: str,
    method: str,
    path: str,
    body: str = "",
) -> str:
    """Build HMAC-SHA256 signature for API authentication.

    Matches the py-clob-client SDK signing:
    - Secret is base64url-decoded before use as HMAC key
    - Message is: timestamp + method + path + body
    - Result is base64url-encoded
    """
    import base64

    base64_secret = base64.urlsafe_b64decode(api_secret)
    message = str(timestamp) + method.upper() + path
    if body:
        # Match SDK: replace single quotes with double quotes for consistency
        message += body.replace("'", '"')
    h = hmac.new(base64_secret, message.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(h.digest()).decode("utf-8")


class ClobPrivateClient:
    """Async authenticated CLOB client for order management.

    Uses the official py-clob-client SDK (synchronous) for order creation/posting,
    wrapped in asyncio.to_thread() to stay async. The SDK handles EIP-712 signing,
    order serialization, and HMAC auth headers for order operations.

    For non-order endpoints (cancel, heartbeat, balances, open orders), we use
    direct REST calls with HMAC auth headers (Level 2 auth).
    """

    def __init__(
        self,
        base_url: str = "https://clob.polymarket.com",
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        funder: str = "",
        wallet_address: str = "",
        private_key: str = "",
        chain_id: int = 137,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.funder = funder
        self.wallet_address = wallet_address
        self.private_key = private_key
        self.chain_id = chain_id
        self._session: aiohttp.ClientSession | None = None
        self._sdk_client = None  # Lazy-initialized py-clob-client ClobClient

    def _get_sdk_client(self):
        """Lazy-initialize the synchronous py-clob-client SDK client.

        This handles EIP-712 signing for order creation/posting.
        """
        if self._sdk_client is None:
            if not self.private_key:
                raise ClobAuthError(
                    "private_key is required for live order signing. "
                    "Set PRIVATE_KEY in .env"
                )

            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self.api_key,
                api_secret=self.api_secret,
                api_passphrase=self.api_passphrase,
            )

            self._sdk_client = ClobClient(
                host=self.base_url,
                chain_id=self.chain_id,
                key=self.private_key,
                creds=creds,
                signature_type=0,  # EOA (standard ECDSA EIP-712)
                funder=self.funder or self.wallet_address,
            )

            logger.info(
                "sdk_client_initialized",
                host=self.base_url,
                chain_id=self.chain_id,
                signature_type=0,
                funder=self.funder or self.wallet_address,
            )

        return self._sdk_client

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

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """Build authentication headers for a request.

        Matches the py-clob-client SDK header format (Level 2 auth):
        POLY_ADDRESS, POLY_SIGNATURE, POLY_TIMESTAMP, POLY_API_KEY, POLY_PASSPHRASE
        """
        timestamp = str(int(time.time()))
        signature = _build_hmac_signature(
            self.api_secret, timestamp, method, path, body
        )
        headers = {
            "POLY_ADDRESS": self.wallet_address,
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.api_passphrase,
        }
        if self.funder:
            headers["POLY_FUNDER"] = self.funder
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated request."""
        session = await self._get_session()
        url = f"{self.base_url}{path}"

        import json as json_mod
        # Use compact separators matching SDK: json.dumps(body, separators=(",", ":"))
        body_str = json_mod.dumps(json_body, separators=(",", ":"), ensure_ascii=False) if json_body is not None else ""
        auth_headers = self._auth_headers(method.upper(), path, body_str)

        headers = {
            **auth_headers,
            "Content-Type": "application/json",
        }

        logger.debug("clob_private_request", method=method, path=path)

        # Send pre-serialized body to match the signature exactly
        async with session.request(
            method, url, data=body_str if body_str else None, params=params, headers=headers
        ) as resp:
            if resp.status == 425:
                raise ClobRestartError("Matching engine restarting (425)")
            if resp.status == 429:
                raise ClobRateLimitError("Rate limited (429)")
            if resp.status == 503:
                raise ClobPausedError("Exchange paused / cancel-only (503)")
            if resp.status in (400, 401):
                error_body = await resp.text()
                logger.error(
                    "clob_auth_error",
                    status=resp.status,
                    body=error_body,
                    path=path,
                )
                raise ClobAuthError(f"Auth error {resp.status}: {error_body}")
            resp.raise_for_status()
            return await resp.json()

    async def create_order(self, order: CreateOrderRequest) -> OrderResponse:
        """Submit a signed order to the CLOB.

        Uses the py-clob-client SDK for proper EIP-712 order signing.
        The SDK handles:
        - Creating the EIP-712 typed data structure
        - Signing with the private key
        - Serializing with order_to_json()
        - Posting with proper HMAC auth headers
        """
        from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
        from py_clob_client.clob_types import OrderType as SdkOrderType

        sdk_client = self._get_sdk_client()

        # Map our OrderType to SDK OrderType
        sdk_order_type_map = {
            OrderType.GTC: SdkOrderType.GTC,
            OrderType.GTD: SdkOrderType.GTD,
            OrderType.FOK: SdkOrderType.FOK,
            OrderType.FAK: SdkOrderType.FAK,
        }
        sdk_order_type = sdk_order_type_map.get(order.order_type, SdkOrderType.GTC)

        # Build SDK OrderArgs
        order_args = OrderArgs(
            token_id=order.token_id,
            price=float(order.price),
            size=float(order.size),
            side=order.side.value,
            fee_rate_bps=order.fee_rate_bps,
            expiration=order.expiration,
        )

        # Determine tick_size string for SDK
        tick_size_str = order.tick_size if order.tick_size else "0.01"

        options = PartialCreateOrderOptions(
            tick_size=tick_size_str,
            neg_risk=order.neg_risk if order.neg_risk else None,
        )

        def _create_and_post():
            """Synchronous SDK call — run in thread."""
            signed_order = sdk_client.create_order(order_args, options)
            return sdk_client.post_order(
                signed_order,
                orderType=sdk_order_type,
                post_only=order.post_only,
            )

        try:
            data = await asyncio.to_thread(_create_and_post)
        except Exception as e:
            error_str = str(e)
            logger.error(
                "sdk_order_creation_failed",
                token_id=order.token_id,
                side=order.side.value,
                price=order.price,
                size=order.size,
                error=error_str,
            )
            # Map SDK exceptions to our exception types
            if "rate" in error_str.lower() or "429" in error_str:
                raise ClobRateLimitError(error_str) from e
            if "425" in error_str or "restart" in error_str.lower():
                raise ClobRestartError(error_str) from e
            if "503" in error_str or "paused" in error_str.lower():
                raise ClobPausedError(error_str) from e
            raise ClobAuthError(error_str) from e

        # SDK returns a dict-like response; normalize to OrderResponse
        response: OrderResponse
        if isinstance(data, dict):
            response = OrderResponse.model_validate(data)
        # If the SDK returns a requests.Response, extract json
        elif hasattr(data, 'json'):
            try:
                json_data = data.json()
                response = OrderResponse.model_validate(json_data)
            except Exception:
                response = OrderResponse(
                    success=True,
                    status="SUBMITTED",
                )
        else:
            response = OrderResponse(success=True, status="SUBMITTED")

        order_id = response.order_id or response.id
        if response.success and order_id:
            logger.info(
                "order_submitted",
                token_id=order.token_id,
                order_id=order_id,
                side=order.side.value,
                price=order.price,
                size=order.size,
                status=response.status,
            )
        # If the SDK returns a requests.Response, extract json
        else:
            logger.warning(
                "order_submission_rejected",
                token_id=order.token_id,
                side=order.side.value,
                price=order.price,
                size=order.size,
                status=response.status,
                error=response.error_msg or "missing_order_id",
            )

        return response

    async def create_orders_batch(
        self, orders: list[CreateOrderRequest]
    ) -> list[OrderResponse]:
        """Submit orders concurrently with semaphore for rate limiting.

        Creates and posts each order individually through the SDK since
        the SDK doesn't have a native batch method that handles signing.
        Uses asyncio.gather for concurrent submission with semaphore(10).
        """
        if not orders:
            return []

        sem = asyncio.Semaphore(10)  # Max 10 concurrent submissions

        async def submit_one(order: CreateOrderRequest) -> OrderResponse:
            async with sem:
                try:
                    return await self.create_order(order)
                except Exception as e:
                    logger.error(
                        "batch_order_failed",
                        token_id=order.token_id,
                        error=str(e),
                    )
                    return OrderResponse(
                        success=False,
                        error_msg=str(e),
                    )

        responses = await asyncio.gather(*[submit_one(o) for o in orders])
        logger.info("orders_batch_created", count=len(orders), success=sum(1 for r in responses if r.success))
        return list(responses)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel a single order by ID.

        Uses the SDK client for proper auth header generation.
        """
        if self.private_key:
            sdk_client = self._get_sdk_client()
            data = await asyncio.to_thread(sdk_client.cancel, order_id)
            logger.info("order_canceled", order_id=order_id)
            if isinstance(data, dict):
                return data
            if hasattr(data, 'json'):
                try:
                    return data.json()
                except Exception:
                    return {"success": True}
            return {"success": True}

        # Fallback to direct REST
        data = await self._request(
            "DELETE", "/order", json_body={"orderID": order_id}
        )
        logger.info("order_canceled", order_id=order_id)
        return data

    async def cancel_orders(self, order_ids: list[str]) -> dict[str, Any]:
        """Cancel multiple orders.

        Uses the SDK client for proper auth header generation.
        """
        if self.private_key:
            sdk_client = self._get_sdk_client()
            data = await asyncio.to_thread(sdk_client.cancel_orders, order_ids)
            logger.info("orders_canceled", count=len(order_ids))
            if isinstance(data, dict):
                return data
            if hasattr(data, 'json'):
                try:
                    return data.json()
                except Exception:
                    return {"success": True}
            return {"success": True}

        # Fallback to direct REST
        data = await self._request(
            "DELETE", "/orders", json_body=[{"orderID": oid} for oid in order_ids]
        )
        logger.info("orders_canceled", count=len(order_ids))
        return data

    async def cancel_all(self) -> dict[str, Any]:
        """Cancel all open orders for this account.

        Uses the SDK client for proper auth header generation.
        """
        if self.private_key:
            sdk_client = self._get_sdk_client()
            data = await asyncio.to_thread(sdk_client.cancel_all)
            logger.warning("all_orders_canceled")
            if isinstance(data, dict):
                return data
            if hasattr(data, 'json'):
                try:
                    return data.json()
                except Exception:
                    return {"success": True}
            return {"success": True}

        # Fallback to direct REST
        data = await self._request("DELETE", "/cancel-all")
        logger.warning("all_orders_canceled")
        return data

    async def cancel_market_orders(
        self, market: str, asset_id: str | None = None
    ) -> dict[str, Any]:
        """Cancel all orders for a specific market."""
        payload: dict[str, Any] = {"market": market}
        if asset_id:
            payload["asset_id"] = asset_id
        data = await self._request("DELETE", "/cancel-market-orders", json_body=payload)
        logger.info("market_orders_canceled", market=market, asset_id=asset_id)
        return data

    async def get_open_orders(
        self,
        market: str | None = None,
        asset_id: str | None = None,
    ) -> list[OpenOrder]:
        """Get all open orders, optionally filtered by market or asset.

        Uses GET /data/orders per the SDK (not /orders which is POST-only).
        """
        params: dict[str, Any] = {}
        if market:
            params["market"] = market
        if asset_id:
            params["asset_id"] = asset_id

        data = await self._request("GET", "/data/orders", params=params)
        # SDK returns paginated: {"data": [...], "next_cursor": "..."}
        if isinstance(data, dict):
            orders_data = data.get("data", data.get("orders", []))
        else:
            orders_data = data
        orders = [OpenOrder.model_validate(o) for o in orders_data]
        logger.debug("open_orders_fetched", count=len(orders))
        return orders

    async def send_heartbeat(self, heartbeat_id: str | None = None) -> HeartbeatResponse:
        """Send a REST heartbeat to keep orders alive.

        Must be sent every 5s (with 10s + 5s server grace).
        Failure → server cancels all orders.

        Uses POST /v1/heartbeats with a JSON body per the SDK.
        """
        payload = {"heartbeat_id": heartbeat_id}
        data = await self._request("POST", "/v1/heartbeats", json_body=payload)
        resp = HeartbeatResponse.model_validate(data)
        logger.debug("heartbeat_sent", heartbeat_id=resp.heartbeat_id)
        return resp

    async def get_balances(self, signature_type: int = 0) -> dict[str, Any]:
        """Get account balances (USDC allowance + balance).

        Uses GET /balance-allowance per the SDK.
        Requires asset_type (CONDITIONAL or COLLATERAL) and signature_type.
        """
        params = {
            "asset_type": "COLLATERAL",
            "token_id": "",
            "signature_type": str(signature_type),
        }
        data = await self._request("GET", "/balance-allowance", params=params)
        logger.debug("balances_fetched", data=data)
        return data


class ClobRestartError(Exception):
    """HTTP 425 — matching engine restarting."""


class ClobRateLimitError(Exception):
    """HTTP 429 — rate limited."""


class ClobPausedError(Exception):
    """HTTP 503 — exchange paused / cancel-only."""


class ClobAuthError(Exception):
    """HTTP 400/401 — authentication error."""

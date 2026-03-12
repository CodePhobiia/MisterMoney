"""Order manager — diff desired vs live, sign, submit from §12.

Per cycle: cancel stale → submit new → reconcile

Rules:
- postOnly=true with GTC or short GTD
- TTL default: 20–45s effective
- Reprice on: price move ≥1 tick, size move ≥20%, age>TTL,
  tick size change, fill changes inventory
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import structlog
from pydantic import BaseModel, Field

from pmm1.api.clob_private import (
    ClobAuthError,
    ClobPausedError,
    ClobPrivateClient,
    ClobRateLimitError,
    ClobRestartError,
    CreateOrderRequest,
    OrderSide,
    OrderType,
)
from pmm1.execution.batcher import OrderBatcher
from pmm1.execution.tick_rounding import (
    compute_gtd_expiration,
    price_to_string,
    round_ask,
    round_bid,
    round_size,
)
from pmm1.state.orders import OrderState, OrderTracker, TrackedOrder
from pmm1.strategy.quote_engine import QuoteIntent

logger = structlog.get_logger(__name__)


class OrderDiff(BaseModel):
    """Diff between desired and live orders."""

    to_cancel: list[str] = Field(default_factory=list)  # order_ids to cancel
    to_submit: list[CreateOrderRequest] = Field(default_factory=list)  # new orders
    unchanged: list[str] = Field(default_factory=list)  # order_ids left alone


class OrderSubmission(BaseModel):
    """Submission request plus tracking metadata."""

    request: CreateOrderRequest
    condition_id: str = ""
    strategy: str = ""


class OrderManager:
    """Manages the full order lifecycle: diff, cancel stale, submit new.

    Implements the order management rules from §12.
    """

    def __init__(
        self,
        client: ClobPrivateClient,
        order_tracker: OrderTracker,
        batcher: OrderBatcher | None = None,
        order_ttl_s: int = 30,
        use_gtd: bool = True,
        post_only: bool = True,
        reprice_threshold_ticks: float = 1.0,
        reprice_threshold_size_pct: float = 0.20,
    ) -> None:
        self._client = client
        self._tracker = order_tracker
        self._batcher = batcher or OrderBatcher()
        self._order_ttl_s = order_ttl_s
        self._use_gtd = use_gtd
        self._post_only = post_only
        self._reprice_threshold_ticks = reprice_threshold_ticks
        self._reprice_threshold_size_pct = reprice_threshold_size_pct
        self._server_time_offset: int = 0

    def set_server_time_offset(self, server_time: int) -> None:
        """Set offset between local and server time."""
        self._server_time_offset = server_time - int(time.time())

    def _get_server_time(self) -> int:
        return int(time.time()) + self._server_time_offset

    def _should_reprice(
        self,
        live: TrackedOrder,
        desired_price: float,
        desired_size: float,
        tick_size: Decimal,
    ) -> bool:
        """Check if a live order should be repriced.

        Reprice when:
        - Price moved ≥ 1 tick
        - Size changed ≥ 20%
        - Order age > TTL
        - Tick size changed (handled externally)
        """
        # Age check
        if live.age_seconds > self._order_ttl_s:
            return True

        # Price check
        tick_float = float(tick_size)
        price_diff = abs(live.price_float - desired_price)
        if price_diff >= tick_float * self._reprice_threshold_ticks:
            return True

        # Size check
        if live.remaining_size_float > 0:
            size_ratio = abs(live.remaining_size_float - desired_size) / live.remaining_size_float
            if size_ratio >= self._reprice_threshold_size_pct:
                return True

        return False

    def compute_diff(
        self,
        intent: QuoteIntent,
        tick_size: Decimal = Decimal("0.01"),
    ) -> OrderDiff:
        """Compute diff between desired quote intent and live orders.

        Returns:
            OrderDiff with lists of orders to cancel, submit, or leave.
        """
        diff = OrderDiff()

        # Get current live orders for this token
        live_bids = self._tracker.get_active_by_side(intent.token_id, "BUY")
        live_asks = self._tracker.get_active_by_side(intent.token_id, "SELL")

        # Process bid side
        if intent.has_bid:
            bid_price = round_bid(intent.bid_price, tick_size)
            bid_size = round_size(intent.bid_size)
            bid_price_str = price_to_string(bid_price)
            bid_size_str = str(bid_size)

            # Check if any live bid matches
            matched = False
            for live in live_bids:
                if not self._should_reprice(live, float(bid_price), float(bid_size), tick_size):
                    diff.unchanged.append(live.order_id)
                    matched = True
                else:
                    diff.to_cancel.append(live.order_id)

            if not matched:
                # Cancel all live bids and submit new one
                for live in live_bids:
                    if live.order_id not in diff.to_cancel and live.order_id not in diff.unchanged:
                        diff.to_cancel.append(live.order_id)

                expiration = 0
                order_type = OrderType.GTC
                if self._use_gtd:
                    expiration = compute_gtd_expiration(
                        self._order_ttl_s, self._get_server_time()
                    )
                    order_type = OrderType.GTD

                diff.to_submit.append(CreateOrderRequest(
                    token_id=intent.token_id,
                    price=bid_price_str,
                    size=bid_size_str,
                    side=OrderSide.BUY,
                    order_type=order_type,
                    expiration=expiration,
                    neg_risk=intent.neg_risk,
                    post_only=self._post_only,
                    tick_size=str(tick_size),
                ))
        else:
            # No desired bid → cancel all live bids
            for live in live_bids:
                diff.to_cancel.append(live.order_id)

        # Process ask side
        if intent.has_ask:
            ask_price = round_ask(intent.ask_price, tick_size)
            ask_size = round_size(intent.ask_size)
            ask_price_str = price_to_string(ask_price)
            ask_size_str = str(ask_size)

            matched = False
            for live in live_asks:
                if not self._should_reprice(live, float(ask_price), float(ask_size), tick_size):
                    diff.unchanged.append(live.order_id)
                    matched = True
                else:
                    diff.to_cancel.append(live.order_id)

            if not matched:
                for live in live_asks:
                    if live.order_id not in diff.to_cancel and live.order_id not in diff.unchanged:
                        diff.to_cancel.append(live.order_id)

                expiration = 0
                order_type = OrderType.GTC
                if self._use_gtd:
                    expiration = compute_gtd_expiration(
                        self._order_ttl_s, self._get_server_time()
                    )
                    order_type = OrderType.GTD

                diff.to_submit.append(CreateOrderRequest(
                    token_id=intent.token_id,
                    price=ask_price_str,
                    size=ask_size_str,
                    side=OrderSide.SELL,
                    order_type=order_type,
                    expiration=expiration,
                    neg_risk=intent.neg_risk,
                    post_only=self._post_only,
                    tick_size=str(tick_size),
                ))
        else:
            for live in live_asks:
                diff.to_cancel.append(live.order_id)

        return diff

    def _process_submission_response(
        self,
        submission: OrderSubmission,
        response: Any,
    ) -> dict[str, Any]:
        """Normalize one submission response and update canonical tracking."""
        req = submission.request
        order_id = getattr(response, "order_id", "") or getattr(response, "id", "")
        success = bool(getattr(response, "success", False))
        error_msg = getattr(response, "error_msg", "") or ""

        if not success or not order_id:
            logger.warning(
                "order_submission_rejected",
                token_id=req.token_id[:16],
                side=req.side.value,
                price=req.price,
                size=req.size,
                strategy=submission.strategy or "?",
                status=getattr(response, "status", ""),
                error=error_msg or "missing_order_id",
            )
            return {
                "order_id": order_id,
                "success": False,
                "error": error_msg or "missing_order_id",
            }

        tracked = TrackedOrder(
            order_id=order_id,
            token_id=req.token_id,
            condition_id=submission.condition_id,
            side=req.side.value,
            price=req.price,
            original_size=req.size,
            remaining_size=req.size,
            state=OrderState.SUBMITTED,
            neg_risk=req.neg_risk,
            post_only=req.post_only,
            order_type=req.order_type.value,
            strategy=submission.strategy,
            transaction_hashes=list(getattr(response, "transaction_hashes", [])),
        )
        self._tracker.track_submitted(tracked, source=submission.strategy or "submit")
        return {"order_id": order_id, "success": True, "error": ""}

    async def _submit_orders(
        self,
        submissions: list[OrderSubmission],
    ) -> list[dict[str, Any]]:
        """Submit orders and update canonical tracking for successful responses."""
        if not submissions:
            return []

        results: list[dict[str, Any]] = []
        for batch in self._batcher.batch(submissions):
            requests = [submission.request for submission in batch]
            responses = await self._client.create_orders_batch(requests)
            if len(responses) != len(batch):
                logger.error(
                    "order_submission_batch_mismatch",
                    requested=len(batch),
                    received=len(responses),
                )

            for submission, response in zip(batch, responses):
                results.append(self._process_submission_response(submission, response))

        return results

    async def submit_order(
        self,
        request: CreateOrderRequest,
        *,
        condition_id: str = "",
        strategy: str = "manual",
    ) -> dict[str, Any]:
        """Submit one order and track it if the exchange accepted it."""
        results = await self._submit_orders([
            OrderSubmission(
                request=request,
                condition_id=condition_id,
                strategy=strategy,
            )
        ])
        if results:
            return results[0]
        return {"order_id": "", "success": False, "error": "no_response"}

    async def diff_and_apply(
        self,
        intent: QuoteIntent,
        tick_size: Decimal = Decimal("0.01"),
    ) -> dict[str, Any]:
        """Compute diff and execute: cancel stale → submit new.

        Returns summary of actions taken.
        """
        diff = self.compute_diff(intent, tick_size)

        results: dict[str, Any] = {
            "canceled": 0,
            "submitted": 0,
            "rejected": 0,
            "unchanged": len(diff.unchanged),
            "errors": [],
        }

        # 1. Cancel stale orders
        if diff.to_cancel:
            try:
                # Deduplicate
                to_cancel = list(set(diff.to_cancel))
                await self._client.cancel_orders(to_cancel)
                for oid in to_cancel:
                    self._tracker.update_state(oid, OrderState.CANCELED, source="diff_cancel")
                results["canceled"] = len(to_cancel)
            except (ClobRestartError, ClobPausedError) as e:
                results["errors"].append(f"cancel_error: {e}")
                return results  # Don't submit if we can't cancel
            except ClobRateLimitError:
                results["errors"].append("cancel_rate_limited")
                await asyncio.sleep(1)
                return results
            except ClobAuthError as e:
                results["errors"].append(f"cancel_auth_error: {e}")
                return results
            except Exception as e:
                results["errors"].append(f"cancel_unexpected: {e}")
                logger.error("order_cancel_error", error=str(e))
                return results  # Don't submit if we can't cancel

        # 2. Submit new orders
        if diff.to_submit:
            try:
                submit_results = await self._submit_orders([
                    OrderSubmission(
                        request=req,
                        condition_id=intent.condition_id,
                        strategy=intent.strategy,
                    )
                    for req in diff.to_submit
                ])
                results["submitted"] = sum(1 for result in submit_results if result["success"])
                results["rejected"] = sum(1 for result in submit_results if not result["success"])
            except (ClobRestartError, ClobPausedError) as e:
                results["errors"].append(f"submit_error: {e}")
            except ClobRateLimitError:
                results["errors"].append("submit_rate_limited")
            except ClobAuthError as e:
                results["errors"].append(f"submit_auth_error: {e}")
            except Exception as e:
                results["errors"].append(f"submit_unexpected: {e}")
                logger.error("order_submit_error", error=str(e))

        if results["errors"] or results["rejected"]:
            logger.warning("order_cycle_errors", **results)
        else:
            logger.debug(
                "order_cycle_complete",
                token_id=intent.token_id[:16],
                **results,
            )

        return results

    async def cancel_all(self) -> bool:
        """Emergency: cancel ALL orders."""
        try:
            await self._client.cancel_all()
            # Mark all tracked active orders as canceled
            for order in self._tracker.get_active_orders():
                self._tracker.update_state(order.order_id, OrderState.CANCELED, source="cancel_all")
            logger.critical("emergency_cancel_all_executed")
            return True
        except Exception as e:
            logger.error("emergency_cancel_all_failed", error=str(e))
            return False

    async def cancel_market(self, token_id: str) -> bool:
        """Cancel all orders for a specific token."""
        try:
            active = self._tracker.get_active_orders(token_id)
            if not active:
                return True

            order_ids = [o.order_id for o in active if o.order_id]
            if order_ids:
                await self._client.cancel_orders(order_ids)
                for oid in order_ids:
                    self._tracker.update_state(oid, OrderState.CANCELED, source="cancel_market")

            logger.info("market_orders_canceled", token_id=token_id[:16], count=len(order_ids))
            return True
        except Exception as e:
            logger.error("market_cancel_failed", token_id=token_id[:16], error=str(e))
            return False

    async def submit_exit(
        self,
        token_id: str,
        condition_id: str,
        price: float,
        size: float,
        tick_size: Decimal = Decimal("0.01"),
        urgency: str = "high",
        neg_risk: bool = False,
    ) -> dict[str, Any]:
        """Submit an exit (sell) order — cancel existing orders for the token first.

        Used by ExitManager for stop-loss, take-profit, resolution, flatten, orphan exits.

        Pricing by urgency:
        - critical: best_bid - 2 ticks (must fill)
        - high: best_bid (fill now)
        - medium: best_bid (join queue)
        - low: best_bid (passive)
        """
        result: dict[str, Any] = {"submitted": False, "canceled": 0, "error": None}

        # 1. Cancel all existing orders for this token
        try:
            active = self._tracker.get_active_orders(token_id)
            if active:
                order_ids = [o.order_id for o in active if o.order_id]
                if order_ids:
                    await self._client.cancel_orders(order_ids)
                    for oid in order_ids:
                        self._tracker.update_state(oid, OrderState.CANCELED, source="exit_cancel")
                    result["canceled"] = len(order_ids)
        except Exception as e:
            logger.warning("exit_cancel_failed", token_id=token_id[:16], error=str(e))

        # 2. Adjust price by urgency
        tick_float = float(tick_size)
        if urgency == "critical":
            sell_price = price - 2 * tick_float
        elif urgency == "high":
            sell_price = price - tick_float
        else:
            sell_price = price

        sell_price = max(tick_float, sell_price)  # Don't go below min tick

        # Round to valid tick — for normal/medium urgency round UP (better proceeds),
        # for critical/high urgency round DOWN (fill speed priority)
        if urgency in ("critical", "high"):
            sell_price_d = round_bid(sell_price, tick_size)
        else:
            sell_price_d = round_ask(sell_price, tick_size)
        sell_size_d = round_size(size)
        sell_price_str = price_to_string(sell_price_d)
        sell_size_str = str(sell_size_d)

        # 3. Submit sell order
        try:
            expiration = 0
            order_type = OrderType.GTC
            if self._use_gtd:
                expiration = compute_gtd_expiration(
                    self._order_ttl_s, self._get_server_time()
                )
                order_type = OrderType.GTD

            req = CreateOrderRequest(
                token_id=token_id,
                price=sell_price_str,
                size=sell_size_str,
                side=OrderSide.SELL,
                order_type=order_type,
                expiration=expiration,
                neg_risk=neg_risk,
                post_only=False,  # Exit orders may cross — not post-only
                tick_size=str(tick_size),
            )

            submit_result = await self.submit_order(
                req,
                condition_id=condition_id,
                strategy="exit",
            )
            result["submitted"] = submit_result["success"]
            if submit_result["success"]:
                logger.info(
                    "exit_order_submitted",
                    token_id=token_id[:16],
                    price=sell_price_str,
                    size=sell_size_str,
                    urgency=urgency,
                )
            else:
                result["error"] = submit_result["error"]

        except (ClobRestartError, ClobPausedError) as e:
            result["error"] = f"submit_error: {e}"
        except ClobRateLimitError:
            result["error"] = "submit_rate_limited"
        except ClobAuthError as e:
            result["error"] = f"submit_auth_error: {e}"
        except Exception as e:
            result["error"] = f"submit_unexpected: {e}"
            logger.error("exit_order_submit_error", token_id=token_id[:16], error=str(e))

        return result

    async def execute_arb(
        self,
        orders: list[CreateOrderRequest],
    ) -> list[dict[str, Any]]:
        """Execute arb orders (FOK, non-postOnly).

        Arb orders should be all-or-nothing.
        """
        results = []
        try:
            results = await self._submit_orders([
                OrderSubmission(request=req, strategy="arb")
                for req in orders
            ])
        except Exception as e:
            results.append({"order_id": "", "success": False, "error": str(e)})
            logger.error("arb_execution_error", error=str(e))

        return results

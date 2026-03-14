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
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from pmm1.execution.mutation_guard import MutationGuardDecision
    from pmm1.storage.spine import SpineEmitter

logger = structlog.get_logger(__name__)


class OrderCancellation(BaseModel):
    """Cancellation request plus replacement telemetry."""

    order_id: str
    token_id: str = ""
    condition_id: str = ""
    side: str = ""
    reasons: list[str] = Field(default_factory=list)


class OrderSubmission(BaseModel):
    """Submission request plus tracking metadata."""

    request: CreateOrderRequest
    condition_id: str = ""
    strategy: str = ""
    replacement_reasons: list[str] = Field(default_factory=list)
    replaced_order_ids: list[str] = Field(default_factory=list)


class OrderDiff(BaseModel):
    """Diff between desired and live orders."""

    to_cancel: list[OrderCancellation] = Field(default_factory=list)
    to_submit: list[OrderSubmission] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    replacement_reason_counts: dict[str, int] = Field(default_factory=dict)


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
        spine_emitter: SpineEmitter | None = None,
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
        self._spine = spine_emitter
        self._mutation_guard: (
            Callable[[CreateOrderRequest, str, str], MutationGuardDecision]
            | None
        ) = None

    def set_mutation_guard(
        self,
        guard: Callable[[CreateOrderRequest, str, str], MutationGuardDecision],
    ) -> None:
        """Register the guard for direct submit paths that bypass quote-intent shaping."""
        self._mutation_guard = guard

    def set_server_time_offset(self, server_time: int) -> None:
        """Set offset between local and server time."""
        self._server_time_offset = server_time - int(time.time())

    def _get_server_time(self) -> int:
        return int(time.time()) + self._server_time_offset

    def _dedupe_reasons(self, reasons: list[str]) -> list[str]:
        deduped: list[str] = []
        for reason in reasons:
            if reason and reason not in deduped:
                deduped.append(reason)
        return deduped

    def _build_order_request(
        self,
        *,
        token_id: str,
        side: OrderSide,
        price: float,
        size: float,
        tick_size: Decimal,
        neg_risk: bool,
        post_only: bool | None = None,
        order_type: OrderType | None = None,
    ) -> CreateOrderRequest:
        """Create a normalized order request for a desired quote."""
        rounded_price = (
            round_bid(price, tick_size)
            if side == OrderSide.BUY
            else round_ask(price, tick_size)
        )
        rounded_size = round_size(size)
        expiration = 0
        normalized_order_type = order_type or OrderType.GTC
        if self._use_gtd and normalized_order_type in (OrderType.GTC, OrderType.GTD):
            expiration = compute_gtd_expiration(
                self._order_ttl_s,
                self._get_server_time(),
            )
            normalized_order_type = OrderType.GTD

        return CreateOrderRequest(
            token_id=token_id,
            price=price_to_string(rounded_price),
            size=str(rounded_size),
            side=side,
            order_type=normalized_order_type,
            expiration=expiration,
            neg_risk=neg_risk,
            post_only=self._post_only if post_only is None else post_only,
            tick_size=str(tick_size),
        )

    def _replacement_reasons(
        self,
        live: TrackedOrder,
        desired_request: CreateOrderRequest,
        tick_size: Decimal,
        extra_reasons: list[str] | None = None,
    ) -> list[str]:
        """Return explicit reasons a live order should be replaced."""
        reasons = list(extra_reasons or [])
        if live.age_seconds > self._order_ttl_s:
            reasons.append("ttl_expired")

        tick_float = float(tick_size)
        desired_price = float(desired_request.price)
        price_diff = abs(live.price_float - desired_price)
        if price_diff >= tick_float * self._reprice_threshold_ticks:
            reasons.append("price_move")

        desired_size = float(desired_request.size)
        live_remaining = live.remaining_size_float
        if live_remaining > 0:
            size_ratio = abs(live_remaining - desired_size) / live_remaining
            if size_ratio >= self._reprice_threshold_size_pct:
                reasons.append("size_move")

        return self._dedupe_reasons(reasons)

    def _replacement_sort_key(
        self,
        live: TrackedOrder,
        desired_request: CreateOrderRequest,
        reasons: list[str],
    ) -> tuple[float, float, float, float]:
        desired_price = float(desired_request.price)
        desired_size = float(desired_request.size)
        live_size = live.remaining_size_float or live.original_size_float
        size_ratio = (
            abs(live_size - desired_size) / live_size
            if live_size > 0
            else float("inf")
        )
        return (
            float(len(reasons)),
            abs(live.price_float - desired_price),
            size_ratio,
            -live.created_at,
        )

    def _record_cancel(
        self,
        diff: OrderDiff,
        live: TrackedOrder,
        reasons: list[str],
    ) -> None:
        normalized_reasons = self._dedupe_reasons(reasons)
        diff.to_cancel.append(OrderCancellation(
            order_id=live.order_id,
            token_id=live.token_id,
            condition_id=live.condition_id,
            side=live.side,
            reasons=normalized_reasons,
        ))
        for reason in normalized_reasons:
            diff.replacement_reason_counts[reason] = (
                diff.replacement_reason_counts.get(reason, 0) + 1
            )

    def _plan_side(
        self,
        *,
        diff: OrderDiff,
        live_orders: list[TrackedOrder],
        desired_request: CreateOrderRequest | None,
        desired_strategy: str,
        no_desired_reasons: list[str],
        extra_reason_builder: Callable[[TrackedOrder], list[str]] | None = None,
    ) -> None:
        """Plan keep/cancel/submit actions for one token side."""
        if desired_request is None:
            for live in live_orders:
                reasons = list(no_desired_reasons)
                if live.origin in {"startup_sync", "reconcile_import"}:
                    reasons.append("reconcile_mismatch")
                self._record_cancel(diff, live, reasons)
            return

        if not live_orders:
            diff.to_submit.append(OrderSubmission(
                request=desired_request,
                condition_id="",
                strategy=desired_strategy,
            ))
            return

        evaluations: list[tuple[TrackedOrder, list[str]]] = []
        for live in live_orders:
            extra_reasons = extra_reason_builder(live) if extra_reason_builder else []
            reasons = self._replacement_reasons(
                live,
                desired_request,
                Decimal(desired_request.tick_size),
                extra_reasons=extra_reasons,
            )
            evaluations.append((live, reasons))

        keeper, keeper_reasons = min(
            evaluations,
            key=lambda item: self._replacement_sort_key(item[0], desired_request, item[1]),
        )

        if not keeper_reasons:
            diff.unchanged.append(keeper.order_id)
            for live, _ in evaluations:
                if live.order_id == keeper.order_id:
                    continue
                reasons = ["duplicate_live_order"]
                if live.origin in {"startup_sync", "reconcile_import"}:
                    reasons.append("reconcile_mismatch")
                self._record_cancel(diff, live, reasons)
            return

        replaced_order_ids: list[str] = []
        for live, reasons in evaluations:
            effective_reasons = list(reasons)
            if live.order_id != keeper.order_id:
                effective_reasons.append("duplicate_live_order")
                if live.origin in {"startup_sync", "reconcile_import"}:
                    effective_reasons.append("reconcile_mismatch")
            self._record_cancel(diff, live, effective_reasons)
            replaced_order_ids.append(live.order_id)

        diff.to_submit.append(OrderSubmission(
            request=desired_request,
            condition_id="",
            strategy=desired_strategy,
            replacement_reasons=keeper_reasons,
            replaced_order_ids=replaced_order_ids,
        ))

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
        live_bids = self._tracker.get_active_by_side(intent.token_id, "BUY")
        live_asks = self._tracker.get_active_by_side(intent.token_id, "SELL")

        desired_bid = None
        if intent.has_bid and intent.bid_price is not None and intent.bid_size is not None:
            desired_bid = self._build_order_request(
                token_id=intent.token_id,
                side=OrderSide.BUY,
                price=float(intent.bid_price),
                size=float(intent.bid_size),
                tick_size=tick_size,
                neg_risk=intent.neg_risk,
            )

        desired_ask = None
        if intent.has_ask and intent.ask_price is not None and intent.ask_size is not None:
            desired_ask = self._build_order_request(
                token_id=intent.token_id,
                side=OrderSide.SELL,
                price=float(intent.ask_price),
                size=float(intent.ask_size),
                tick_size=tick_size,
                neg_risk=intent.neg_risk,
            )

        self._plan_side(
            diff=diff,
            live_orders=live_bids,
            desired_request=desired_bid,
            desired_strategy=intent.strategy,
            no_desired_reasons=intent.bid_suppression_reasons or ["quote_removed"],
        )
        self._plan_side(
            diff=diff,
            live_orders=live_asks,
            desired_request=desired_ask,
            desired_strategy=intent.strategy,
            no_desired_reasons=intent.ask_suppression_reasons or ["quote_removed"],
        )

        for submission in diff.to_submit:
            if not submission.condition_id:
                submission.condition_id = intent.condition_id

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
                replacement_reasons=submission.replacement_reasons,
                replaced_order_ids=submission.replaced_order_ids,
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
        self._tracker.track_submitted(tracked, source="submit")
        if submission.replacement_reasons:
            logger.info(
                "order_replacement_submitted",
                token_id=req.token_id[:16],
                condition_id=submission.condition_id[:16] if submission.condition_id else "?",
                order_id=order_id[:16],
                side=req.side.value,
                strategy=submission.strategy or "?",
                replacement_reasons=submission.replacement_reasons,
                replaced_order_ids=submission.replaced_order_ids,
                price=req.price,
                size=req.size,
            )
        return {"order_id": order_id, "success": True, "error": ""}

    async def _emit_order_event(
        self,
        *,
        event_type: str,
        strategy: str,
        condition_id: str = "",
        token_id: str = "",
        order_id: str = "",
        payload_json: dict[str, Any] | None = None,
    ) -> None:
        """Emit a canonical order event when the spine is enabled."""
        if self._spine is None:
            return
        await self._spine.emit_event(
            event_type=event_type,
            strategy=strategy or "manual",
            condition_id=condition_id or None,
            token_id=token_id or None,
            order_id=order_id or None,
            payload_json=payload_json or {},
        )

    async def _submit_orders(
        self,
        submissions: list[OrderSubmission],
    ) -> list[dict[str, Any]]:
        """Submit orders and update canonical tracking for successful responses."""
        if not submissions:
            return []

        results: list[dict[str, Any]] = []
        for batch in self._batcher.batch(submissions):
            for submission in batch:
                req = submission.request
                await self._emit_order_event(
                    event_type="order_submit_requested",
                    strategy=submission.strategy or "manual",
                    condition_id=submission.condition_id,
                    token_id=req.token_id,
                    payload_json={
                        "side": req.side.value,
                        "price": req.price,
                        "size": req.size,
                        "post_only": req.post_only,
                        "order_type": req.order_type.value,
                        "expiration": req.expiration,
                        "replacement_reasons": list(submission.replacement_reasons),
                        "replaced_order_ids": list(submission.replaced_order_ids),
                    },
                )
            requests = [submission.request for submission in batch]
            responses = await self._client.create_orders_batch(requests)
            if len(responses) != len(batch):
                logger.error(
                    "order_submission_batch_mismatch",
                    requested=len(batch),
                    received=len(responses),
                )

            for submission, response in zip(batch, responses):
                result = self._process_submission_response(submission, response)
                results.append(result)
                req = submission.request
                if result["success"]:
                    await self._emit_order_event(
                        event_type="order_submit_acknowledged",
                        strategy=submission.strategy or "manual",
                        condition_id=submission.condition_id,
                        token_id=req.token_id,
                        order_id=result["order_id"],
                        payload_json={
                            "side": req.side.value,
                            "price": req.price,
                            "size": req.size,
                            "post_only": req.post_only,
                            "order_type": req.order_type.value,
                            "expiration": req.expiration,
                            "replacement_reasons": list(submission.replacement_reasons),
                            "replaced_order_ids": list(submission.replaced_order_ids),
                        },
                    )
                else:
                    await self._emit_order_event(
                        event_type="order_rejected",
                        strategy=submission.strategy or "manual",
                        condition_id=submission.condition_id,
                        token_id=req.token_id,
                        payload_json={
                            "side": req.side.value,
                            "price": req.price,
                            "size": req.size,
                            "error": result["error"],
                            "replacement_reasons": list(submission.replacement_reasons),
                            "replaced_order_ids": list(submission.replaced_order_ids),
                        },
                    )

        return results

    async def submit_order(
        self,
        request: CreateOrderRequest,
        *,
        condition_id: str = "",
        strategy: str = "manual",
    ) -> dict[str, Any]:
        """Submit one order and track it if the exchange accepted it."""
        if self._mutation_guard is not None:
            decision = self._mutation_guard(request, condition_id, strategy)
            if not decision.allowed:
                logger.warning(
                    "order_submission_blocked",
                    condition_id=condition_id[:16] if condition_id else "?",
                    token_id=request.token_id[:16],
                    strategy=strategy or "manual",
                    reason=decision.reason,
                    details=decision.details,
                )
                await self._emit_order_event(
                    event_type="order_rejected",
                    strategy=strategy or "manual",
                    condition_id=condition_id,
                    token_id=request.token_id,
                    payload_json={
                        "side": request.side.value,
                        "price": request.price,
                        "size": request.size,
                        "error": f"blocked_by_guard:{decision.reason}",
                        "guard_details": decision.details,
                    },
                )
                return {
                    "order_id": "",
                    "success": False,
                    "error": f"blocked_by_guard:{decision.reason}",
                }
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
            "replacement_reason_counts": dict(diff.replacement_reason_counts),
            "errors": [],
        }

        # 1. Cancel stale orders
        if diff.to_cancel:
            try:
                cancel_map: dict[str, OrderCancellation] = {}
                for cancel in diff.to_cancel:
                    existing = cancel_map.get(cancel.order_id)
                    if existing is None:
                        cancel_map[cancel.order_id] = cancel
                        continue
                    existing.reasons = self._dedupe_reasons(existing.reasons + cancel.reasons)

                for cancel in cancel_map.values():
                    tracked = self._tracker.get(cancel.order_id)
                    await self._emit_order_event(
                        event_type="order_cancel_requested",
                        strategy=(tracked.strategy if tracked else intent.strategy) or "manual",
                        condition_id=cancel.condition_id or (
                            tracked.condition_id if tracked else ""
                        ),
                        token_id=cancel.token_id or (
                            tracked.token_id if tracked else ""
                        ),
                        order_id=cancel.order_id,
                        payload_json={
                            "side": cancel.side or (
                                tracked.side if tracked else ""
                            ),
                            "reasons": list(cancel.reasons),
                            "source": "diff_cancel",
                        },
                    )

                to_cancel = list(cancel_map)
                await self._client.cancel_orders(to_cancel)
                for cancel in cancel_map.values():
                    self._tracker.update_state(
                        cancel.order_id,
                        OrderState.CANCELED,
                        source="diff_cancel",
                    )
                    logger.info(
                        "order_replacement_canceled",
                        token_id=cancel.token_id[:16] if cancel.token_id else "?",
                        condition_id=cancel.condition_id[:16] if cancel.condition_id else "?",
                        order_id=cancel.order_id[:16],
                        side=cancel.side,
                        reasons=cancel.reasons,
                    )
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
                submit_results = await self._submit_orders(diff.to_submit)
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
                await self._emit_order_event(
                    event_type="order_cancel_requested",
                    strategy=order.strategy or "manual",
                    condition_id=order.condition_id,
                    token_id=order.token_id,
                    order_id=order.order_id,
                    payload_json={"side": order.side, "source": "cancel_all"},
                )
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
                for order in active:
                    await self._emit_order_event(
                        event_type="order_cancel_requested",
                        strategy=order.strategy or "manual",
                        condition_id=order.condition_id,
                        token_id=order.token_id,
                        order_id=order.order_id,
                        payload_json={"side": order.side, "source": "cancel_market"},
                    )
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
        """Submit or maintain an exit (sell) order for a token.

        Used by ExitManager for stop-loss, take-profit, resolution, flatten, orphan exits.

        Pricing by urgency:
        - critical: best_bid - 2 ticks (must fill)
        - high: best_bid (fill now)
        - medium: best_bid (join queue)
        - low: best_bid (passive)
        """
        result: dict[str, Any] = {
            "submitted": False,
            "kept": False,
            "unchanged": 0,
            "canceled": 0,
            "replacement_reason_counts": {},
            "error": None,
        }

        tick_float = float(tick_size)
        if urgency == "critical":
            sell_price = price - 2 * tick_float
        elif urgency == "high":
            sell_price = price - tick_float
        else:
            sell_price = price

        sell_price = max(tick_float, sell_price)  # Don't go below min tick

        if urgency in ("critical", "high"):
            sell_price_d = round_bid(sell_price, tick_size)
        else:
            sell_price_d = round_ask(sell_price, tick_size)
        desired_exit = self._build_order_request(
            token_id=token_id,
            side=OrderSide.SELL,
            price=float(sell_price_d),
            size=size,
            tick_size=tick_size,
            neg_risk=neg_risk,
            post_only=False,
        )

        diff = OrderDiff()
        active = self._tracker.get_active_orders(token_id)
        live_buys = [order for order in active if order.side == "BUY"]
        live_sells = [order for order in active if order.side == "SELL"]

        for live_buy in live_buys:
            self._record_cancel(diff, live_buy, ["exit_replace"])

        def _exit_extra_reasons(live: TrackedOrder) -> list[str]:
            reasons: list[str] = []
            if live.strategy != "exit":
                reasons.append("exit_replace")
            if live.post_only:
                reasons.append("exit_replace")
            return reasons

        self._plan_side(
            diff=diff,
            live_orders=live_sells,
            desired_request=desired_exit,
            desired_strategy="exit",
            no_desired_reasons=["exit_replace"],
            extra_reason_builder=_exit_extra_reasons,
        )
        for submission in diff.to_submit:
            submission.condition_id = condition_id

        result["replacement_reason_counts"] = dict(diff.replacement_reason_counts)
        result["unchanged"] = len(diff.unchanged)
        result["kept"] = bool(diff.unchanged) and not diff.to_submit

        if diff.to_cancel:
            try:
                cancel_map: dict[str, OrderCancellation] = {}
                for cancel in diff.to_cancel:
                    existing = cancel_map.get(cancel.order_id)
                    if existing is None:
                        cancel_map[cancel.order_id] = cancel
                        continue
                    existing.reasons = self._dedupe_reasons(existing.reasons + cancel.reasons)

                for cancel in cancel_map.values():
                    tracked = self._tracker.get(cancel.order_id)
                    await self._emit_order_event(
                        event_type="order_cancel_requested",
                        strategy=(tracked.strategy if tracked else "exit") or "exit",
                        condition_id=cancel.condition_id or (
                            tracked.condition_id if tracked else condition_id
                        ),
                        token_id=cancel.token_id or (
                            tracked.token_id if tracked else token_id
                        ),
                        order_id=cancel.order_id,
                        payload_json={
                            "side": cancel.side or (tracked.side if tracked else "SELL"),
                            "reasons": list(cancel.reasons),
                            "source": "exit_cancel",
                        },
                    )

                order_ids = list(cancel_map)
                await self._client.cancel_orders(order_ids)
                for cancel in cancel_map.values():
                    self._tracker.update_state(
                        cancel.order_id,
                        OrderState.CANCELED,
                        source="exit_cancel",
                    )
                    logger.info(
                        "exit_order_canceled_for_replace",
                        token_id=cancel.token_id[:16] if cancel.token_id else "?",
                        condition_id=cancel.condition_id[:16] if cancel.condition_id else "?",
                        order_id=cancel.order_id[:16],
                        side=cancel.side,
                        reasons=cancel.reasons,
                    )
                result["canceled"] = len(order_ids)
            except (ClobRestartError, ClobPausedError) as e:
                result["error"] = f"cancel_error: {e}"
                return result
            except ClobRateLimitError:
                result["error"] = "cancel_rate_limited"
                return result
            except ClobAuthError as e:
                result["error"] = f"cancel_auth_error: {e}"
                return result
            except Exception as e:
                result["error"] = f"cancel_unexpected: {e}"
                logger.warning("exit_cancel_failed", token_id=token_id[:16], error=str(e))
                return result

        if diff.to_submit:
            try:
                submit_results = await self._submit_orders(diff.to_submit)
                result["submitted"] = any(item["success"] for item in submit_results)
                if not result["submitted"]:
                    first_error = next(
                        (item["error"] for item in submit_results if item["error"]),
                        "no_response",
                    )
                    result["error"] = first_error
            except (ClobRestartError, ClobPausedError) as e:
                result["error"] = f"submit_error: {e}"
            except ClobRateLimitError:
                result["error"] = "submit_rate_limited"
            except ClobAuthError as e:
                result["error"] = f"submit_auth_error: {e}"
            except Exception as e:
                result["error"] = f"submit_unexpected: {e}"
                logger.error("exit_order_submit_error", token_id=token_id[:16], error=str(e))
        elif result["kept"]:
            logger.info(
                "exit_order_kept",
                token_id=token_id[:16],
                condition_id=condition_id[:16] if condition_id else "?",
                order_id=diff.unchanged[0][:16],
                urgency=urgency,
                price=desired_exit.price,
                size=desired_exit.size,
            )

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

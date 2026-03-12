"""Order state tracking with state machine from §11.

Order State Machine:
    INTENT → SIGNED → SUBMITTED → LIVE|MATCHED|DELAYED → PARTIAL → FILLED|CANCELED|EXPIRED|FAILED
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class OrderState(str, Enum):
    """Order lifecycle states from §11."""

    INTENT = "INTENT"
    SIGNED = "SIGNED"
    SUBMITTED = "SUBMITTED"
    LIVE = "LIVE"
    MATCHED = "MATCHED"
    DELAYED = "DELAYED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


# Valid state transitions
_VALID_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.INTENT: {OrderState.SIGNED, OrderState.FAILED},
    OrderState.SIGNED: {OrderState.SUBMITTED, OrderState.FAILED},
    OrderState.SUBMITTED: {
        OrderState.LIVE,
        OrderState.MATCHED,
        OrderState.DELAYED,
        OrderState.FAILED,
        OrderState.CANCELED,
    },
    OrderState.LIVE: {
        OrderState.PARTIAL,
        OrderState.MATCHED,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.EXPIRED,
    },
    OrderState.MATCHED: {
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.FAILED,
        OrderState.RETRYING,
    },
    OrderState.DELAYED: {
        OrderState.LIVE,
        OrderState.MATCHED,
        OrderState.FAILED,
        OrderState.CANCELED,
    },
    OrderState.PARTIAL: {
        OrderState.MATCHED,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.EXPIRED,
    },
    OrderState.RETRYING: {
        OrderState.MATCHED,
        OrderState.FILLED,
        OrderState.FAILED,
        OrderState.CANCELED,
    },
    # Terminal states
    OrderState.FILLED: set(),
    OrderState.CANCELED: set(),
    OrderState.EXPIRED: set(),
    OrderState.FAILED: set(),
}

TERMINAL_STATES = {OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED, OrderState.FAILED}
ACTIVE_STATES = {OrderState.SUBMITTED, OrderState.LIVE, OrderState.PARTIAL, OrderState.MATCHED, OrderState.DELAYED}

LIFECYCLE_COUNTER_NAMES = (
    "submitted",
    "live",
    "matched",
    "delayed",
    "partial_fill",
    "filled",
    "canceled",
    "expired",
    "failed",
)

_STATE_TO_LIFECYCLE_COUNTER: dict[OrderState, str] = {
    OrderState.SUBMITTED: "submitted",
    OrderState.LIVE: "live",
    OrderState.MATCHED: "matched",
    OrderState.DELAYED: "delayed",
    OrderState.PARTIAL: "partial_fill",
    OrderState.FILLED: "filled",
    OrderState.CANCELED: "canceled",
    OrderState.EXPIRED: "expired",
    OrderState.FAILED: "failed",
}


def zero_lifecycle_counts() -> dict[str, int]:
    """Return an empty lifecycle counter snapshot."""
    return {name: 0 for name in LIFECYCLE_COUNTER_NAMES}


class TrackedOrder(BaseModel):
    """An order tracked through its lifecycle."""

    order_id: str = ""
    client_order_id: str = ""
    token_id: str = ""
    condition_id: str = ""
    side: str = ""  # BUY or SELL
    price: str = ""
    original_size: str = ""
    filled_size: str = "0"
    remaining_size: str = ""
    state: OrderState = OrderState.INTENT
    neg_risk: bool = False
    post_only: bool = True
    order_type: str = "GTC"  # GTC, GTD, FOK, FAK
    expiration: int = 0
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    submitted_at: float | None = None
    filled_at: float | None = None
    canceled_at: float | None = None
    error_msg: str = ""
    # Execution metadata
    transaction_hashes: list[str] = Field(default_factory=list)
    fills: list[dict[str, Any]] = Field(default_factory=list)
    # Strategy context
    strategy: str = ""  # mm, parity_arb, neg_risk_arb
    intent_tag: str = ""  # for matching intent → result
    # Rewards tracking
    is_scoring: bool = False  # Whether order is scoring for Polymarket rewards
    origin: str = ""  # submit | startup_sync | reconcile_import | restored

    @property
    def price_float(self) -> float:
        return float(self.price) if self.price else 0.0

    @property
    def original_size_float(self) -> float:
        return float(self.original_size) if self.original_size else 0.0

    @property
    def filled_size_float(self) -> float:
        return float(self.filled_size) if self.filled_size else 0.0

    @property
    def remaining_size_float(self) -> float:
        if self.remaining_size:
            return float(self.remaining_size)
        return self.original_size_float - self.filled_size_float

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def is_buy(self) -> bool:
        return self.side == "BUY"

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    def transition_to(self, new_state: OrderState) -> bool:
        """Attempt state transition. Returns True if valid and applied.

        Logs warnings for invalid transitions but does NOT raise —
        we want to be resilient to out-of-order WS messages.
        Hard-rejects transitions from terminal states to prevent ghost orders.
        """
        if new_state == self.state:
            return True  # No-op, already in state

        # Hard-reject transitions from terminal states
        if self.state in TERMINAL_STATES:
            logger.warning(
                "rejected_terminal_transition",
                order_id=self.order_id,
                from_state=self.state.value,
                to_state=new_state.value,
            )
            return False

        valid_next = _VALID_TRANSITIONS.get(self.state, set())
        if new_state not in valid_next:
            logger.warning(
                "invalid_order_transition",
                order_id=self.order_id,
                from_state=self.state.value,
                to_state=new_state.value,
            )
            # Allow it anyway for resilience (WS messages may arrive out of order)
            # but log the violation

        old_state = self.state
        self.state = new_state
        self.updated_at = time.time()

        if new_state == OrderState.SUBMITTED:
            self.submitted_at = time.time()
        elif new_state == OrderState.FILLED:
            self.filled_at = time.time()
        elif new_state == OrderState.CANCELED:
            self.canceled_at = time.time()

        logger.debug(
            "order_state_transition",
            order_id=self.order_id[:16] if self.order_id else "?",
            from_state=old_state.value,
            to_state=new_state.value,
        )
        return True

    def apply_fill(self, fill_size: str, fill_price: str | None = None) -> None:
        """Apply a fill to this order."""
        fill_qty = float(fill_size)
        self.filled_size = str(float(self.filled_size) + fill_qty)
        remaining = self.original_size_float - self.filled_size_float
        self.remaining_size = str(max(0.0, remaining))

        self.fills.append({
            "size": fill_size,
            "price": fill_price or self.price,
            "timestamp": time.time(),
        })

        if remaining <= 0:
            self.transition_to(OrderState.FILLED)
        elif self.state not in TERMINAL_STATES:
            self.transition_to(OrderState.PARTIAL)


class OrderTracker:
    """Tracks all orders across their lifecycle."""

    def __init__(self) -> None:
        self._orders: dict[str, TrackedOrder] = {}  # order_id → TrackedOrder
        self._by_token: dict[str, set[str]] = {}  # token_id → set of order_ids
        self._by_strategy: dict[str, set[str]] = {}  # strategy → set of order_ids
        self._lifecycle_counts: dict[str, int] = zero_lifecycle_counts()

    def track(self, order: TrackedOrder) -> None:
        """Start tracking a new order."""
        self._orders[order.order_id] = order

        if order.token_id:
            self._by_token.setdefault(order.token_id, set()).add(order.order_id)
        if order.strategy:
            self._by_strategy.setdefault(order.strategy, set()).add(order.order_id)

    def _record_lifecycle_state(self, order: TrackedOrder, state: OrderState) -> None:
        """Increment canonical lifecycle counters for a state transition."""
        counter_name = _STATE_TO_LIFECYCLE_COUNTER.get(state)
        if counter_name:
            self._lifecycle_counts[counter_name] += 1

    def track_submitted(self, order: TrackedOrder, source: str = "") -> bool:
        """Track an order that was successfully accepted by the exchange."""
        if not order.order_id:
            logger.warning(
                "track_submitted_missing_order_id",
                token_id=order.token_id[:16] if order.token_id else "?",
                strategy=order.strategy or source or "?",
            )
            return False

        now = time.time()
        order.state = OrderState.SUBMITTED
        order.updated_at = now
        if order.submitted_at is None:
            order.submitted_at = now
        if source:
            order.origin = source

        self.track(order)
        self._record_lifecycle_state(order, OrderState.SUBMITTED)
        return True

    def _parse_exchange_timestamp(self, raw_value: Any) -> float:
        """Parse exchange timestamps from numeric or ISO-8601 formats."""
        if raw_value in (None, ""):
            return time.time()

        if isinstance(raw_value, (int, float)):
            ts = float(raw_value)
            return ts / 1000.0 if ts > 1_000_000_000_000 else ts

        value = str(raw_value).strip()
        if not value:
            return time.time()

        try:
            ts = float(value)
            return ts / 1000.0 if ts > 1_000_000_000_000 else ts
        except ValueError:
            pass

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return time.time()

    def _exchange_status_to_state(self, status: str) -> OrderState:
        return {
            "LIVE": OrderState.LIVE,
            "MATCHED": OrderState.MATCHED,
            "DELAYED": OrderState.DELAYED,
            "MINED": OrderState.MATCHED,
            "CONFIRMED": OrderState.FILLED,
            "FILLED": OrderState.FILLED,
            "CANCELED": OrderState.CANCELED,
            "CANCELLED": OrderState.CANCELED,
            "EXPIRED": OrderState.EXPIRED,
            "FAILED": OrderState.FAILED,
            "RETRYING": OrderState.RETRYING,
        }.get((status or "").upper(), OrderState.LIVE)

    def sync_exchange_order(
        self,
        exchange_order: Any,
        *,
        source: str = "exchange_sync",
        strategy: str = "restored",
    ) -> tuple[TrackedOrder | None, bool]:
        """Import or refresh a live order from exchange truth without touching counters."""
        if hasattr(exchange_order, "model_dump"):
            data = exchange_order.model_dump(by_alias=True)
        elif isinstance(exchange_order, dict):
            data = dict(exchange_order)
        else:
            logger.warning("sync_exchange_order_invalid_payload", payload_type=type(exchange_order).__name__)
            return None, False

        order_id = str(data.get("orderID") or data.get("order_id") or data.get("id") or "").strip()
        if not order_id:
            logger.warning("sync_exchange_order_missing_id", source=source)
            return None, False

        state = self._exchange_status_to_state(str(data.get("status", "")))
        original_size = str(data.get("originalSize") or data.get("original_size") or data.get("size") or "0")
        filled_size = str(data.get("sizeMatched") or data.get("size_matched") or "0")
        try:
            remaining_size = str(max(0.0, float(original_size) - float(filled_size)))
        except (TypeError, ValueError):
            remaining_size = original_size

        created_at = self._parse_exchange_timestamp(
            data.get("createdAt") or data.get("created_at")
        )
        token_id = str(data.get("asset_id") or data.get("assetId") or data.get("token_id") or "")
        condition_id = str(
            data.get("condition_id")
            or data.get("conditionId")
            or data.get("market")
            or ""
        )
        side = str(data.get("side") or "").upper()
        price = str(data.get("price") or "")
        expiration_raw = data.get("expiration") or 0
        try:
            expiration = int(float(expiration_raw))
        except (TypeError, ValueError):
            expiration = 0

        existing = self._orders.get(order_id)
        if existing is not None:
            existing.token_id = token_id or existing.token_id
            existing.condition_id = condition_id or existing.condition_id
            existing.side = side or existing.side
            existing.price = price or existing.price
            existing.original_size = original_size or existing.original_size
            existing.filled_size = filled_size
            existing.remaining_size = remaining_size
            existing.expiration = expiration or existing.expiration
            existing.origin = existing.origin or source
            existing.updated_at = time.time()
            if existing.created_at <= 0:
                existing.created_at = created_at
            if existing.state not in TERMINAL_STATES and state != existing.state:
                existing.transition_to(state)
            return existing, False

        tracked = TrackedOrder(
            order_id=order_id,
            token_id=token_id,
            condition_id=condition_id,
            side=side,
            price=price,
            original_size=original_size,
            filled_size=filled_size,
            remaining_size=remaining_size,
            state=state,
            expiration=expiration,
            created_at=created_at,
            updated_at=time.time(),
            submitted_at=created_at if state in ACTIVE_STATES else None,
            strategy=strategy,
            origin=source,
        )
        self.track(tracked)
        return tracked, True

    def import_exchange_orders(
        self,
        exchange_orders: list[Any],
        *,
        source: str = "exchange_sync",
        strategy: str = "restored",
    ) -> list[str]:
        """Import all open orders from exchange truth and return newly tracked ids."""
        imported_ids: list[str] = []
        for exchange_order in exchange_orders:
            tracked, created = self.sync_exchange_order(
                exchange_order,
                source=source,
                strategy=strategy,
            )
            if tracked is not None and created:
                imported_ids.append(tracked.order_id)
        return imported_ids

    def get(self, order_id: str) -> TrackedOrder | None:
        """Get order by ID."""
        return self._orders.get(order_id)

    def update_state(self, order_id: str, new_state: OrderState, source: str = "") -> bool:
        """Update order state."""
        order = self._orders.get(order_id)
        if order is None:
            logger.warning("update_unknown_order", order_id=order_id)
            return False
        old_state = order.state
        updated = order.transition_to(new_state)
        if updated and order.state != old_state:
            self._record_lifecycle_state(order, order.state)
        return updated

    def apply_fill(
        self,
        order_id: str,
        fill_size: str,
        fill_price: str | None = None,
        source: str = "",
    ) -> bool:
        """Apply a fill to a tracked order."""
        order = self._orders.get(order_id)
        if order is None:
            logger.warning("fill_unknown_order", order_id=order_id)
            return False
        old_state = order.state
        order.apply_fill(fill_size, fill_price)
        if order.state != old_state:
            self._record_lifecycle_state(order, order.state)
        return True

    def get_active_orders(self, token_id: str | None = None) -> list[TrackedOrder]:
        """Get all active (non-terminal) orders, optionally for a specific token."""
        if token_id:
            order_ids = self._by_token.get(token_id, set())
            return [
                self._orders[oid]
                for oid in order_ids
                if oid in self._orders and self._orders[oid].is_active
            ]
        return [o for o in self._orders.values() if o.is_active]

    def get_active_by_side(self, token_id: str, side: str) -> list[TrackedOrder]:
        """Get active orders for a token on a specific side."""
        return [
            o for o in self.get_active_orders(token_id)
            if o.side == side
        ]

    def get_orders_by_strategy(self, strategy: str) -> list[TrackedOrder]:
        """Get all orders for a strategy."""
        order_ids = self._by_strategy.get(strategy, set())
        return [self._orders[oid] for oid in order_ids if oid in self._orders]

    def count_active(self, token_id: str | None = None, side: str | None = None) -> int:
        """Count active orders, optionally filtered."""
        orders = self.get_active_orders(token_id)
        if side:
            orders = [o for o in orders if o.side == side]
        return len(orders)

    def cleanup_terminal(self, max_age_s: float = 3600) -> int:
        """Remove terminal orders older than max_age_s."""
        now = time.time()
        to_remove = []
        for oid, order in self._orders.items():
            if order.is_terminal and (now - order.updated_at) > max_age_s:
                to_remove.append(oid)

        for oid in to_remove:
            order = self._orders.pop(oid)
            if order.token_id in self._by_token:
                self._by_token[order.token_id].discard(oid)
            if order.strategy in self._by_strategy:
                self._by_strategy[order.strategy].discard(oid)

        if to_remove:
            logger.info("orders_cleaned_up", count=len(to_remove))
        return len(to_remove)

    def reconcile_with_exchange(self, exchange_orders: list[dict[str, Any]]) -> dict[str, Any]:
        """Reconcile local state with exchange open orders.

        Returns dict of mismatches found.
        """
        exchange_by_id = {
            str(o.get("orderID") or o.get("order_id") or o.get("id") or ""): o
            for o in exchange_orders
            if str(o.get("orderID") or o.get("order_id") or o.get("id") or "")
        }
        exchange_ids = set(exchange_by_id)
        local_active_ids = {o.order_id for o in self.get_active_orders()}

        # Orders on exchange but not tracked locally
        unknown_on_exchange = exchange_ids - local_active_ids
        # Orders we think are active but not on exchange
        missing_from_exchange = local_active_ids - exchange_ids

        imported_from_exchange: list[str] = []
        for oid in sorted(unknown_on_exchange):
            tracked, created = self.sync_exchange_order(
                exchange_by_id[oid],
                source="reconcile_import",
            )
            if tracked is not None and created:
                imported_from_exchange.append(oid)

        # Mark missing orders as canceled
        for oid in missing_from_exchange:
            self.update_state(oid, OrderState.CANCELED, source="reconcile")

        matched_ids = exchange_ids & {o.order_id for o in self.get_active_orders()}

        result = {
            "unknown_on_exchange": list(unknown_on_exchange),
            "imported_from_exchange": imported_from_exchange,
            "missing_from_exchange": list(missing_from_exchange),
            "matched": len(matched_ids),
        }

        if unknown_on_exchange or missing_from_exchange:
            logger.warning("order_reconciliation_mismatch", **result)
        else:
            logger.debug("order_reconciliation_clean", matched=result["matched"])

        return result

    @property
    def total_active(self) -> int:
        return sum(1 for o in self._orders.values() if o.is_active)

    @property
    def total_tracked(self) -> int:
        return len(self._orders)

    def snapshot_lifecycle_counts(self) -> dict[str, int]:
        """Return a copy of cumulative lifecycle counters."""
        return dict(self._lifecycle_counts)

    def diff_lifecycle_counts(self, baseline: dict[str, int] | None = None) -> dict[str, int]:
        """Return lifecycle counter deltas from a prior snapshot."""
        current = self.snapshot_lifecycle_counts()
        reference = zero_lifecycle_counts()
        if baseline:
            for key, value in baseline.items():
                if key in reference:
                    reference[key] = value

        return {
            key: current.get(key, 0) - reference.get(key, 0)
            for key in LIFECYCLE_COUNTER_NAMES
        }

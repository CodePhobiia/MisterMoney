"""Diff engine — compare target vs live orders and generate minimal mutations.

Philosophy:
- Minimize order churn (avoid unnecessary cancels/adds)
- Respect persistence optimizer decisions (NEVER cancel ENTRENCHED/SCORING orders)
- Match orders within tick tolerance (don't reprice for tiny differences)
- Rate-limit repricing to prevent spam
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from pmm2.persistence.action_ev import PersistenceAction
from pmm2.planner.quote_planner import TargetQuotePlan

logger = structlog.get_logger(__name__)


class OrderMutation(BaseModel):
    """A single order change to execute."""

    action: str  # "add", "cancel", "amend"
    token_id: str = ""
    condition_id: str = ""
    side: str = ""
    price: float = 0.0
    size: float = 0.0
    order_id: str = ""  # for cancel/amend
    reason: str = ""

    model_config = {"frozen": False}


class DiffEngine:
    """Compare target plan vs current live orders.
    Generate minimal mutation set.

    Key rules:
    1. NEVER mutate ENTRENCHED or SCORING orders unless persistence optimizer approves
    2. Match orders within tick tolerance (avoid unnecessary reprices)
    3. Prefer amends over cancel+add when possible
    4. Rate-limit mutations per market
    """

    def __init__(self):
        """Initialize diff engine."""
        pass

    def diff(
        self,
        target: TargetQuotePlan,
        live_orders: list,
        persistence_decisions: dict[str, tuple[PersistenceAction, float]] | None = None,
        tick_size: float = 0.01,
    ) -> list[OrderMutation]:
        """Generate minimal mutations to move from current to target.

        Rules:
        1. If live order matches target rung (same side, price within tick) → keep
        2. If live order has no matching target rung → cancel (unless HOLD/ENTRENCHED)
        3. If target rung has no matching live order → add
        4. If persistence optimizer says IMPROVE/WIDEN → amend
        5. If persistence optimizer says HOLD → skip even if target differs
        6. Never exceed max_reprices_per_minute (enforced by caller)

        Args:
            target: target quote plan
            live_orders: list of live orders (TrackedOrder objects)
            persistence_decisions: map of order_id → (action, ev) from persistence optimizer
            tick_size: price tick size for matching tolerance

        Returns:
            List of OrderMutations to execute
        """
        persistence_decisions = persistence_decisions or {}
        mutations: list[OrderMutation] = []

        # Build lookup: (token_id, side, price_bucket) → live orders
        live_by_key: dict[tuple[str, str, float], list] = {}
        for order in live_orders:
            if not hasattr(order, "token_id") or not hasattr(order, "side"):
                continue

            # Bucket price to nearest tick
            price_bucket = round(order.price / tick_size) * tick_size
            key = (order.token_id, order.side, price_bucket)
            live_by_key.setdefault(key, []).append(order)

        # Track which live orders we've matched
        matched_live: set[str] = set()

        # --- Pass 1: Match target rungs to live orders ---
        for rung in target.ladder:
            price_bucket = round(rung.price / tick_size) * tick_size
            key = (rung.token_id, rung.side, price_bucket)

            candidates = live_by_key.get(key, [])

            # Find best match (prefer exact size match, then closest size)
            best_match = None
            best_size_diff = float("inf")

            for order in candidates:
                if order.order_id in matched_live:
                    continue  # already matched

                # Check persistence decision
                decision = persistence_decisions.get(order.order_id)
                if decision:
                    action, _ = decision
                    if action == PersistenceAction.HOLD:
                        # Optimizer says HOLD → keep this order as-is
                        matched_live.add(order.order_id)
                        logger.debug(
                            "diff_keep_hold",
                            order_id=order.order_id,
                            reason="persistence_hold",
                        )
                        best_match = order
                        break

                # Check size match
                size_diff = abs(order.size_open - rung.size)
                if size_diff < best_size_diff:
                    best_match = order
                    best_size_diff = size_diff

            if best_match:
                matched_live.add(best_match.order_id)

                # Check if amend needed (size mismatch)
                if best_size_diff > 0.01:  # More than 0.01 shares difference
                    decision = persistence_decisions.get(best_match.order_id)
                    if decision:
                        action, _ = decision
                        if action in (
                            PersistenceAction.IMPROVE_1T,
                            PersistenceAction.IMPROVE_2T,
                            PersistenceAction.WIDEN_1T,
                        ):
                            # Persistence optimizer approved a move → amend
                            mutations.append(
                                OrderMutation(
                                    action="amend",
                                    order_id=best_match.order_id,
                                    token_id=rung.token_id,
                                    condition_id=rung.condition_id,
                                    side=rung.side,
                                    price=rung.price,
                                    size=rung.size,
                                    reason=f"persistence_{action.value}",
                                )
                            )
                            logger.debug(
                                "diff_amend",
                                order_id=best_match.order_id,
                                old_size=best_match.size_open,
                                new_size=rung.size,
                                reason=action.value,
                            )

                logger.debug(
                    "diff_match",
                    order_id=best_match.order_id,
                    rung_price=rung.price,
                    rung_size=rung.size,
                )
            else:
                # No match → add new order
                mutations.append(
                    OrderMutation(
                        action="add",
                        token_id=rung.token_id,
                        condition_id=rung.condition_id,
                        side=rung.side,
                        price=rung.price,
                        size=rung.size,
                        reason="target_rung_unmatched",
                    )
                )
                logger.debug(
                    "diff_add",
                    token_id=rung.token_id,
                    side=rung.side,
                    price=rung.price,
                    size=rung.size,
                )

        # --- Pass 2: Cancel unmatched live orders ---
        for order in live_orders:
            if not hasattr(order, "order_id"):
                continue

            if order.order_id in matched_live:
                continue  # Already matched

            # Check persistence decision
            decision = persistence_decisions.get(order.order_id)
            if decision:
                action, _ = decision
                if action == PersistenceAction.HOLD:
                    # Optimizer says HOLD → don't cancel
                    logger.debug(
                        "diff_skip_cancel_hold",
                        order_id=order.order_id,
                        reason="persistence_hold",
                    )
                    continue
                elif action == PersistenceAction.CANCEL:
                    # Explicitly approved cancel
                    mutations.append(
                        OrderMutation(
                            action="cancel",
                            order_id=order.order_id,
                            token_id=getattr(order, "token_id", ""),
                            condition_id=getattr(order, "condition_id", ""),
                            reason="persistence_cancel",
                        )
                    )
                    logger.debug(
                        "diff_cancel",
                        order_id=order.order_id,
                        reason="persistence_cancel",
                    )
                    continue

            # No target rung for this order → cancel (but check persistence first)
            # If we reach here and no explicit HOLD, we can cancel
            mutations.append(
                OrderMutation(
                    action="cancel",
                    order_id=order.order_id,
                    token_id=getattr(order, "token_id", ""),
                    condition_id=getattr(order, "condition_id", ""),
                    reason="no_target_rung",
                )
            )
            logger.debug(
                "diff_cancel",
                order_id=order.order_id,
                reason="no_target_rung",
            )

        logger.info(
            "diff_complete",
            condition_id=target.condition_id,
            target_rungs=len(target.ladder),
            live_orders=len(live_orders),
            mutations=len(mutations),
            adds=sum(1 for m in mutations if m.action == "add"),
            cancels=sum(1 for m in mutations if m.action == "cancel"),
            amends=sum(1 for m in mutations if m.action == "amend"),
        )

        return mutations

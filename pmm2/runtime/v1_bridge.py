"""V1 execution bridge — PMM-2 decisions → V1 order manager.

PMM-2 outputs OrderMutations → V1's order manager executes them.
V1's heartbeat, risk engine, reconciliation remain authoritative.
PMM-2 NEVER bypasses V1 safety layers.

Shadow mode (default): log all decisions but execute nothing.
Live mode: pass through V1's order manager with full safety checks.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from pmm2.planner.diff_engine import OrderMutation

logger = structlog.get_logger(__name__)


class V1Bridge:
    """Bridge between PMM-2 decisions and V1 execution.

    In shadow mode: log mutations but don't execute.
    In live mode: pass mutations to V1's order manager.

    Key safety principles:
    - NEVER bypass V1's risk limits
    - NEVER bypass V1's heartbeat/reconciliation
    - ALWAYS defer to V1's order manager for execution
    - Shadow mode is the DEFAULT (fail-safe)
    """

    def __init__(
        self,
        order_manager: Any = None,
        risk_limits: Any = None,
        shadow_mode: bool = True,
        controller_label: str = "pmm2_shadow",
        stage_name: str = "shadow",
        live_capital_pct: float = 0.0,
        strategy_label: str = "pmm2_shadow",
    ) -> None:
        """Initialize V1 bridge.

        Args:
            order_manager: V1's order manager (from bot_state)
            risk_limits: V1's risk limits (from bot_state)
            shadow_mode: if True, log only (don't execute)
        """
        self.order_manager = order_manager
        self.risk_limits = risk_limits
        self.shadow_mode = shadow_mode
        self.controller_label = controller_label
        self.stage_name = stage_name
        self.live_capital_pct = live_capital_pct
        self.strategy_label = strategy_label
        self.mutation_log: list[dict[str, Any]] = []

        logger.info(
            "v1_bridge_initialized",
            shadow_mode=shadow_mode,
            controller=controller_label,
            stage=stage_name,
            strategy=strategy_label,
            has_order_manager=order_manager is not None,
        )

    async def execute_mutations(
        self,
        mutations: list[OrderMutation],
        tick_sizes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute order mutations through V1.

        In shadow mode: log mutations but don't execute.
        In live mode: pass through V1's order manager.

        Args:
            mutations: list of OrderMutations to execute
            tick_sizes: optional map of condition_id → tick size

        Returns:
            {
                executed: int,
                skipped: int,
                failed: int,
                shadow: bool,
                details: list[dict]
            }
        """
        tick_sizes = tick_sizes or {}
        result: dict[str, Any] = {
            "executed": 0,
            "skipped": 0,
            "failed": 0,
            "shadow": self.shadow_mode,
            "details": [],
        }

        if not self.shadow_mode and not self.order_manager:
            raise RuntimeError(
                "V1 bridge live mode requires order_manager. "
                "Set shadow_mode=True or provide order_manager."
            )

        if not mutations:
            logger.debug("execute_mutations_empty")
            return result

        logger.info(
            "execute_mutations_started",
            mutations=len(mutations),
            shadow=self.shadow_mode,
            controller=self.controller_label,
            stage=self.stage_name,
            strategy=self.strategy_label,
        )

        for mutation in mutations:
            detail: dict[str, Any] = {
                "controller": self.controller_label,
                "stage": self.stage_name,
                "strategy": self.strategy_label,
                "action": mutation.action,
                "condition_id": mutation.condition_id,
                "token_id": mutation.token_id,
                "side": mutation.side,
                "price": mutation.price,
                "size": mutation.size,
                "order_id": mutation.order_id,
                "reason": mutation.reason,
                "timestamp": time.time(),
            }

            if self.shadow_mode:
                # Shadow mode: just log
                logger.info(
                    "pmm2_shadow_mutation",
                    controller=self.controller_label,
                    stage=self.stage_name,
                    strategy=self.strategy_label,
                    live_capital_pct=self.live_capital_pct,
                    action=mutation.action,
                    condition_id=mutation.condition_id,
                    token_id=mutation.token_id,
                    side=mutation.side,
                    price=mutation.price,
                    size=mutation.size,
                    order_id=mutation.order_id,
                    reason=mutation.reason,
                )
                detail["status"] = "shadow"
                result["executed"] += 1
                self.mutation_log.append(detail)
                result["details"].append(detail)
                continue

            # Live mode: execute through V1
            try:
                if mutation.action == "add":
                    success = await self._execute_add(mutation)
                elif mutation.action == "cancel":
                    success = await self._execute_cancel(mutation)
                elif mutation.action == "amend":
                    success = await self._execute_amend(mutation)
                else:
                    logger.error("unknown_mutation_action", action=mutation.action)
                    success = False

                if success:
                    detail["status"] = "executed"
                    result["executed"] += 1
                else:
                    detail["status"] = "failed"
                    result["failed"] += 1

            except Exception as e:
                logger.error(
                    "mutation_execution_failed",
                    action=mutation.action,
                    error=str(e),
                )
                detail["status"] = "failed"
                detail["error"] = str(e)
                result["failed"] += 1

            self.mutation_log.append(detail)
            result["details"].append(detail)

        logger.info(
            "execute_mutations_complete",
            executed=result["executed"],
            failed=result["failed"],
            shadow=self.shadow_mode,
            controller=self.controller_label,
            stage=self.stage_name,
        )

        return result

    async def _execute_add(self, mutation: OrderMutation) -> bool:
        """Execute an 'add' mutation via V1 order manager.

        Creates a signed order through V1's CLOB private client.

        Args:
            mutation: OrderMutation with action="add"

        Returns:
            True if successful, False otherwise
        """
        if not self.order_manager:
            logger.error("order_manager_not_available")
            return False

        try:
            from pmm1.api.clob_private import CreateOrderRequest, OrderSide, OrderType

            req = CreateOrderRequest(
                token_id=mutation.token_id,
                price=str(mutation.price),
                size=str(mutation.size),
                side=OrderSide(mutation.side),
                order_type=OrderType.GTC,
                neg_risk=False,
            )
            submit_order = getattr(self.order_manager, "submit_order", None)
            if submit_order is None:
                logger.error("order_manager_missing_submit_order")
                return False

            result = await submit_order(
                req,
                condition_id=mutation.condition_id,
                strategy=self.strategy_label,
            )
            success = bool(result.get("success"))

            logger.info(
                "v1_order_add",
                controller=self.controller_label,
                stage=self.stage_name,
                strategy=self.strategy_label,
                condition_id=mutation.condition_id,
                token_id=mutation.token_id,
                side=mutation.side,
                price=mutation.price,
                size=mutation.size,
                order_id=result.get("order_id", ""),
                success=success,
            )
            return success

        except Exception as e:
            logger.error("v1_order_add_failed", error=str(e))
            return False

    async def _execute_cancel(self, mutation: OrderMutation) -> bool:
        """Execute a 'cancel' mutation via V1 order manager.

        Cancels an order through V1's CLOB private client.

        Args:
            mutation: OrderMutation with action="cancel"

        Returns:
            True if successful, False otherwise
        """
        if not self.order_manager:
            logger.error("order_manager_not_available")
            return False

        try:
            client = getattr(self.order_manager, "_client", None)
            tracker = getattr(self.order_manager, "_tracker", None)
            if not client:
                logger.error("order_manager_missing_client")
                return False
            tracked = tracker.get(mutation.order_id) if tracker else None
            prior_strategy = getattr(tracked, "strategy", "")

            await client.cancel_orders([mutation.order_id])
            if tracker is not None:
                from pmm1.state.orders import OrderState

                tracker.update_state(mutation.order_id, OrderState.CANCELED, source="pmm2_cancel")
            success = True

            logger.info(
                "v1_order_cancel",
                controller=self.controller_label,
                stage=self.stage_name,
                strategy=self.strategy_label,
                order_id=mutation.order_id,
                prior_strategy=prior_strategy,
                success=success,
            )
            return success

        except Exception as e:
            logger.error("v1_order_cancel_failed", error=str(e))
            return False

    async def _execute_amend(self, mutation: OrderMutation) -> bool:
        """Execute an 'amend' mutation via V1 order manager.

        Polymarket CLOB doesn't support amend — cancel + re-add.

        Args:
            mutation: OrderMutation with action="amend"

        Returns:
            True if successful, False otherwise
        """
        if not self.order_manager:
            logger.error("order_manager_not_available")
            return False

        try:
            client = getattr(self.order_manager, "_client", None)
            tracker = getattr(self.order_manager, "_tracker", None)
            submit_order = getattr(self.order_manager, "submit_order", None)
            if not client:
                logger.error("order_manager_missing_client")
                return False
            if submit_order is None:
                logger.error("order_manager_missing_submit_order")
                return False

            # Step 1: Cancel existing order
            if mutation.order_id:
                try:
                    await client.cancel_orders([mutation.order_id])
                    if tracker is not None:
                        from pmm1.state.orders import OrderState

                        tracker.update_state(
                            mutation.order_id,
                            OrderState.CANCELED,
                            source="pmm2_amend",
                        )
                except Exception as cancel_err:
                    logger.warning(
                        "v1_amend_cancel_failed",
                        order_id=mutation.order_id,
                        error=str(cancel_err),
                    )
                    # Continue to place new order even if cancel fails
                    # (order may have already been filled/canceled)

            # Step 2: Place new order at amended price/size
            from pmm1.api.clob_private import CreateOrderRequest, OrderSide, OrderType

            req = CreateOrderRequest(
                token_id=mutation.token_id,
                price=str(mutation.price),
                size=str(mutation.size),
                side=OrderSide(mutation.side),
                order_type=OrderType.GTC,
                neg_risk=False,
            )
            result = await submit_order(
                req,
                condition_id=mutation.condition_id,
                strategy=self.strategy_label,
            )
            success = bool(result.get("success"))

            logger.info(
                "v1_order_amend",
                controller=self.controller_label,
                stage=self.stage_name,
                strategy=self.strategy_label,
                order_id=mutation.order_id,
                new_price=mutation.price,
                new_size=mutation.size,
                replacement_order_id=result.get("order_id", ""),
                success=success,
            )
            return success

        except Exception as e:
            logger.error("v1_order_amend_failed", error=str(e))
            return False

    def get_mutation_log(self, since_sec: float = 3600) -> list[dict[str, Any]]:
        """Get recent mutation log entries.

        Args:
            since_sec: return entries from last N seconds (default 1 hour)

        Returns:
            List of mutation log dicts
        """
        cutoff = time.time() - since_sec
        recent = [
            entry for entry in self.mutation_log if entry["timestamp"] >= cutoff
        ]
        return recent

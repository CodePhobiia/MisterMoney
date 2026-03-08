"""V1 execution bridge — PMM-2 decisions → V1 order manager.

PMM-2 outputs OrderMutations → V1's order manager executes them.
V1's heartbeat, risk engine, reconciliation remain authoritative.
PMM-2 NEVER bypasses V1 safety layers.

Shadow mode (default): log all decisions but execute nothing.
Live mode: pass through V1's order manager with full safety checks.
"""

from __future__ import annotations

import time

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
        order_manager=None,
        risk_limits=None,
        shadow_mode: bool = True,
    ):
        """Initialize V1 bridge.

        Args:
            order_manager: V1's order manager (from bot_state)
            risk_limits: V1's risk limits (from bot_state)
            shadow_mode: if True, log only (don't execute)
        """
        self.order_manager = order_manager
        self.risk_limits = risk_limits
        self.shadow_mode = shadow_mode
        self.mutation_log: list[dict] = []

        logger.info(
            "v1_bridge_initialized",
            shadow_mode=shadow_mode,
            has_order_manager=order_manager is not None,
        )

    async def execute_mutations(
        self,
        mutations: list[OrderMutation],
        tick_sizes: dict[str, any] | None = None,
    ) -> dict:
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
        result = {
            "executed": 0,
            "skipped": 0,
            "failed": 0,
            "shadow": self.shadow_mode,
            "details": [],
        }

        if not self.shadow_mode:
            raise NotImplementedError(
                "V1 bridge live execution not implemented. "
                "PMM-2 must run in shadow_mode=True until bridge methods are wired to V1 order manager. "
                "Set pmm2.shadow_mode=true in config."
            )

        if not mutations:
            logger.debug("execute_mutations_empty")
            return result

        logger.info(
            "execute_mutations_started",
            mutations=len(mutations),
            shadow=self.shadow_mode,
        )

        for mutation in mutations:
            detail = {
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
        )

        return result

    async def _execute_add(self, mutation: OrderMutation) -> bool:
        """Execute an 'add' mutation via V1 order manager.

        Args:
            mutation: OrderMutation with action="add"

        Returns:
            True if successful, False otherwise
        """
        if not self.order_manager:
            logger.error("order_manager_not_available")
            return False

        try:
            # V1 order manager should have a method like:
            # await self.order_manager.place_order(
            #     token_id=mutation.token_id,
            #     side=mutation.side,
            #     price=mutation.price,
            #     size=mutation.size,
            # )
            #
            # For now, we'll log that we would call it
            logger.info(
                "v1_order_add",
                token_id=mutation.token_id,
                side=mutation.side,
                price=mutation.price,
                size=mutation.size,
            )

            # TODO: Integrate with actual V1 order manager when available
            # This is a placeholder for the integration point
            return True

        except Exception as e:
            logger.error("v1_order_add_failed", error=str(e))
            return False

    async def _execute_cancel(self, mutation: OrderMutation) -> bool:
        """Execute a 'cancel' mutation via V1 order manager.

        Args:
            mutation: OrderMutation with action="cancel"

        Returns:
            True if successful, False otherwise
        """
        if not self.order_manager:
            logger.error("order_manager_not_available")
            return False

        try:
            # V1 order manager should have a method like:
            # await self.order_manager.cancel_order(order_id=mutation.order_id)
            logger.info("v1_order_cancel", order_id=mutation.order_id)

            # TODO: Integrate with actual V1 order manager
            return True

        except Exception as e:
            logger.error("v1_order_cancel_failed", error=str(e))
            return False

    async def _execute_amend(self, mutation: OrderMutation) -> bool:
        """Execute an 'amend' mutation via V1 order manager.

        Args:
            mutation: OrderMutation with action="amend"

        Returns:
            True if successful, False otherwise
        """
        if not self.order_manager:
            logger.error("order_manager_not_available")
            return False

        try:
            # V1 order manager may support amend, or we might need to cancel+add
            # await self.order_manager.amend_order(
            #     order_id=mutation.order_id,
            #     new_price=mutation.price,
            #     new_size=mutation.size,
            # )
            logger.info(
                "v1_order_amend",
                order_id=mutation.order_id,
                new_price=mutation.price,
                new_size=mutation.size,
            )

            # TODO: Integrate with actual V1 order manager
            return True

        except Exception as e:
            logger.error("v1_order_amend_failed", error=str(e))
            return False

    def get_mutation_log(self, since_sec: float = 3600) -> list[dict]:
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

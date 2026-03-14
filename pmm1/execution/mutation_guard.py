"""Central live-mutation guard for direct order submission paths."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from pmm1.api.clob_private import CreateOrderRequest, OrderSide
from pmm1.risk.drawdown import DrawdownGovernor
from pmm1.risk.kill_switch import KillSwitch
from pmm1.risk.limits import RiskLimits
from pmm1.state.heartbeats import HeartbeatState
from pmm1.state.inventory import InventoryManager


class MutationGuardDecision(BaseModel):
    """Result of evaluating a direct live order submission."""

    allowed: bool = True
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class LiveMutationGuard:
    """Authorize or block direct live submissions that bypass quote-intent shaping."""

    def __init__(
        self,
        *,
        risk_limits: RiskLimits,
        inventory_manager: InventoryManager,
        kill_switch: KillSwitch,
        drawdown: DrawdownGovernor,
        heartbeat: HeartbeatState | None,
        market_getter: Callable[[str], Any | None],
        resume_token_getter: Callable[[], bool] | None = None,
        resume_reason_getter: Callable[[], str] | None = None,
    ) -> None:
        self._risk_limits = risk_limits
        self._inventory = inventory_manager
        self._kill_switch = kill_switch
        self._drawdown = drawdown
        self._heartbeat = heartbeat
        self._market_getter = market_getter
        self._resume_token_getter = resume_token_getter
        self._resume_reason_getter = resume_reason_getter

    def evaluate(
        self,
        request: CreateOrderRequest,
        *,
        condition_id: str,
        strategy: str,
    ) -> MutationGuardDecision:
        """Return whether a direct live mutation is allowed."""

        if self._kill_switch.is_triggered:
            return MutationGuardDecision(
                allowed=False,
                reason="kill_switch_active",
                details={
                    "active_reasons": [reason.value for reason in self._kill_switch.active_reasons],
                },
            )

        if self._heartbeat is not None and not self._heartbeat.is_healthy:
            return MutationGuardDecision(
                allowed=False,
                reason="heartbeat_unhealthy",
                details={
                    "consecutive_failures": self._heartbeat.consecutive_failures,
                },
            )

        if self._drawdown.state.should_flatten_only:
            return MutationGuardDecision(
                allowed=False,
                reason="drawdown_flatten_only",
                details={"tier": self._drawdown.state.tier.value},
            )

        if self._resume_token_getter is not None and not self._resume_token_getter():
            blocked_reason = (
                self._resume_reason_getter() if self._resume_reason_getter is not None else ""
            )
            return MutationGuardDecision(
                allowed=False,
                reason="resume_token_invalid",
                details={"blocked_reason": blocked_reason},
            )

        if strategy == "taker_bootstrap" and self._drawdown.state.should_pause_taker:
            return MutationGuardDecision(
                allowed=False,
                reason="drawdown_pause_taker",
                details={"tier": self._drawdown.state.tier.value},
            )

        price = float(request.price)
        size = float(request.size)
        side = request.side

        if side == OrderSide.SELL:
            if not self._inventory.can_place_sell(request.token_id, size):
                return MutationGuardDecision(
                    allowed=False,
                    reason="insufficient_inventory",
                    details={
                        "token_id": request.token_id,
                        "size": size,
                    },
                )
            return MutationGuardDecision(allowed=True)

        if not self._inventory.can_place_buy(request.token_id, size, price):
            return MutationGuardDecision(
                allowed=False,
                reason="insufficient_usdc",
                details={
                    "token_id": request.token_id,
                    "size": size,
                    "price": price,
                },
            )

        proposed_dollars = size * price
        market_check = self._risk_limits.check_per_market_gross(
            condition_id,
            proposed_additional_dollars=proposed_dollars,
        )
        if not market_check.passed:
            return MutationGuardDecision(
                allowed=False,
                reason="per_market_gross",
                details={
                    "breaches": list(market_check.breaches),
                    "adjustments": dict(market_check.adjustments),
                },
            )

        market = self._market_getter(condition_id)
        event_id = str(getattr(market, "event_id", "") or "")
        if event_id:
            cluster_check = self._risk_limits.check_per_event_cluster(
                event_id,
                proposed_additional=proposed_dollars,
            )
            if not cluster_check.passed:
                return MutationGuardDecision(
                    allowed=False,
                    reason="event_cluster",
                    details={
                        "event_id": event_id,
                        "breaches": list(cluster_check.breaches),
                        "adjustments": dict(cluster_check.adjustments),
                    },
                )

        # Only BUY orders reach this point — SELLs return early (line 105-115),
        # so proposed_additional_net is always positive here.
        directional_check = self._risk_limits.check_total_directional(
            proposed_additional_net=size * price,
        )
        if not directional_check.passed:
            return MutationGuardDecision(
                allowed=False,
                reason="total_directional",
                details={"breaches": list(directional_check.breaches)},
            )

        return MutationGuardDecision(allowed=True)

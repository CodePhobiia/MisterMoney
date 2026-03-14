"""Inventory manager — free inventory calculation, rebalancing priorities from §13."""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel

from pmm1.state.orders import OrderTracker
from pmm1.state.positions import PositionTracker

logger = structlog.get_logger(__name__)


class InventorySnapshot(BaseModel):
    """Point-in-time snapshot of inventory for a single market."""

    condition_id: str
    token_id_yes: str = ""
    token_id_no: str = ""
    yes_position: float = 0.0
    no_position: float = 0.0
    yes_reserved_in_orders: float = 0.0
    no_reserved_in_orders: float = 0.0
    yes_free: float = 0.0
    no_free: float = 0.0
    net_exposure: float = 0.0
    gross_exposure: float = 0.0


class InventoryManager:
    """Manages inventory across positions and open orders.

    Core formula from §13:
        freeInventory = balance - Σ(openOrderRemaining)

    Rebalancing priorities:
        1. Merge stale paired inventory
        2. Split new collateral
        3. Internal cross-event conversion (neg-risk)
        4. External market hedge (last resort)
    """

    def __init__(
        self,
        position_tracker: PositionTracker,
        order_tracker: OrderTracker,
    ) -> None:
        self.positions = position_tracker
        self.orders = order_tracker
        self._usdc_balance: float = 0.0
        self._usdc_allowance: float = 0.0

    def update_balances(self, balance: float, allowance: float) -> None:
        """Update USDC balance from exchange."""
        self._usdc_balance = balance
        self._usdc_allowance = allowance

    @property
    def usdc_balance(self) -> float:
        return self._usdc_balance

    @property
    def usdc_allowance(self) -> float:
        return self._usdc_allowance

    def get_reserved_in_orders(self, token_id: str, side: str) -> float:
        """Calculate total size reserved in open orders for a token+side.

        For BUY orders: reserves USDC (price × remaining_size)
        For SELL orders: reserves token inventory (remaining_size)
        """
        active_orders = self.orders.get_active_by_side(token_id, side)
        return sum(o.remaining_size_float for o in active_orders)

    def get_buy_reserved_usdc(self, token_id: str) -> float:
        """Total USDC reserved in BUY orders for a token."""
        active_buys = self.orders.get_active_by_side(token_id, "BUY")
        return sum(o.remaining_size_float * o.price_float for o in active_buys)

    def get_sell_reserved_shares(self, token_id: str) -> float:
        """Total shares reserved in SELL orders for a token."""
        active_sells = self.orders.get_active_by_side(token_id, "SELL")
        return sum(o.remaining_size_float for o in active_sells)

    def get_inventory_snapshot(self, condition_id: str) -> InventorySnapshot | None:
        """Get full inventory snapshot for a market.

        Free inventory = position - reserved_in_sell_orders
        """
        pos = self.positions.get(condition_id)
        if pos is None:
            return None

        yes_sell_reserved = self.get_sell_reserved_shares(pos.token_id_yes)
        no_sell_reserved = self.get_sell_reserved_shares(pos.token_id_no)

        yes_free = max(0.0, pos.yes_size - yes_sell_reserved)
        no_free = max(0.0, pos.no_size - no_sell_reserved)

        return InventorySnapshot(
            condition_id=condition_id,
            token_id_yes=pos.token_id_yes,
            token_id_no=pos.token_id_no,
            yes_position=pos.yes_size,
            no_position=pos.no_size,
            yes_reserved_in_orders=yes_sell_reserved,
            no_reserved_in_orders=no_sell_reserved,
            yes_free=yes_free,
            no_free=no_free,
            net_exposure=pos.net_exposure,
            gross_exposure=pos.gross_exposure,
        )

    def get_free_usdc(self) -> float:
        """USDC available for new buy orders.

        free_usdc = balance - Σ(all_buy_order_reserved_usdc)
        """
        total_buy_reserved = 0.0
        for token_id in self.orders._by_token:
            total_buy_reserved += self.get_buy_reserved_usdc(token_id)
        return max(0.0, self._usdc_balance - total_buy_reserved)

    def can_place_buy(
        self,
        token_id: str,
        size: float,
        price: float,
    ) -> bool:
        """Check if we have enough free USDC for a buy order."""
        required_usdc = size * price
        return self.get_free_usdc() >= required_usdc

    def can_place_sell(
        self,
        token_id: str,
        size: float,
    ) -> bool:
        """Check if we have enough free inventory to sell."""
        pos = self.positions.get_by_token(token_id)
        if pos is None:
            return False

        is_yes = token_id == pos.token_id_yes
        current_size = pos.yes_size if is_yes else pos.no_size
        reserved = self.get_sell_reserved_shares(token_id)
        free = current_size - reserved
        return free >= size

    def get_max_buy_size(self, price: float) -> float:
        """Maximum buy size given current free USDC."""
        if price <= 0:
            return 0.0
        return self.get_free_usdc() / price

    def get_max_sell_size(self, token_id: str) -> float:
        """Maximum sell size given current free inventory."""
        pos = self.positions.get_by_token(token_id)
        if pos is None:
            return 0.0

        is_yes = token_id == pos.token_id_yes
        current_size = pos.yes_size if is_yes else pos.no_size
        reserved = self.get_sell_reserved_shares(token_id)
        return max(0.0, current_size - reserved)

    def check_merge_opportunity(self, condition_id: str) -> float:
        """Check if we have paired YES+NO inventory that can be merged for USDC.

        Returns the number of shares that can be merged (min of YES, NO sizes).
        """
        pos = self.positions.get(condition_id)
        if pos is None:
            return 0.0
        return min(pos.yes_size, pos.no_size)

    def get_rebalance_actions(self, condition_id: str) -> list[dict[str, Any]]:
        """Get recommended rebalancing actions for a market.

        Priority from §13:
            1. Merge stale paired inventory
            2. Split new collateral
            3. Internal cross-event conversion (neg-risk)
            4. External market hedge (last resort)
        """
        actions: list[dict[str, Any]] = []
        pos = self.positions.get(condition_id)
        if pos is None:
            return actions

        # 1. Merge opportunity
        merge_size = self.check_merge_opportunity(condition_id)
        if merge_size > 1.0:  # Only if meaningful
            actions.append({
                "type": "merge",
                "condition_id": condition_id,
                "size": merge_size,
                "priority": 1,
                "description": f"Merge {merge_size:.1f} paired YES+NO → USDC",
            })

        # 2. Split opportunity (if we have USDC and no position)
        if pos.is_flat and self.get_free_usdc() > 10:
            actions.append({
                "type": "split",
                "condition_id": condition_id,
                "priority": 2,
                "description": "Split USDC into YES+NO tokens",
            })

        # 3. Neg-risk conversion (if applicable)
        if pos.neg_risk and abs(pos.net_exposure) > 0:
            actions.append({
                "type": "neg_risk_conversion",
                "condition_id": condition_id,
                "net_exposure": pos.net_exposure,
                "priority": 3,
                "description": f"Neg-risk conversion opportunity (net={pos.net_exposure:.1f})",
            })

        return sorted(actions, key=lambda a: a["priority"])

    def get_total_nav_estimate(self, price_oracle: dict[str, float] | None = None) -> float:
        """Estimate total NAV (Net Asset Value).

        NAV = USDC balance + Σ(position_values at current prices)
        """
        nav = self._usdc_balance

        for pos in self.positions._positions.values():
            if price_oracle:
                yes_price = price_oracle.get(pos.token_id_yes, pos.yes_avg_price)
                no_price = price_oracle.get(pos.token_id_no, pos.no_avg_price)
            else:
                yes_price = pos.yes_avg_price
                no_price = pos.no_avg_price

            nav += pos.yes_size * yes_price
            nav += pos.no_size * no_price

        return nav

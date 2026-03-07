"""Position tracking — YES/NO inventory per market."""

from __future__ import annotations

import time
from typing import Any

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class MarketPosition(BaseModel):
    """Position in a single market (condition), tracking YES and NO sides."""

    condition_id: str
    token_id_yes: str = ""
    token_id_no: str = ""
    yes_size: float = 0.0
    no_size: float = 0.0
    yes_avg_price: float = 0.0
    no_avg_price: float = 0.0
    yes_cost_basis: float = 0.0
    no_cost_basis: float = 0.0
    realized_pnl: float = 0.0
    neg_risk: bool = False
    event_id: str = ""
    last_update: float = Field(default_factory=time.time)

    @property
    def net_exposure(self) -> float:
        """Net directional exposure: positive = long YES, negative = long NO.

        For binary: YES position is effectively long, NO is short.
        net = yes_size - no_size (in share terms).
        """
        return self.yes_size - self.no_size

    @property
    def gross_exposure(self) -> float:
        """Total absolute exposure."""
        return self.yes_size + self.no_size

    @property
    def yes_value(self) -> float:
        """Current YES position value at cost basis."""
        return self.yes_size * self.yes_avg_price

    @property
    def no_value(self) -> float:
        """Current NO position value at cost basis."""
        return self.no_size * self.no_avg_price

    @property
    def total_cost_basis(self) -> float:
        return self.yes_cost_basis + self.no_cost_basis

    @property
    def is_flat(self) -> bool:
        return self.yes_size == 0 and self.no_size == 0

    def apply_fill(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        fee: float = 0.0,
    ) -> None:
        """Apply a trade fill to update position.

        Args:
            token_id: Which token was traded (YES or NO).
            side: BUY or SELL.
            size: Fill size.
            price: Fill price.
            fee: Trade fee.
        """
        is_yes = token_id == self.token_id_yes

        if side == "BUY":
            if is_yes:
                # Buying YES → increasing YES position
                old_cost = self.yes_cost_basis
                old_size = self.yes_size
                new_size = old_size + size
                new_cost = old_cost + (size * price) + fee
                self.yes_size = new_size
                self.yes_cost_basis = new_cost
                self.yes_avg_price = new_cost / new_size if new_size > 0 else 0.0
            else:
                # Buying NO → increasing NO position
                old_cost = self.no_cost_basis
                old_size = self.no_size
                new_size = old_size + size
                new_cost = old_cost + (size * price) + fee
                self.no_size = new_size
                self.no_cost_basis = new_cost
                self.no_avg_price = new_cost / new_size if new_size > 0 else 0.0
        else:  # SELL
            if is_yes:
                # Selling YES → reducing YES position
                if self.yes_size > 0:
                    cost_per_share = self.yes_avg_price
                    pnl = size * (price - cost_per_share) - fee
                    self.realized_pnl += pnl
                    self.yes_size = max(0.0, self.yes_size - size)
                    self.yes_cost_basis = self.yes_size * self.yes_avg_price
            else:
                # Selling NO → reducing NO position
                if self.no_size > 0:
                    cost_per_share = self.no_avg_price
                    pnl = size * (price - cost_per_share) - fee
                    self.realized_pnl += pnl
                    self.no_size = max(0.0, self.no_size - size)
                    self.no_cost_basis = self.no_size * self.no_avg_price

        self.last_update = time.time()
        logger.debug(
            "position_updated",
            condition_id=self.condition_id[:16],
            token_id=token_id[:16],
            side=side,
            size=size,
            price=price,
            yes_size=self.yes_size,
            no_size=self.no_size,
        )

    def mark_to_market(self, yes_price: float, no_price: float) -> float:
        """Calculate unrealized PnL at current market prices."""
        yes_unrealized = self.yes_size * (yes_price - self.yes_avg_price) if self.yes_size > 0 else 0.0
        no_unrealized = self.no_size * (no_price - self.no_avg_price) if self.no_size > 0 else 0.0
        return yes_unrealized + no_unrealized

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class PositionTracker:
    """Tracks positions across all markets."""

    def __init__(self) -> None:
        self._positions: dict[str, MarketPosition] = {}  # condition_id → position
        self._by_event: dict[str, set[str]] = {}  # event_id → set of condition_ids
        self._token_to_condition: dict[str, str] = {}  # token_id → condition_id

    def register_market(
        self,
        condition_id: str,
        token_id_yes: str,
        token_id_no: str,
        neg_risk: bool = False,
        event_id: str = "",
    ) -> MarketPosition:
        """Register a market for position tracking."""
        if condition_id not in self._positions:
            self._positions[condition_id] = MarketPosition(
                condition_id=condition_id,
                token_id_yes=token_id_yes,
                token_id_no=token_id_no,
                neg_risk=neg_risk,
                event_id=event_id,
            )
        else:
            pos = self._positions[condition_id]
            pos.token_id_yes = token_id_yes
            pos.token_id_no = token_id_no
            pos.neg_risk = neg_risk
            pos.event_id = event_id

        self._token_to_condition[token_id_yes] = condition_id
        self._token_to_condition[token_id_no] = condition_id

        if event_id:
            self._by_event.setdefault(event_id, set()).add(condition_id)

        return self._positions[condition_id]

    def get(self, condition_id: str) -> MarketPosition | None:
        """Get position for a market."""
        return self._positions.get(condition_id)

    def get_by_token(self, token_id: str) -> MarketPosition | None:
        """Get position by token ID (YES or NO)."""
        condition_id = self._token_to_condition.get(token_id)
        if condition_id:
            return self._positions.get(condition_id)
        return None

    def apply_fill(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        fee: float = 0.0,
    ) -> MarketPosition | None:
        """Apply a fill to the appropriate position."""
        position = self.get_by_token(token_id)
        if position is None:
            logger.warning("fill_for_untracked_market", token_id=token_id[:16])
            return None
        position.apply_fill(token_id, side, size, price, fee)
        return position

    def get_event_positions(self, event_id: str) -> list[MarketPosition]:
        """Get all positions for an event."""
        condition_ids = self._by_event.get(event_id, set())
        return [
            self._positions[cid]
            for cid in condition_ids
            if cid in self._positions
        ]

    def get_event_net_exposure(self, event_id: str) -> float:
        """Total net exposure across all markets in an event."""
        return sum(p.net_exposure for p in self.get_event_positions(event_id))

    def get_event_gross_exposure(self, event_id: str) -> float:
        """Total gross exposure across all markets in an event."""
        return sum(p.gross_exposure for p in self.get_event_positions(event_id))

    def get_total_net_exposure(self) -> float:
        """Total net directional exposure across all positions."""
        return sum(abs(p.net_exposure) for p in self._positions.values())

    def get_total_gross_exposure(self) -> float:
        """Total gross exposure across all positions."""
        return sum(p.gross_exposure for p in self._positions.values())

    def get_total_realized_pnl(self) -> float:
        """Sum of realized PnL across all positions."""
        return sum(p.realized_pnl for p in self._positions.values())

    def get_active_positions(self) -> list[MarketPosition]:
        """Get positions that are not flat."""
        return [p for p in self._positions.values() if not p.is_flat]

    def reconcile_with_exchange(
        self, exchange_positions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Reconcile local positions with exchange data.
        
        Also zeros out local positions that no longer exist on the exchange.
        """
        mismatches = []
        
        # Build set of tokens the exchange reports we hold
        exchange_tokens: set[str] = set()
        for ep in exchange_positions:
            token_id = ep.get("asset", "")
            exchange_size = float(ep.get("size", 0))
            if exchange_size > 0 and token_id:
                exchange_tokens.add(token_id)
        
        # Zero out local positions not on exchange
        for cid, pos in list(self._positions.items()):
            if pos.yes_size > 0 and pos.token_id_yes and pos.token_id_yes not in exchange_tokens:
                logger.info("position_zeroed_by_exchange",
                            condition_id=cid[:16],
                            token_id=pos.token_id_yes[:16],
                            old_size=pos.yes_size,
                            side="YES")
                pos.yes_size = 0.0
                pos.yes_cost_basis = 0.0
            if pos.no_size > 0 and pos.token_id_no and pos.token_id_no not in exchange_tokens:
                logger.info("position_zeroed_by_exchange",
                            condition_id=cid[:16],
                            token_id=pos.token_id_no[:16],
                            old_size=pos.no_size,
                            side="NO")
                pos.no_size = 0.0
                pos.no_cost_basis = 0.0
        
        for ep in exchange_positions:
            token_id = ep.get("asset", "")
            exchange_size = float(ep.get("size", 0))
            pos = self.get_by_token(token_id)

            if pos is None:
                if exchange_size > 0:
                    mismatches.append({
                        "type": "unknown_position",
                        "token_id": token_id,
                        "exchange_size": exchange_size,
                    })
                    # Auto-adopt: create position from exchange truth
                    # so sell logic can manage it.
                    # Use token_id as condition_id (best guess);
                    # also register in token→condition map.
                    self._positions[token_id] = MarketPosition(
                        condition_id=token_id,
                        token_id_yes=token_id,
                        token_id_no="",
                        yes_size=exchange_size,
                        no_size=0.0,
                        yes_avg_price=0.0,  # unknown entry — handled by exit logic
                        no_avg_price=0.0,
                    )
                    self._token_to_condition[token_id] = token_id
                    logger.info("position_auto_adopted",
                                token_id=token_id[:16],
                                size=exchange_size)
                continue

            is_yes = token_id == pos.token_id_yes
            local_size = pos.yes_size if is_yes else pos.no_size

            if abs(local_size - exchange_size) > 0.01:
                mismatches.append({
                    "type": "size_mismatch",
                    "token_id": token_id,
                    "local_size": local_size,
                    "exchange_size": exchange_size,
                    "side": "YES" if is_yes else "NO",
                })
                # Auto-correct to exchange truth
                if is_yes:
                    pos.yes_size = exchange_size
                else:
                    pos.no_size = exchange_size

        if mismatches:
            logger.warning("position_reconciliation_mismatches", mismatches=mismatches)
        else:
            logger.debug("position_reconciliation_clean")

        return {"mismatches": mismatches, "count": len(mismatches)}

    @property
    def market_count(self) -> int:
        return len(self._positions)

    @property
    def active_count(self) -> int:
        return len(self.get_active_positions())

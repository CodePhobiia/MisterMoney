"""PnL attribution — break down PnL by strategy, market, time period."""

from __future__ import annotations

import time
from collections import defaultdict

import structlog
from pydantic import BaseModel, Field

from pmm1.analytics.pnl import FillRecord

logger = structlog.get_logger(__name__)


class StrategyAttribution(BaseModel):
    """PnL attribution for a single strategy."""

    strategy: str
    total_pnl: float = 0.0
    fill_count: int = 0
    total_volume: float = 0.0
    total_fees: float = 0.0
    avg_edge_per_trade: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0


class MarketAttribution(BaseModel):
    """PnL attribution for a single market."""

    condition_id: str
    total_pnl: float = 0.0
    fill_count: int = 0
    total_volume: float = 0.0
    spread_capture: float = 0.0
    adverse_selection: float = 0.0
    inventory_pnl: float = 0.0


class Attribution(BaseModel):
    """Complete PnL attribution breakdown."""

    # By strategy
    by_strategy: dict[str, StrategyAttribution] = Field(default_factory=dict)
    # By market
    by_market: dict[str, MarketAttribution] = Field(default_factory=dict)
    # Totals
    total_pnl: float = 0.0
    total_volume: float = 0.0
    total_fills: int = 0
    period_start: float = 0.0
    period_end: float = Field(default_factory=time.time)


class PnLAttributor:
    """Computes PnL attribution from fill records."""

    def __init__(self) -> None:
        self._fills: list[FillRecord] = []

    def add_fill(self, fill: FillRecord) -> None:
        """Add a fill record for attribution."""
        self._fills.append(fill)

    def compute(
        self,
        market_carry: dict[str, float] | None = None,
    ) -> Attribution:
        """Compute full PnL attribution.

        Args:
            market_carry: Per-market carry PnL from InventoryCarryTracker.
                          Maps condition_id -> carry amount.
        """
        attribution = Attribution()

        if not self._fills:
            return attribution

        carry = market_carry or {}
        attribution.period_start = self._fills[0].fill_timestamp
        attribution.total_fills = len(self._fills)

        # Group by strategy
        strategy_fills: dict[str, list[FillRecord]] = defaultdict(list)
        market_fills: dict[str, list[FillRecord]] = defaultdict(list)

        for fill in self._fills:
            strategy = fill.strategy or "unknown"
            strategy_fills[strategy].append(fill)
            market_fills[fill.condition_id].append(fill)

        # Compute strategy attribution
        for strategy, fills in strategy_fills.items():
            attr = self._compute_strategy_attribution(strategy, fills)
            attribution.by_strategy[strategy] = attr
            attribution.total_pnl += attr.total_pnl
            attribution.total_volume += attr.total_volume

        # Compute market attribution
        for condition_id, fills in market_fills.items():
            carry_pnl = carry.get(condition_id, 0.0)
            mattr = self._compute_market_attribution(
                condition_id, fills, carry_pnl=carry_pnl,
            )
            attribution.by_market[condition_id] = mattr

        return attribution

    def _compute_strategy_attribution(
        self, strategy: str, fills: list[FillRecord]
    ) -> StrategyAttribution:
        """Compute attribution for a single strategy."""
        total_pnl = 0.0
        total_volume = 0.0
        total_fees = 0.0
        wins = 0
        losses = 0
        win_sum = 0.0
        loss_sum = 0.0

        for fill in fills:
            volume = fill.price * fill.size
            total_volume += volume
            total_fees += fill.fee

            # Estimate per-fill PnL
            if fill.side == "BUY":
                # Bought: profit if mid was above our price
                fill_pnl = (fill.mid_at_fill - fill.price) * fill.size - fill.fee
            else:
                fill_pnl = (fill.price - fill.mid_at_fill) * fill.size - fill.fee

            total_pnl += fill_pnl

            if fill_pnl > 0:
                wins += 1
                win_sum += fill_pnl
            elif fill_pnl < 0:
                losses += 1
                loss_sum += fill_pnl

        total = wins + losses
        win_rate = wins / total if total > 0 else 0.0
        avg_win = win_sum / wins if wins > 0 else 0.0
        avg_loss = loss_sum / losses if losses > 0 else 0.0
        avg_edge = total_pnl / len(fills) if fills else 0.0

        return StrategyAttribution(
            strategy=strategy,
            total_pnl=total_pnl,
            fill_count=len(fills),
            total_volume=total_volume,
            total_fees=total_fees,
            avg_edge_per_trade=avg_edge,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
        )

    def _compute_market_attribution(
        self,
        condition_id: str,
        fills: list[FillRecord],
        carry_pnl: float = 0.0,
    ) -> MarketAttribution:
        """Compute attribution for a single market.

        Args:
            condition_id: Market condition ID.
            fills: Fill records for this market.
            carry_pnl: Inventory carry PnL from InventoryCarryTracker.
        """
        total_pnl = 0.0
        total_volume = 0.0
        spread_capture = 0.0
        adverse_selection = 0.0

        for fill in fills:
            volume = fill.price * fill.size
            total_volume += volume

            # Spread capture
            if fill.side == "BUY":
                sc = (fill.mid_at_fill - fill.price) * fill.size
            else:
                sc = (fill.price - fill.mid_at_fill) * fill.size
            spread_capture += sc

            # Adverse selection (using 5s mark)
            if fill.mid_5s_after is not None:
                if fill.side == "BUY":
                    as_val = (fill.mid_5s_after - fill.mid_at_fill) * fill.size
                else:
                    as_val = (fill.mid_at_fill - fill.mid_5s_after) * fill.size
                adverse_selection -= max(0, -as_val)

            fill_pnl = sc - fill.fee
            total_pnl += fill_pnl

        return MarketAttribution(
            condition_id=condition_id,
            total_pnl=total_pnl + carry_pnl,
            fill_count=len(fills),
            total_volume=total_volume,
            spread_capture=spread_capture,
            adverse_selection=adverse_selection,
            inventory_pnl=carry_pnl,
        )

    def reset(self) -> Attribution:
        """Compute final attribution and reset."""
        result = self.compute()
        self._fills.clear()
        return result

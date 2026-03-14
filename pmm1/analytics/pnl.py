"""PnL decomposition from §16.

Components:
- Spread capture
- Adverse selection (1s / 5s / 30s)
- Inventory carry PnL
- Arb locked-in PnL
- Maker rebates
- Liquidity rewards
- Slippage
- Reject/cancel costs
"""

from __future__ import annotations

import time

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class PnLComponent(BaseModel):
    """A single PnL component."""

    name: str
    amount: float = 0.0
    trade_count: int = 0
    avg_per_trade: float = 0.0


class PnLSnapshot(BaseModel):
    """Complete PnL decomposition at a point in time."""

    # Core spread PnL
    spread_capture: float = 0.0
    adverse_selection_1s: float = 0.0
    adverse_selection_5s: float = 0.0
    adverse_selection_30s: float = 0.0

    # Other PnL sources
    inventory_carry: float = 0.0
    arb_pnl: float = 0.0
    maker_rebates: float = 0.0
    liquidity_rewards: float = 0.0

    # Costs
    slippage: float = 0.0
    reject_cancel_costs: float = 0.0
    fees_paid: float = 0.0

    # Totals
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    total_trades: int = 0
    total_volume: float = 0.0

    # Time period
    period_start: float = 0.0
    period_end: float = Field(default_factory=time.time)
    period_label: str = ""

    @property
    def total_adverse_selection(self) -> float:
        return self.adverse_selection_1s + self.adverse_selection_5s + self.adverse_selection_30s

    @property
    def net_spread(self) -> float:
        """Spread capture minus adverse selection."""
        return self.spread_capture + self.total_adverse_selection  # AS is negative

    @property
    def as_ratio_5s(self) -> float:
        """5-second adverse selection as ratio of spread capture.

        Target from §16: AS < 60% of spread capture.
        """
        if self.spread_capture == 0:
            return 0.0
        return abs(self.adverse_selection_5s) / self.spread_capture


class FillRecord(BaseModel):
    """A fill with pre/post price for PnL attribution."""

    order_id: str = ""
    token_id: str = ""
    condition_id: str = ""
    side: str = ""  # BUY or SELL
    price: float = 0.0
    size: float = 0.0
    fee: float = 0.0
    strategy: str = ""  # mm, parity_arb, neg_risk_arb
    fill_timestamp: float = 0.0
    # Market prices at fill time and after
    mid_at_fill: float = 0.0
    mid_1s_after: float | None = None
    mid_5s_after: float | None = None
    mid_30s_after: float | None = None


class PnLTracker:
    """Tracks and decomposes PnL in real-time.

    Accumulates fill records and computes PnL components.
    """

    def __init__(self) -> None:
        self._fills: list[FillRecord] = []
        self._daily_rebates: float = 0.0
        self._daily_rewards: float = 0.0
        self._daily_start_ts: float = time.time()
        self._reject_count: int = 0

    def record_fill(self, fill: FillRecord) -> None:
        """Record a fill for PnL tracking."""
        self._fills.append(fill)

    def update_post_fill_prices(
        self,
        order_id: str,
        mid_1s: float | None = None,
        mid_5s: float | None = None,
        mid_30s: float | None = None,
    ) -> None:
        """Update post-fill midpoints for adverse selection calculation.

        Called after 1s, 5s, 30s delays to measure price impact.
        """
        for fill in reversed(self._fills):
            if fill.order_id == order_id:
                if mid_1s is not None:
                    fill.mid_1s_after = mid_1s
                if mid_5s is not None:
                    fill.mid_5s_after = mid_5s
                if mid_30s is not None:
                    fill.mid_30s_after = mid_30s
                break

    def record_rebates(self, amount: float) -> None:
        """Record maker rebate payment."""
        self._daily_rebates += amount

    def record_rewards(self, amount: float) -> None:
        """Record liquidity reward payment."""
        self._daily_rewards += amount

    def record_reject(self) -> None:
        """Record an order rejection."""
        self._reject_count += 1

    def compute_snapshot(self, period_label: str = "session") -> PnLSnapshot:
        """Compute current PnL decomposition."""
        snapshot = PnLSnapshot(
            period_start=self._daily_start_ts,
            period_label=period_label,
        )

        total_volume = 0.0

        for fill in self._fills:
            volume = fill.price * fill.size
            total_volume += volume

            if fill.strategy in ("parity_arb", "neg_risk_arb"):
                # Arb PnL
                if fill.side == "BUY":
                    snapshot.arb_pnl -= volume + fill.fee
                else:
                    snapshot.arb_pnl += volume - fill.fee
            else:
                # Market making PnL — decompose into spread + adverse selection
                # Spread capture: difference between fill price and mid at fill
                if fill.side == "BUY":
                    spread_component = fill.mid_at_fill - fill.price
                else:
                    spread_component = fill.price - fill.mid_at_fill
                snapshot.spread_capture += spread_component * fill.size

                # Adverse selection: how much mid moved against us after fill
                if fill.mid_1s_after is not None:
                    if fill.side == "BUY":
                        as_1s = (fill.mid_1s_after - fill.mid_at_fill) * fill.size
                    else:
                        as_1s = (fill.mid_at_fill - fill.mid_1s_after) * fill.size
                    # If mid moved in our favor, AS is negative (good for us)
                    # If mid moved against us, AS is positive (bad — we subtract it)
                    snapshot.adverse_selection_1s -= max(0, -as_1s)

                if fill.mid_5s_after is not None:
                    if fill.side == "BUY":
                        as_5s = (fill.mid_5s_after - fill.mid_at_fill) * fill.size
                    else:
                        as_5s = (fill.mid_at_fill - fill.mid_5s_after) * fill.size
                    snapshot.adverse_selection_5s -= max(0, -as_5s)

                if fill.mid_30s_after is not None:
                    if fill.side == "BUY":
                        as_30s = (fill.mid_30s_after - fill.mid_at_fill) * fill.size
                    else:
                        as_30s = (fill.mid_at_fill - fill.mid_30s_after) * fill.size
                    snapshot.adverse_selection_30s -= max(0, -as_30s)

            snapshot.fees_paid += fill.fee

        snapshot.maker_rebates = self._daily_rebates
        snapshot.liquidity_rewards = self._daily_rewards
        snapshot.total_trades = len(self._fills)
        snapshot.total_volume = total_volume

        snapshot.gross_pnl = (
            snapshot.spread_capture
            + snapshot.total_adverse_selection
            + snapshot.arb_pnl
            + snapshot.inventory_carry
        )

        snapshot.net_pnl = (
            snapshot.gross_pnl
            + snapshot.maker_rebates
            + snapshot.liquidity_rewards
            - snapshot.fees_paid
            - snapshot.slippage
            - snapshot.reject_cancel_costs
        )

        return snapshot

    def reset_daily(self) -> PnLSnapshot:
        """Reset for new day, returning final snapshot."""
        final = self.compute_snapshot("daily")
        self._fills.clear()
        self._daily_rebates = 0.0
        self._daily_rewards = 0.0
        self._daily_start_ts = time.time()
        self._reject_count = 0
        return final

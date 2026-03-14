"""Statistical edge validation — Paper 2 §5.

Tracks whether the bot's trading edge is statistically real
using SPRT (Sequential Probability Ratio Test) and rolling Sharpe.

Key insight from Paper 2: a 5% edge at even odds needs ~620 trades
to confirm. SPRT can reduce this by ~50% through adaptive early
stopping.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from pmm1.math.validation import (
    annualized_sharpe,
    brier_score,
    per_trade_sharpe,
    required_sample_size,
    rolling_sharpe,
    sprt_update,
)


@dataclass
class TradeOutcome:
    """A single resolved trade for edge tracking."""

    predicted_p: float
    market_p: float
    outcome: float  # 1.0 = YES resolved, 0.0 = NO resolved
    pnl: float
    timestamp: float = field(default_factory=time.time)
    side: str = ""
    condition_id: str = ""


class EdgeTracker:
    """Tracks edge validation metrics from Paper 2 §5.

    Usage:
        tracker = EdgeTracker()
        # After each resolved trade:
        tracker.record_trade(predicted_p=0.65, market_p=0.55,
                            outcome=1.0, pnl=0.10)
        # Query status:
        status = tracker.get_summary()
    """

    def __init__(
        self,
        min_trades: int = 50,
        target_edge: float = 0.05,
    ) -> None:
        self.min_trades = min_trades
        self.target_edge = target_edge
        self.trades: list[TradeOutcome] = []
        self.sprt_log_ratio: float = 0.0
        self.sprt_decision: str = "undecided"

    def record_trade(
        self,
        predicted_p: float,
        market_p: float,
        outcome: float,
        pnl: float,
        side: str = "",
        condition_id: str = "",
    ) -> None:
        """Record a resolved trade for edge tracking.

        Also updates the running SPRT test.
        """
        trade = TradeOutcome(
            predicted_p=predicted_p,
            market_p=market_p,
            outcome=outcome,
            pnl=pnl,
            side=side,
            condition_id=condition_id,
        )
        self.trades.append(trade)

        # Update SPRT if we have enough context
        if self.sprt_decision == "undecided":
            self.sprt_log_ratio, self.sprt_decision = sprt_update(
                self.sprt_log_ratio,
                outcome,
                p_true=predicted_p,
                p_null=market_p,
            )

    def get_rolling_sharpe(self, window: int = 100) -> float:
        """Annualized Sharpe from recent trades.

        Paper 2: SR_annual = SR_trade × √(N_trades/year).
        Context: S&P 500 ≈ 0.4, top quants ≈ 1.0-2.0.
        """
        if len(self.trades) < 2:
            return 0.0
        returns = [t.pnl for t in self.trades]
        sr = rolling_sharpe(returns, window)
        trades_per_year = min(len(self.trades), window) * (365 * 24 / max(1, self._hours_elapsed()))
        return annualized_sharpe(sr, int(min(trades_per_year, 10000)))

    def get_edge_confidence(self) -> float:
        """Returns [0, 1] confidence that edge is real.

        Used to modulate Kelly fraction dynamically:
            effective_kelly = kelly_fraction * edge_confidence

        Ramps from 0.1 (few trades) to 1.0 (SPRT confirmed).
        """
        n = len(self.trades)
        if n < self.min_trades:
            # Linear ramp from 0.1 to 0.5 over min_trades
            return 0.1 + 0.4 * (n / self.min_trades)

        if self.sprt_decision == "edge_confirmed":
            return 1.0
        elif self.sprt_decision == "no_edge":
            return 0.1  # Minimal sizing, don't stop completely

        # Undecided: use win rate signal
        wins = sum(1 for t in self.trades if t.pnl > 0)
        win_rate = wins / n
        # Confidence based on how far win rate exceeds 50%
        excess = max(0, win_rate - 0.5) * 2  # 0 at 50%, 1 at 100%
        return min(1.0, 0.5 + excess * 0.5)

    def get_brier_score(self) -> float:
        """Brier score of predictions vs outcomes."""
        if not self.trades:
            return 1.0
        probs = [t.predicted_p for t in self.trades]
        outcomes = [t.outcome for t in self.trades]
        return brier_score(probs, outcomes)

    def get_required_trades(self) -> int:
        """How many more trades needed for significance."""
        n_required = required_sample_size(
            self.target_edge, p_market=0.5,
        )
        return max(0, n_required - len(self.trades))

    def get_summary(self) -> dict[str, Any]:
        """Full diagnostics for runtime status / dashboard."""
        n = len(self.trades)
        total_pnl = sum(t.pnl for t in self.trades)
        wins = sum(1 for t in self.trades if t.pnl > 0)

        return {
            "total_trades": n,
            "total_pnl": round(total_pnl, 4),
            "win_rate": round(wins / n, 4) if n > 0 else 0.0,
            "brier_score": round(self.get_brier_score(), 4),
            "sprt_decision": self.sprt_decision,
            "sprt_log_ratio": round(self.sprt_log_ratio, 4),
            "edge_confidence": round(self.get_edge_confidence(), 4),
            "rolling_sharpe": round(
                self.get_rolling_sharpe(), 4,
            ),
            "trades_to_significance": self.get_required_trades(),
            "per_trade_sharpe": round(
                per_trade_sharpe(
                    self.target_edge, 0.5,
                ),
                4,
            ),
        }

    def _hours_elapsed(self) -> float:
        """Hours between first and last trade."""
        if len(self.trades) < 2:
            return 1.0
        return max(
            1.0,
            (self.trades[-1].timestamp - self.trades[0].timestamp) / 3600,
        )

"""Statistical edge validation — Paper 2 §5.

Tracks whether the bot's trading edge is statistically real
using SPRT (Sequential Probability Ratio Test) and rolling Sharpe.

Key insight from Paper 2: a 5% edge at even odds needs ~620 trades
to confirm. SPRT can reduce this by ~50% through adaptive early
stopping.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from pmm1.math.validation import (
    annualized_sharpe,
    brier_score,
    per_trade_sharpe,
    required_sample_size,
    rolling_sharpe,
    sprt_update_glr,
)

logger = structlog.get_logger(__name__)


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
        self._running_wins = 0
        self._running_total = 0
        self._running_market_p_sum: float = 0.0
        self._decision_trade_index = 0
        self._decision_history: list[tuple[str, int]] = []
        self.window_size = 200
        # ST-08: Exponential decay for adaptive windowing
        self._decay_lambda: float = 0.995  # Gives effective window of ~200 trades
        self._weighted_wins: float = 0.0
        self._weighted_total: float = 0.0

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

        # Track running stats for GLR
        self._running_total += 1
        self._running_market_p_sum += market_p
        if outcome > 0.5:
            self._running_wins += 1

        # Sliding window: reset SPRT after window_size trades past last decision
        if (
            self.sprt_decision != "undecided"
            and len(self.trades) - self._decision_trade_index >= self.window_size
        ):
            self._decision_history.append(
                (self.sprt_decision, self._decision_trade_index),
            )
            self.sprt_log_ratio = 0.0
            self.sprt_decision = "undecided"
            self._running_wins = 0
            self._running_total = 0
            self._running_market_p_sum = 0.0
            self._decision_trade_index = len(self.trades)

        # ST-08: Exponential decay for adaptive windowing
        win = 1.0 if outcome > 0.5 else 0.0
        self._weighted_wins = self._weighted_wins * self._decay_lambda + win
        self._weighted_total = self._weighted_total * self._decay_lambda + 1.0

        # Use GLR instead of fixed-alternative SPRT
        if self.sprt_decision == "undecided":
            p_null_avg = (
                self._running_market_p_sum / self._running_total
                if self._running_total > 0
                else 0.5
            )
            self.sprt_log_ratio, self.sprt_decision = sprt_update_glr(
                self.sprt_log_ratio,
                outcome,
                self._running_wins,
                self._running_total,
                p_null=p_null_avg,
            )
            if self.sprt_decision != "undecided":
                self._decision_trade_index = len(self.trades)

    def get_weighted_win_rate(self) -> float:
        """Win rate with exponential decay weighting (ST-08).

        More recent trades have higher weight. Effective window
        adapts: in stable environments it smoothly averages many trades;
        after a regime change, stale data decays away.
        """
        if self._weighted_total < 1.0:
            return 0.5
        return self._weighted_wins / self._weighted_total

    def get_bayesian_edge_confidence(self, min_edge: float = 0.0) -> float:
        """Bayesian edge confidence via Beta-Bernoulli (ST-03)."""
        from pmm1.math.validation import beta_sf

        wins = self._running_wins
        total = self._running_total
        losses = total - wins
        a = 1 + wins
        b = 1 + losses
        p_null = self._running_market_p_sum / max(1, total) if total > 0 else 0.5
        threshold = min(0.99, p_null + min_edge)
        return beta_sf(threshold, a, b)

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
            # Linear ramp from 0.3 to 0.5 over min_trades
            # Floor at 0.3 (was 0.1) to prevent cold-start deadlock
            return 0.3 + 0.2 * (n / self.min_trades)

        if self.sprt_decision == "edge_confirmed":
            return 1.0
        elif self.sprt_decision == "no_edge":
            return 0.1  # Minimal sizing, don't stop completely

        # Undecided: use weighted win rate signal (ST-08)
        win_rate = self.get_weighted_win_rate()  # Changed from counting all trades
        excess = max(0, win_rate - 0.5) * 2
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
            "decision_history": [
                {"decision": d, "at_trade": i}
                for d, i in self._decision_history[-5:]
            ],
        }

    def _hours_elapsed(self) -> float:
        """Hours between first and last trade."""
        if len(self.trades) < 2:
            return 1.0
        return max(
            1.0,
            (self.trades[-1].timestamp - self.trades[0].timestamp) / 3600,
        )

    def save(self, path: str) -> None:
        """Persist edge tracker state to disk.

        Follows the same pattern as ReasonerMemory._save() in
        pmm1/strategy/reasoner_memory.py.
        """
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            data = {
                "trades": [
                    {
                        "predicted_p": t.predicted_p,
                        "market_p": t.market_p,
                        "outcome": t.outcome,
                        "pnl": t.pnl,
                        "timestamp": t.timestamp,
                        "side": t.side,
                        "condition_id": t.condition_id,
                    }
                    for t in self.trades[-5000:]  # Keep recent
                ],
                "sprt_log_ratio": self.sprt_log_ratio,
                "sprt_decision": self.sprt_decision,
                "_running_wins": self._running_wins,
                "_running_total": self._running_total,
                "_running_market_p_sum": self._running_market_p_sum,
                "_decision_trade_index": self._decision_trade_index,
                "_decision_history": self._decision_history[-20:],
                "_decay_lambda": self._decay_lambda,
                "_weighted_wins": self._weighted_wins,
                "_weighted_total": self._weighted_total,
            }
            # Atomic write: write to tmp then rename
            tmp_path = path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(data, f)
            Path(tmp_path).replace(path)
            logger.info("edge_tracker_saved", path=path, trades=len(data["trades"]))
        except Exception as e:
            logger.warning("edge_tracker_save_failed", error=str(e))

    def load(self, path: str) -> None:
        """Load edge tracker state from disk with optional discount."""
        try:
            p = Path(path)
            if not p.exists():
                return
            with open(p) as f:
                data = json.load(f)
            self.trades = [
                TradeOutcome(**t) for t in data.get("trades", [])
            ]
            self.sprt_log_ratio = data.get("sprt_log_ratio", 0.0)
            self.sprt_decision = data.get("sprt_decision", "undecided")
            self._running_wins = data.get("_running_wins", 0)
            self._running_total = data.get("_running_total", 0)
            self._running_market_p_sum = data.get("_running_market_p_sum", 0.0)
            self._decision_trade_index = data.get("_decision_trade_index", 0)
            self._decision_history = [
                tuple(x) for x in data.get("_decision_history", [])
            ]
            self._decay_lambda = data.get("_decay_lambda", 0.995)
            self._weighted_wins = data.get("_weighted_wins", 0.0)
            self._weighted_total = data.get("_weighted_total", 0.0)
            logger.info("edge_tracker_loaded", trades=len(self.trades))
        except Exception as e:
            logger.warning("edge_tracker_load_failed", error=str(e))

    def get_running_total(self) -> int:
        """Expose running total for external use (e.g., FDR correction)."""
        return self._running_total

    def warm_start(
        self,
        prior_wins: int,
        prior_total: int,
        prior_log_ratio: float = 0.0,
        discount: float = 0.8,
    ) -> None:
        """Initialize from prior session data with discount.

        Discounts historical data to account for potential regime change
        between sessions. discount=0.8 means we trust 80% of history.
        """
        self._running_wins = int(prior_wins * discount)
        self._running_total = int(prior_total * discount)
        if self._running_total > 0:
            self._running_market_p_sum = self._running_total * 0.5  # Assume ~0.5 avg market_p
        self.sprt_log_ratio = prior_log_ratio * discount
        logger.info(
            "edge_tracker_warm_started",
            discounted_wins=self._running_wins,
            discounted_total=self._running_total,
            discount=discount,
        )


class MultiMarketEdgeController:
    """Applies Benjamini-Hochberg FDR correction across simultaneous markets.

    ST-02: Without correction, testing 20 markets at alpha=0.05 gives
    a 64% chance of at least one false positive. BH procedure at FDR=0.10
    controls the expected proportion of false discoveries.

    Requires ST-09 (glr_to_pvalue) from Phase 1.
    """

    def __init__(self, fdr_level: float = 0.10) -> None:
        self.fdr_level = fdr_level
        self._per_market_trackers: dict[str, EdgeTracker] = {}
        self._confirmed_markets: set[str] = set()

    def get_or_create_tracker(
        self, condition_id: str, min_trades: int = 50, target_edge: float = 0.05,
    ) -> EdgeTracker:
        """Get or create a per-market EdgeTracker."""
        if condition_id not in self._per_market_trackers:
            self._per_market_trackers[condition_id] = EdgeTracker(
                min_trades=min_trades, target_edge=target_edge,
            )
        return self._per_market_trackers[condition_id]

    @staticmethod
    def _compute_batch_glr(wins: int, total: int, p_null: float) -> float:
        """Recompute full-batch GLR = n * KL(p_mle || p_null).

        The tracker's sprt_log_ratio may be stale (frozen at the point SPRT
        first crossed a threshold). For FDR correction we need the GLR
        computed over the *entire* current window.
        """
        import math

        if total < 2:
            return 0.0
        p_mle = wins / total
        p0 = max(1e-10, min(1.0 - 1e-10, p_null))
        if abs(p_mle - p0) < 0.005:
            return 0.0
        p_mle = max(1e-10, min(1.0 - 1e-10, p_mle))
        kl = p_mle * math.log(p_mle / p0) + (1.0 - p_mle) * math.log(
            (1.0 - p_mle) / (1.0 - p0)
        )
        return total * kl

    def apply_fdr_correction(self) -> set[str]:
        """Apply BH procedure across all market-level SPRT decisions.

        Returns set of condition_ids with FDR-corrected edge confirmation.
        """
        from pmm1.math.validation import glr_to_pvalue

        # Collect p-values from all markets with enough data.
        # Recompute the batch GLR from running stats rather than using the
        # potentially stale tracker.sprt_log_ratio (which freezes when SPRT
        # first crosses its threshold).
        pvalues: list[tuple[str, float]] = []
        for cid, tracker in self._per_market_trackers.items():
            if tracker._running_total < tracker.min_trades:
                continue
            p_null_avg = (
                tracker._running_market_p_sum / tracker._running_total
                if tracker._running_total > 0
                else 0.5
            )
            glr = self._compute_batch_glr(
                tracker._running_wins, tracker._running_total, p_null_avg,
            )
            pval = glr_to_pvalue(glr)
            pvalues.append((cid, pval))

        if not pvalues:
            self._confirmed_markets = set()
            return self._confirmed_markets

        # Sort by p-value (ascending)
        pvalues.sort(key=lambda x: x[1])
        m = len(pvalues)

        # BH step-up procedure: find the largest k such that p_(k) <= k/m * q,
        # then reject all hypotheses i <= k
        max_k = 0
        for k, (cid, pval) in enumerate(pvalues, 1):
            threshold = k / m * self.fdr_level
            if pval <= threshold:
                max_k = k

        confirmed = {pvalues[i][0] for i in range(max_k)}
        self._confirmed_markets = confirmed
        return confirmed

    def is_edge_confirmed(self, condition_id: str) -> bool:
        """Check if a market has FDR-corrected edge confirmation."""
        return condition_id in self._confirmed_markets

    def get_edge_confidence(self, condition_id: str) -> float:
        """Get edge confidence, accounting for FDR correction.

        If a market's individual SPRT says edge_confirmed but FDR
        says otherwise, cap confidence at 0.5 (undecided level).
        """
        tracker = self._per_market_trackers.get(condition_id)
        if tracker is None:
            return 0.1

        raw_confidence = tracker.get_edge_confidence()

        if tracker.sprt_decision == "edge_confirmed" and not self.is_edge_confirmed(condition_id):
            # Individual test passed but FDR correction failed
            return min(raw_confidence, 0.5)

        return raw_confidence

    @property
    def confirmed_count(self) -> int:
        return len(self._confirmed_markets)

    @property
    def tracked_count(self) -> int:
        return len(self._per_market_trackers)

    def get_category_edge_summary(self) -> dict[str, dict]:
        """Per-category edge status (ST-11)."""
        total_trades = sum(t._running_total for t in self._per_market_trackers.values())
        total_wins = sum(t._running_wins for t in self._per_market_trackers.values())
        return {
            "all": {
                "markets": len(self._per_market_trackers),
                "total_trades": total_trades,
                "win_rate": round(total_wins / max(1, total_trades), 4),
                "confirmed": self.confirmed_count,
            },
        }

    def remove_market(self, condition_id: str) -> None:
        """Remove a market from tracking."""
        self._per_market_trackers.pop(condition_id, None)
        self._confirmed_markets.discard(condition_id)

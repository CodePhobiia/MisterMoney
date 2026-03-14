"""Per-market profitability tracking for market selection.

CL-02: The bot selects markets by volume/spread/staleness but ignores
actual historical profitability. This tracks EWMA of PnL per dollar
volume and feeds into market priority scoring.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class MarketProfitabilityTracker:
    """Tracks per-market profitability using EWMA.

    Usage:
        tracker = MarketProfitabilityTracker()
        tracker.record_fill("cid_123", pnl=0.05, volume=10.0)
        score = tracker.profitability_score("cid_123")  # [-2, +2]
    """

    def __init__(self, decay: float = 0.95, min_fills: int = 5) -> None:
        self.decay = decay
        self.min_fills = min_fills
        self._markets: dict[str, dict[str, float]] = {}
        # Each market: {ewma_pnl_per_vol, fill_count, total_volume, total_pnl, last_ts}

    def record_fill(
        self, condition_id: str, pnl: float, volume: float,
    ) -> None:
        """Record a fill's PnL for this market."""
        if volume <= 0:
            return

        pnl_per_vol = pnl / volume

        if condition_id not in self._markets:
            self._markets[condition_id] = {
                "ewma_pnl_per_vol": pnl_per_vol,
                "fill_count": 1,
                "total_volume": volume,
                "total_pnl": pnl,
                "last_ts": time.time(),
            }
            return

        m = self._markets[condition_id]
        m["ewma_pnl_per_vol"] = self.decay * m["ewma_pnl_per_vol"] + (1 - self.decay) * pnl_per_vol
        m["fill_count"] = m["fill_count"] + 1
        m["total_volume"] = m["total_volume"] + volume
        m["total_pnl"] = m["total_pnl"] + pnl
        m["last_ts"] = time.time()

    def profitability_score(self, condition_id: str) -> float:
        """Normalized profitability score in [-2, +2].

        Markets with < min_fills return 0 (neutral).
        Score is the EWMA PnL/vol normalized by the cross-market std.
        """
        m = self._markets.get(condition_id)
        if m is None or m["fill_count"] < self.min_fills:
            return 0.0

        # Get all EWMA values for normalization
        ewmas = [
            v["ewma_pnl_per_vol"]
            for v in self._markets.values()
            if v["fill_count"] >= self.min_fills
        ]
        if len(ewmas) < 2:
            # Can't normalize with < 2 markets
            return max(-2.0, min(2.0, m["ewma_pnl_per_vol"] * 100))

        mean_ewma = sum(ewmas) / len(ewmas)
        var_ewma = sum((e - mean_ewma) ** 2 for e in ewmas) / len(ewmas)
        std_ewma = max(0.0001, var_ewma ** 0.5)

        z = (m["ewma_pnl_per_vol"] - mean_ewma) / std_ewma
        return max(-2.0, min(2.0, z))

    def get_all_scores(self) -> dict[str, float]:
        """Get profitability scores for all tracked markets."""
        return {cid: self.profitability_score(cid) for cid in self._markets}

    def save(self, path: str) -> None:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._markets, f)
            Path(tmp).replace(path)
        except Exception as e:
            logger.warning("market_profitability_save_failed", error=str(e))

    def load(self, path: str) -> None:
        try:
            p = Path(path)
            if not p.exists():
                return
            with open(p) as f:
                self._markets = json.load(f)
            logger.info("market_profitability_loaded", markets=len(self._markets))
        except Exception as e:
            logger.warning("market_profitability_load_failed", error=str(e))

    def get_status(self) -> dict[str, Any]:
        active = [m for m in self._markets.values() if m["fill_count"] >= self.min_fills]
        return {
            "tracked_markets": len(self._markets),
            "active_markets": len(active),
            "total_pnl": sum(m["total_pnl"] for m in self._markets.values()),
        }

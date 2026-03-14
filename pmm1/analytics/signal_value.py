"""Measures the marginal value-add of LLM fair value signals.

CL-06: The bot spends ~$50/day on LLM calls but never measures
whether the signal actually improves PnL vs just using market mid.
This tracks paired observations to compute IC, IR, and ROI.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class PairedObservation:
    """A single fill with LLM and counterfactual data."""

    blended_fv: float
    market_mid: float
    fill_price: float
    side: str
    pnl: float
    llm_used: bool
    timestamp: float = field(default_factory=time.time)
    mid_5s_after: float | None = None


class SignalValueTracker:
    """Tracks LLM signal value-add via paired comparison.

    For each fill, records what happened WITH the LLM signal vs
    what WOULD have happened using market mid only.

    Key metrics:
    - IC: Spearman correlation of LLM-deviation with subsequent moves
    - Dollar value-add: cumulative PnL improvement from LLM
    - ROI: value-add / LLM cost
    """

    def __init__(self, window: int = 200) -> None:
        self.window = window
        self._observations: list[PairedObservation] = []
        self._daily_cost_usd: float = 0.0
        self._daily_value_add: float = 0.0
        self._day_start: float = time.time()

    def record_fill(
        self,
        blended_fv: float,
        market_mid: float,
        fill_price: float,
        side: str,
        pnl: float,
        llm_used: bool,
    ) -> None:
        """Record a fill with its LLM context."""
        obs = PairedObservation(
            blended_fv=blended_fv,
            market_mid=market_mid,
            fill_price=fill_price,
            side=side,
            pnl=pnl,
            llm_used=llm_used,
        )
        self._observations.append(obs)

        # Keep rolling window
        if len(self._observations) > self.window * 2:
            self._observations = self._observations[-self.window:]

    def update_post_fill(self, index: int, mid_5s_after: float) -> None:
        """Update a fill with its 5-second-after midpoint."""
        if 0 <= index < len(self._observations):
            self._observations[index].mid_5s_after = mid_5s_after

    def set_daily_cost(self, cost_usd: float) -> None:
        """Update daily LLM cost."""
        self._daily_cost_usd = cost_usd

    def compute_ic(self) -> float:
        """Information Coefficient: rank correlation of LLM signal with outcomes.

        IC = Spearman(llm_deviation_from_market, subsequent_price_move)
        """
        valid = [
            o for o in self._observations[-self.window:]
            if o.llm_used and o.mid_5s_after is not None
        ]
        if len(valid) < 10:
            return 0.0

        # LLM deviation: how much the blend moved away from market
        deviations = [o.blended_fv - o.market_mid for o in valid]
        # Outcome: did the market move in the LLM's direction?
        outcomes = [o.mid_5s_after - o.market_mid for o in valid]

        return self._spearman(deviations, outcomes)

    def compute_value_add(self) -> float:
        """Dollar value-add from LLM signals.

        Compares actual spread capture to counterfactual
        (what if FV = market mid).
        """
        total_add = 0.0
        for o in self._observations[-self.window:]:
            if not o.llm_used:
                continue
            # Actual edge from LLM
            if o.side == "BUY":
                actual_edge = o.blended_fv - o.fill_price
                counterfactual_edge = o.market_mid - o.fill_price
            else:
                actual_edge = o.fill_price - o.blended_fv
                counterfactual_edge = o.fill_price - o.market_mid
            total_add += actual_edge - counterfactual_edge
        return total_add

    def compute_roi(self) -> float | None:
        """ROI = value_add / cost. None if no cost data."""
        if self._daily_cost_usd <= 0:
            return None
        value = self.compute_value_add()
        return value / self._daily_cost_usd

    def get_status(self) -> dict[str, Any]:
        ic = self.compute_ic()
        value_add = self.compute_value_add()
        roi = self.compute_roi()
        llm_fills = sum(1 for o in self._observations if o.llm_used)
        return {
            "total_observations": len(self._observations),
            "llm_fills": llm_fills,
            "ic": round(ic, 4),
            "value_add_usd": round(value_add, 4),
            "daily_cost_usd": round(self._daily_cost_usd, 2),
            "roi": round(roi, 2) if roi is not None else None,
            "is_worth_it": roi is None or roi > 1.0,
        }

    def save(self, path: str) -> None:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            data = [
                {
                    "blended_fv": o.blended_fv,
                    "market_mid": o.market_mid,
                    "fill_price": o.fill_price,
                    "side": o.side,
                    "pnl": o.pnl,
                    "llm_used": o.llm_used,
                    "timestamp": o.timestamp,
                    "mid_5s_after": o.mid_5s_after,
                }
                for o in self._observations[-self.window:]
            ]
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            Path(tmp).replace(path)
        except Exception as e:
            logger.warning("signal_value_save_failed", error=str(e))

    def load(self, path: str) -> None:
        try:
            p = Path(path)
            if not p.exists():
                return
            with open(p) as f:
                data = json.load(f)
            self._observations = [
                PairedObservation(**d) for d in data
            ]
            logger.info("signal_value_loaded", observations=len(self._observations))
        except Exception as e:
            logger.warning("signal_value_load_failed", error=str(e))

    @staticmethod
    def _spearman(x: list[float], y: list[float]) -> float:
        """Spearman rank correlation (no scipy dependency)."""
        n = len(x)
        if n < 3:
            return 0.0

        def _rank(values: list[float]) -> list[float]:
            indexed = sorted(enumerate(values), key=lambda t: t[1])
            ranks = [0.0] * n
            for rank, (idx, _) in enumerate(indexed):
                ranks[idx] = float(rank)
            return ranks

        rx = _rank(x)
        ry = _rank(y)

        d_sq = sum((a - b) ** 2 for a, b in zip(rx, ry))
        return 1 - 6 * d_sq / (n * (n ** 2 - 1))

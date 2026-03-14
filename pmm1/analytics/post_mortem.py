"""Trade post-mortem -- classifies WHY trades lost money.

CL-05: When a trade loses, the bot never asks: Was it LLM error?
Adverse selection? Bad timing? This classifies each losing fill
and tracks aggregates for learning.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class LossCategory(StrEnum):
    ADVERSE_SELECTION = "adverse_selection"
    LLM_ERROR = "llm_error"
    CARRY_LOSS = "carry_loss"
    BAD_TIMING = "bad_timing"
    SIZING_ERROR = "sizing_error"
    UNKNOWN = "unknown"
    PROFITABLE = "profitable"


class TradePostMortem:
    """Classifies losing trades and tracks loss attribution.

    Called after each fill's 30s markout is available.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {c.value: 0 for c in LossCategory}
        self._amounts: dict[str, float] = {c.value: 0.0 for c in LossCategory}
        self._total_classified: int = 0

    def classify_fill(
        self,
        pnl: float,
        spread_capture: float,
        adverse_selection_5s: float,
        fair_value_error: float | None = None,
        hold_time_hours: float = 0.0,
    ) -> LossCategory:
        """Classify a fill into a loss category.

        Args:
            pnl: Realized PnL of this fill
            spread_capture: Spread component (mid_at_fill - fill_price for BUY)
            adverse_selection_5s: 5s AS component (negative = we lost)
            fair_value_error: |predicted_fv - actual_outcome| if resolved
            hold_time_hours: How long position was held
        """
        self._total_classified += 1

        if pnl >= 0:
            category = LossCategory.PROFITABLE
        elif adverse_selection_5s < -abs(spread_capture) * 0.5:
            # AS cost exceeds half of spread capture
            category = LossCategory.ADVERSE_SELECTION
        elif fair_value_error is not None and fair_value_error > 0.15:
            # FV was off by > 15 percentage points
            category = LossCategory.LLM_ERROR
        elif hold_time_hours > 4.0:
            # Held too long -- position decayed
            category = LossCategory.CARRY_LOSS
        elif abs(pnl) < 0.01 and spread_capture > 0:
            # Spread was captured but timing was off
            category = LossCategory.BAD_TIMING
        else:
            category = LossCategory.UNKNOWN

        self._counts[category.value] = self._counts.get(category.value, 0) + 1
        self._amounts[category.value] = self._amounts.get(category.value, 0.0) + min(0, pnl)

        return category

    def format_for_prompt(self) -> str:
        """Format loss attribution for LLM prompt injection."""
        total_losses = sum(
            self._counts[c.value] for c in LossCategory
            if c != LossCategory.PROFITABLE
        )
        if total_losses < 10:
            return ""

        lines = [f"LOSS ATTRIBUTION (last {self._total_classified} trades):"]
        for cat in LossCategory:
            if cat == LossCategory.PROFITABLE:
                continue
            count = self._counts.get(cat.value, 0)
            if count > 0:
                pct = count / total_losses * 100
                amt = self._amounts.get(cat.value, 0.0)
                lines.append(f"  {cat.value}: {count} ({pct:.0f}%) totaling ${amt:.2f}")
        return "\n".join(lines)

    def get_summary(self) -> dict[str, Any]:
        return {
            "total_classified": self._total_classified,
            "counts": dict(self._counts),
            "amounts": {k: round(v, 4) for k, v in self._amounts.items()},
        }

    def save(self, path: str) -> None:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            data = {
                "counts": self._counts,
                "amounts": self._amounts,
                "total": self._total_classified,
            }
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            Path(tmp).replace(path)
        except Exception as e:
            logger.warning("post_mortem_save_failed", error=str(e))

    def load(self, path: str) -> None:
        try:
            p = Path(path)
            if not p.exists():
                return
            with open(p) as f:
                data = json.load(f)
            self._counts = data.get("counts", self._counts)
            self._amounts = data.get("amounts", self._amounts)
            self._total_classified = data.get("total", 0)
        except Exception as e:
            logger.warning("post_mortem_load_failed", error=str(e))

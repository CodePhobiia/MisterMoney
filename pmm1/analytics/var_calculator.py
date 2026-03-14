"""Position-level Value at Risk for prediction market portfolios (KP-08)."""
from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def position_var_95(size: float, price: float) -> float:
    """95% VaR for a single binary position."""
    return abs(size) * max(price, 1.0 - price)


def portfolio_var_95(positions: list[dict[str, float]], rho: float = 0.05) -> float:
    """Portfolio VaR with uniform correlation."""
    if not positions:
        return 0.0
    vars_ = [position_var_95(p["size"], p["price"]) for p in positions]
    n = len(vars_)
    total_var = sum(v**2 for v in vars_)
    for i in range(n):
        for j in range(i + 1, n):
            total_var += 2 * rho * vars_[i] * vars_[j]
    return max(0, total_var) ** 0.5


class VaRReporter:
    def compute_report(
        self,
        positions: list[dict[str, Any]],
        theme_rho: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        if not positions:
            return {"total_var_95": 0.0, "position_count": 0}
        pos_data = [
            {"size": p.get("size", 0), "price": p.get("price", 0.5)}
            for p in positions
        ]
        total_var = portfolio_var_95(pos_data, rho=0.05)
        return {
            "total_var_95": round(total_var, 4),
            "position_count": len(positions),
            "individual_vars": [
                round(position_var_95(p["size"], p["price"]), 4) for p in pos_data
            ],
        }

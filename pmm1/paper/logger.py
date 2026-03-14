"""Paper trade logger — writes all paper trading activity to JSONL files.

Output files in data/paper/:
  - paper_trades.jsonl  — every simulated fill
  - paper_pnl.jsonl     — periodic PnL snapshots
  - paper_quotes.jsonl  — every quote intent generated
  - paper_arbs.jsonl    — every arb opportunity detected
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class PaperLogger:
    """Writes paper trading activity to JSONL files."""

    def __init__(self, output_dir: str = "data/paper") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._trades_path = self.output_dir / "paper_trades.jsonl"
        self._pnl_path = self.output_dir / "paper_pnl.jsonl"
        self._quotes_path = self.output_dir / "paper_quotes.jsonl"
        self._arbs_path = self.output_dir / "paper_arbs.jsonl"

        # Counters
        self.trades_logged = 0
        self.pnl_logged = 0
        self.quotes_logged = 0
        self.arbs_logged = 0

        logger.info("paper_logger_initialized", output_dir=str(self.output_dir))

    def _write_jsonl(self, path: Path, data: dict[str, Any]) -> None:
        """Append a JSON line to a file."""
        try:
            with open(path, "a") as f:
                f.write(json.dumps(data, default=str) + "\n")
        except Exception as e:
            logger.warning("paper_log_write_failed", path=str(path), error=str(e))

    def log_fill(self, fill_data: dict[str, Any]) -> None:
        """Log a simulated fill."""
        fill_data["log_type"] = "fill"
        fill_data["log_ts"] = time.time()
        self._write_jsonl(self._trades_path, fill_data)
        self.trades_logged += 1

    def log_pnl(self, pnl_data: dict[str, Any]) -> None:
        """Log a PnL snapshot."""
        pnl_data["log_type"] = "pnl_snapshot"
        pnl_data["log_ts"] = time.time()
        self._write_jsonl(self._pnl_path, pnl_data)
        self.pnl_logged += 1

    def log_quote(
        self,
        token_id: str,
        condition_id: str,
        bid_price: float | None,
        bid_size: float | None,
        ask_price: float | None,
        ask_size: float | None,
        reservation_price: float,
        fair_value: float,
        half_spread: float,
        inventory: float,
        strategy: str = "mm",
    ) -> None:
        """Log a quote intent."""
        data = {
            "log_type": "quote",
            "log_ts": time.time(),
            "token_id": token_id[:16],
            "condition_id": condition_id[:16],
            "bid_price": bid_price,
            "bid_size": bid_size,
            "ask_price": ask_price,
            "ask_size": ask_size,
            "reservation_price": round(reservation_price, 6),
            "fair_value": round(fair_value, 6),
            "half_spread": round(half_spread, 6),
            "inventory": round(inventory, 2),
            "strategy": strategy,
        }
        self._write_jsonl(self._quotes_path, data)
        self.quotes_logged += 1

    def log_arb(
        self,
        arb_type: str,
        condition_id: str = "",
        event_id: str = "",
        edge: float = 0.0,
        orders: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Log an arb opportunity detection."""
        data = {
            "log_type": "arb",
            "log_ts": time.time(),
            "arb_type": arb_type,
            "condition_id": condition_id[:16] if condition_id else "",
            "event_id": event_id[:16] if event_id else "",
            "edge": round(edge, 6),
            "num_orders": len(orders) if orders else 0,
            "details": details or {},
        }
        self._write_jsonl(self._arbs_path, data)
        self.arbs_logged += 1

    def get_stats(self) -> dict[str, int]:
        """Get logging stats."""
        return {
            "trades_logged": self.trades_logged,
            "pnl_logged": self.pnl_logged,
            "quotes_logged": self.quotes_logged,
            "arbs_logged": self.arbs_logged,
        }

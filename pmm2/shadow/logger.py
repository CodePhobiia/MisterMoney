"""Shadow execution logger — records PMM-2's decisions without executing them.

Logs every allocation cycle's intended mutations to a rolling JSONL file.
Each cycle log includes:
- V1 actual state (markets, orders, positions)
- PMM-2 intended state (target quotes, mutations)
- Comparison metrics (divergence, EV delta, etc.)

Log files: data/shadow/shadow_YYYY-MM-DD.jsonl (one per day)
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class ShadowLogger:
    """Logs PMM-2's shadow decisions for counterfactual analysis.

    Records every allocation cycle's intended mutations without executing.
    Compares V1 actual vs PMM-2 recommended.
    """

    def __init__(self, db: Any, log_dir: str = "data/shadow") -> None:
        """Initialize shadow logger.

        Args:
            db: Database instance (for potential future persistence)
            log_dir: Directory for shadow log files
        """
        self.db = db
        self.log_dir = log_dir

        # Create log directory
        os.makedirs(log_dir, exist_ok=True)

        # Rolling JSONL file for shadow decisions
        self.log_file: str = ""
        self._rotate_log()

        logger.info("shadow_logger_initialized", log_dir=log_dir)

    def _rotate_log(self) -> None:
        """Create new log file for today."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        self.log_file = os.path.join(self.log_dir, f"shadow_{today}.jsonl")

        logger.info("shadow_log_rotated", log_file=self.log_file)

    def log_allocation_cycle(self, cycle_data: dict[str, Any]) -> None:
        """Log a complete allocation cycle.

        cycle_data should include:
        - timestamp: ISO string
        - v1_markets: list of condition_ids V1 is currently quoting
        - pmm2_markets: list of condition_ids PMM-2 would quote
        - v1_orders: current V1 live orders (summary)
        - pmm2_mutations: what PMM-2 would do (add/cancel/amend)
        - pmm2_plan: the target quote plan
        - ev_breakdown: per-market EV estimates
        - allocator_output: funded bundles
        - comparison: counterfactual comparison metrics

        Args:
            cycle_data: dict with cycle details
        """
        # Check if we need to rotate log file
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        expected_file = os.path.join(self.log_dir, f"shadow_{today}.jsonl")
        if self.log_file != expected_file:
            self._rotate_log()

        # Add timestamp if not present
        if "timestamp" not in cycle_data:
            cycle_data["timestamp"] = datetime.now(UTC).isoformat()

        # Write to JSONL file (compact single line per entry)
        try:
            with open(self.log_file, "a") as f:
                # Compact JSON, one line per entry (JSONL format)
                json_str = json.dumps(cycle_data, sort_keys=True)
                # Write with newline separator
                f.write(json_str + "\n")

            logger.debug(
                "shadow_cycle_logged",
                timestamp=cycle_data.get("timestamp"),
                v1_markets=len(cycle_data.get("v1_markets", [])),
                pmm2_markets=len(cycle_data.get("pmm2_markets", [])),
                mutations=len(cycle_data.get("pmm2_mutations", [])),
            )

        except Exception as e:
            logger.error("shadow_log_write_failed", error=str(e), exc_info=True)

    async def persist_allocation_cycle(self, cycle_data: dict[str, Any]) -> None:
        """Persist the cycle to SQLite so readiness decisions are auditable."""

        if self.db is None or not hasattr(self.db, "execute"):
            return

        comparison = cycle_data.get("comparison", {})
        summary = comparison.get("summary", cycle_data.get("summary", {}))
        diagnostics = comparison.get("gate_diagnostics", cycle_data.get("gate_diagnostics", {}))
        gates = diagnostics.get("gates", {})

        await self.db.execute(
            """
            INSERT OR REPLACE INTO shadow_cycle (
                ts, cycle_num, ready_for_live,
                window_cycles, ev_sample_count, reward_sample_count, churn_sample_count,
                gate_ev_positive, gate_reward_capture, gate_churn, gate_sample_size,
                v1_market_count, pmm2_market_count, market_overlap_pct, overlap_quote_distance_bps,
                v1_total_ev_usdc, pmm2_total_ev_usdc, ev_delta_usdc,
                v1_reward_market_count, pmm2_reward_market_count, reward_market_delta,
                v1_reward_ev_usdc, pmm2_reward_ev_usdc, reward_ev_delta_usdc,
                v1_cancel_rate_per_order_min,
                pmm2_cancel_rate_per_order_min,
                churn_delta_per_order_min,
                gate_blockers_json, gate_diagnostics_json, comparison_json, summary_json,
                v1_state_json, pmm2_plan_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                cycle_data.get("timestamp"),
                int(comparison.get("cycle_num", 0) or 0),
                int(bool(summary.get("ready_for_live", False))),
                int(diagnostics.get("window_cycles", 0) or 0),
                int(diagnostics.get("ev_sample_count", 0) or 0),
                int(diagnostics.get("reward_sample_count", 0) or 0),
                int(diagnostics.get("churn_sample_count", 0) or 0),
                int(bool(gates.get("gate_ev_positive", {}).get("pass", False))),
                int(bool(gates.get("gate_reward_capture", {}).get("pass", False))),
                int(bool(gates.get("gate_churn", {}).get("pass", False))),
                int(bool(gates.get("gate_sample_size", {}).get("pass", False))),
                len(cycle_data.get("v1_markets", [])),
                len(cycle_data.get("pmm2_markets", [])),
                float(comparison.get("market_overlap_pct", 0.0) or 0.0),
                float(comparison.get("overlap_quote_distance_bps", 0.0) or 0.0),
                float(comparison.get("v1_total_ev_usdc", 0.0) or 0.0),
                float(comparison.get("pmm2_total_ev_usdc", 0.0) or 0.0),
                float(comparison.get("ev_delta_usdc", 0.0) or 0.0),
                int(comparison.get("v1_reward_market_count", 0) or 0),
                int(comparison.get("pmm2_reward_market_count", 0) or 0),
                float(comparison.get("reward_market_delta", 0.0) or 0.0),
                float(comparison.get("v1_reward_ev_usdc", 0.0) or 0.0),
                float(comparison.get("pmm2_reward_ev_usdc", 0.0) or 0.0),
                float(comparison.get("reward_ev_delta_usdc", 0.0) or 0.0),
                float(comparison.get("v1_cancel_rate_per_order_min", 0.0) or 0.0),
                float(comparison.get("pmm2_cancel_rate_per_order_min", 0.0) or 0.0),
                float(comparison.get("churn_delta_per_order_min", 0.0) or 0.0),
                json.dumps(diagnostics.get("blocking_gates", []), sort_keys=True),
                json.dumps(diagnostics, sort_keys=True),
                json.dumps(comparison, sort_keys=True),
                json.dumps(summary, sort_keys=True),
                json.dumps(cycle_data.get("v1_state", {}), sort_keys=True),
                json.dumps(cycle_data.get("pmm2_plan", {}), sort_keys=True),
            ),
        )

    def log_divergence(self, divergence_type: str, details: dict[str, Any]) -> None:
        """Log a specific divergence between V1 and PMM-2.

        Types:
        - market_selection: different set of markets
        - order_pricing: different prices for same market
        - order_sizing: different sizes for same market
        - market_entry: PMM-2 wants to enter, V1 doesn't
        - market_exit: PMM-2 wants to exit, V1 doesn't
        - scoring_difference: EV estimates diverge significantly

        Args:
            divergence_type: type of divergence (see above)
            details: additional context about the divergence
        """
        divergence_entry = {
            "type": "divergence",
            "timestamp": datetime.now(UTC).isoformat(),
            "divergence_type": divergence_type,
            "details": details,
        }

        try:
            with open(self.log_file, "a") as f:
                json_str = json.dumps(divergence_entry, sort_keys=True)
                f.write(json_str + "\n")

            logger.info(
                "shadow_divergence_logged",
                divergence_type=divergence_type,
                **details,
            )

        except Exception as e:
            logger.error("shadow_divergence_log_failed", error=str(e), exc_info=True)

    def get_log_path(self, date: str | None = None) -> str:
        """Get path to shadow log file for a specific date.

        Args:
            date: date string (YYYY-MM-DD), or None for today

        Returns:
            Full path to log file
        """
        if date is None:
            date = datetime.now(UTC).strftime("%Y-%m-%d")

        return os.path.join(self.log_dir, f"shadow_{date}.jsonl")

    def read_cycles(self, date: str | None = None) -> list[dict[str, Any]]:
        """Read all allocation cycles from a log file.

        Args:
            date: date string (YYYY-MM-DD), or None for today

        Returns:
            List of cycle dictionaries
        """
        log_path = self.get_log_path(date)

        if not os.path.exists(log_path):
            logger.warning("shadow_log_not_found", path=log_path)
            return []

        cycles = []
        try:
            with open(log_path) as f:
                for line in f:
                    if line.strip():
                        cycle = json.loads(line)
                        # Only include full cycles, not divergence-only entries
                        if cycle.get("type") != "divergence":
                            cycles.append(cycle)

            logger.info(
                "shadow_cycles_loaded",
                date=date or "today",
                count=len(cycles),
            )

        except Exception as e:
            logger.error("shadow_log_read_failed", error=str(e), exc_info=True)

        return cycles

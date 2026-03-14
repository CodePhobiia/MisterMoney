"""
V3 Shadow Logger
Logs V3 shadow signals to JSONL files for analysis
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from v3.evidence.entities import FairValueSignal

log = structlog.get_logger()


class ShadowLogger:
    """Logs V3 shadow signals to JSONL files for analysis"""

    def __init__(self, log_dir: str = "data/v3/shadow"):
        """
        Initialize shadow logger

        Args:
            log_dir: Directory for shadow log files (JSONL)
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Current log files
        self._signal_log: Path | None = None
        self._error_log: Path | None = None

        log.info("shadow_logger_initialized", log_dir=str(self.log_dir))

    def _get_signal_log_path(self) -> Path:
        """Get current signal log path with daily rotation"""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return self.log_dir / f"shadow_{today}.jsonl"

    def _get_error_log_path(self) -> Path:
        """Get current error log path with daily rotation"""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return self.log_dir / f"errors_{today}.jsonl"

    async def log_signal(
        self,
        signal: FairValueSignal,
        market_meta: dict,
        v1_fair_value: float,
        latency_ms: float,
        token_usage: dict | None = None
    ) -> None:
        """
        Log V3 shadow signal with counterfactual comparison to V1

        Args:
            signal: V3 FairValueSignal generated
            market_meta: Market metadata (question, volume, spread, mid)
            v1_fair_value: V1's fair value (book midpoint)
            latency_ms: Route execution latency in milliseconds
            token_usage: Optional token usage stats per model
        """
        log_path = self._get_signal_log_path()

        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "condition_id": signal.condition_id,

            # V3 signal
            "v3_signal": {
                "p_calibrated": signal.p_calibrated,
                "p_low": signal.p_low,
                "p_high": signal.p_high,
                "uncertainty": signal.uncertainty,
                "route": signal.route,
                "evidence_ids": signal.evidence_ids,
                "counterevidence_ids": signal.counterevidence_ids,
                "models_used": signal.models_used,
            },

            # V1 counterfactual
            "v1_fair_value": v1_fair_value,

            # Market context
            "market": {
                "question": market_meta.get("question"),
                "volume_24h": market_meta.get("volume_24h"),
                "current_mid": market_meta.get("current_mid"),
                "spread_cents": market_meta.get("spread_cents"),
            },

            # Performance metrics
            "latency_ms": latency_ms,
            "token_usage": token_usage or {},
        }

        try:
            with open(log_path, 'a') as f:
                f.write(json.dumps(entry) + '\n')

            log.debug("shadow_signal_logged",
                     condition_id=signal.condition_id,
                     route=signal.route,
                     latency_ms=latency_ms)
        except Exception as e:
            log.error("shadow_log_write_failed",
                     error=str(e),
                     path=str(log_path))

    async def log_error(
        self,
        condition_id: str,
        route: str,
        error: str,
        market_meta: dict | None = None
    ) -> None:
        """
        Log shadow evaluation error

        Args:
            condition_id: Market condition ID
            route: Route that failed
            error: Error message
            market_meta: Optional market metadata
        """
        log_path = self._get_error_log_path()

        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "condition_id": condition_id,
            "route": route,
            "error": error,
            "market": market_meta or {},
        }

        try:
            with open(log_path, 'a') as f:
                f.write(json.dumps(entry) + '\n')

            log.warning("shadow_error_logged",
                       condition_id=condition_id,
                       route=route,
                       error=error)
        except Exception as e:
            log.error("error_log_write_failed",
                     error=str(e),
                     path=str(log_path))

    def rotate_log(self) -> None:
        """
        Daily rotation: shadow_YYYY-MM-DD.jsonl

        This is called automatically when log path changes (date change).
        Old logs remain on disk for historical analysis.
        """
        # Rotation happens automatically via _get_signal_log_path()
        # which embeds the date in the filename
        log.info("shadow_log_rotated",
                date=datetime.utcnow().strftime("%Y-%m-%d"))

    async def get_daily_summary(self, date: str | None = None) -> dict[str, Any]:
        """
        Get summary statistics for a specific day

        Args:
            date: Date string (YYYY-MM-DD), defaults to today

        Returns:
            Summary dict with counts, routes, avg latency, etc.
        """
        if date is None:
            date = datetime.utcnow().strftime("%Y-%m-%d")

        log_path = self.log_dir / f"shadow_{date}.jsonl"

        if not log_path.exists():
            return {
                "date": date,
                "markets_evaluated": 0,
                "signals_generated": 0,
                "errors": 0,
            }

        # Parse log file
        route_counts = {}
        total_latency = 0.0
        signal_count = 0

        try:
            with open(log_path) as f:
                for line in f:
                    entry = json.loads(line)
                    route = entry.get("v3_signal", {}).get("route")

                    if route:
                        route_counts[route] = route_counts.get(route, 0) + 1
                        total_latency += entry.get("latency_ms", 0)
                        signal_count += 1
        except Exception as e:
            log.error("failed_to_read_daily_summary",
                     date=date,
                     error=str(e))

        # Count errors
        error_path = self.log_dir / f"errors_{date}.jsonl"
        error_count = 0

        if error_path.exists():
            try:
                with open(error_path) as f:
                    error_count = sum(1 for _ in f)
            except Exception as e:
                log.error("failed_to_count_errors",
                         date=date,
                         error=str(e))

        return {
            "date": date,
            "markets_evaluated": signal_count,
            "signals_generated": signal_count,
            "errors": error_count,
            "route_breakdown": route_counts,
            "avg_latency_ms": total_latency / signal_count if signal_count > 0 else 0,
        }

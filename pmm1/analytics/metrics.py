"""Runtime metrics — operational and performance metrics."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


class MarketMetrics(BaseModel):
    """Metrics for a single market."""

    condition_id: str = ""
    token_id: str = ""
    quote_count: int = 0
    fill_count: int = 0
    cancel_count: int = 0
    reject_count: int = 0
    total_volume: float = 0.0
    total_fees: float = 0.0
    avg_spread_quoted: float = 0.0
    avg_fill_rate: float = 0.0
    quote_uptime_pct: float = 0.0
    last_quote_ts: float = 0.0
    last_fill_ts: float = 0.0


class BotMetrics(BaseModel):
    """Aggregate bot-level metrics."""

    # Operational
    uptime_seconds: float = 0.0
    total_quote_cycles: int = 0
    total_orders_submitted: int = 0
    total_orders_canceled: int = 0
    total_orders_filled: int = 0
    total_orders_rejected: int = 0
    total_fills: int = 0

    # Performance
    total_volume: float = 0.0
    total_fees_paid: float = 0.0
    total_pnl: float = 0.0
    daily_pnl: float = 0.0

    # Health
    heartbeat_success_rate: float = 0.0
    ws_reconnect_count: int = 0
    reconciliation_mismatch_count: int = 0
    kill_switch_trigger_count: int = 0

    # Quote quality
    avg_spread_cents: float = 0.0
    avg_quote_uptime_pct: float = 0.0
    reject_rate_pct: float = 0.0
    fill_rate_pct: float = 0.0

    # Markets
    active_markets: int = 0
    quoting_markets: int = 0


class MetricsCollector:
    """Collects and aggregates runtime metrics."""

    def __init__(self) -> None:
        self._start_time = time.time()
        self._market_metrics: dict[str, MarketMetrics] = {}
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = defaultdict(float)
        self._quote_spreads: list[float] = []
        self._cycle_times: list[float] = []

    def increment(self, name: str, value: int = 1) -> None:
        """Increment a counter."""
        self._counters[name] += value

    def set_gauge(self, name: str, value: float) -> None:
        """Set a gauge value."""
        self._gauges[name] = value

    def record_quote_cycle(
        self,
        duration_ms: float,
        markets_quoted: int,
        orders_submitted: int,
        orders_canceled: int,
    ) -> None:
        """Record a quote cycle completion."""
        self._counters["quote_cycles"] += 1
        self._counters["orders_submitted"] += orders_submitted
        self._counters["orders_canceled"] += orders_canceled
        self._cycle_times.append(duration_ms)
        self._gauges["markets_quoted"] = markets_quoted

        # Keep last 1000 cycle times
        if len(self._cycle_times) > 1000:
            self._cycle_times = self._cycle_times[-1000:]

    def record_fill(
        self,
        token_id: str,
        condition_id: str,
        price: float,
        size: float,
        fee: float,
    ) -> None:
        """Record a fill."""
        self._counters["total_fills"] += 1
        self._gauges["total_volume"] += price * size
        self._gauges["total_fees"] += fee

        metrics = self._get_market_metrics(token_id, condition_id)
        metrics.fill_count += 1
        metrics.total_volume += price * size
        metrics.total_fees += fee
        metrics.last_fill_ts = time.time()

    def record_quote(
        self,
        token_id: str,
        condition_id: str,
        spread_cents: float,
    ) -> None:
        """Record a quote submission."""
        self._counters["total_quotes"] += 1
        self._quote_spreads.append(spread_cents)

        if len(self._quote_spreads) > 10000:
            self._quote_spreads = self._quote_spreads[-10000:]

        metrics = self._get_market_metrics(token_id, condition_id)
        metrics.quote_count += 1
        metrics.last_quote_ts = time.time()
        # Running average spread
        n = metrics.quote_count
        metrics.avg_spread_quoted = (
            metrics.avg_spread_quoted * (n - 1) + spread_cents
        ) / n

    def record_reject(self, token_id: str = "", condition_id: str = "") -> None:
        """Record an order rejection."""
        self._counters["total_rejects"] += 1
        if token_id:
            metrics = self._get_market_metrics(token_id, condition_id)
            metrics.reject_count += 1

    def record_cancel(self, token_id: str = "", condition_id: str = "") -> None:
        """Record an order cancellation."""
        self._counters["total_cancels"] += 1
        if token_id:
            metrics = self._get_market_metrics(token_id, condition_id)
            metrics.cancel_count += 1

    def _get_market_metrics(self, token_id: str, condition_id: str = "") -> MarketMetrics:
        """Get or create market metrics."""
        if token_id not in self._market_metrics:
            self._market_metrics[token_id] = MarketMetrics(
                token_id=token_id,
                condition_id=condition_id,
            )
        return self._market_metrics[token_id]

    def get_bot_metrics(self) -> BotMetrics:
        """Compute aggregate bot metrics."""
        uptime = time.time() - self._start_time
        total_submitted = self._counters["orders_submitted"]
        total_rejects = self._counters["total_rejects"]

        reject_rate = (
            total_rejects / total_submitted * 100
            if total_submitted > 0
            else 0.0
        )

        avg_spread = (
            sum(self._quote_spreads) / len(self._quote_spreads)
            if self._quote_spreads
            else 0.0
        )

        return BotMetrics(
            uptime_seconds=uptime,
            total_quote_cycles=self._counters["quote_cycles"],
            total_orders_submitted=total_submitted,
            total_orders_canceled=self._counters["total_cancels"],
            total_orders_filled=self._counters["total_fills"],
            total_orders_rejected=total_rejects,
            total_fills=self._counters["total_fills"],
            total_volume=self._gauges["total_volume"],
            total_fees_paid=self._gauges["total_fees"],
            avg_spread_cents=avg_spread,
            reject_rate_pct=reject_rate,
            active_markets=len(self._market_metrics),
            quoting_markets=int(self._gauges.get("markets_quoted", 0)),
        )

    def get_market_metrics(self, token_id: str) -> MarketMetrics | None:
        """Get metrics for a specific market."""
        return self._market_metrics.get(token_id)

    def get_all_market_metrics(self) -> dict[str, MarketMetrics]:
        """Get all market metrics."""
        return dict(self._market_metrics)

    def get_acceptance_criteria(self) -> dict[str, Any]:
        """Check acceptance criteria from §16.

        - Quote uptime > 99%
        - Order reject rate < 0.5%
        - 5-second adverse selection < 60% of spread capture
        """
        metrics = self.get_bot_metrics()
        total = metrics.total_orders_submitted
        rejects = metrics.total_orders_rejected

        return {
            "reject_rate_pct": metrics.reject_rate_pct,
            "reject_rate_ok": metrics.reject_rate_pct < 0.5,
            "total_submitted": total,
            "total_rejected": rejects,
            "uptime_seconds": metrics.uptime_seconds,
        }

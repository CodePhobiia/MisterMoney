"""
Canary Metrics Tracker
Tracks V3 canary mode performance and usage
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

log = structlog.get_logger()


@dataclass
class BlendRecord:
    """Single blend operation record."""
    condition_id: str
    book_mid: float
    v3_signal: float | None
    blended: float
    v3_used: bool
    skew_cents: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    miss_reason: str | None = None


class CanaryMetrics:
    """Tracks V3 canary mode performance."""

    def __init__(self):
        """Initialize metrics tracker."""
        self.records: list[BlendRecord] = []
        self.miss_reasons: defaultdict[str, int] = defaultdict(int)

    def record_blend(
        self,
        condition_id: str,
        book_mid: float,
        v3_signal: float | None,
        blended: float,
        v3_used: bool,
        skew_cents: float,
        miss_reason: str | None = None,
    ) -> None:
        """
        Record a blend operation.

        Args:
            condition_id: Condition ID
            book_mid: Book midpoint
            v3_signal: Raw V3 signal (p_calibrated) or None
            blended: Final blended fair value
            v3_used: Whether V3 signal was used
            skew_cents: Actual skew applied (in cents)
            miss_reason: Why V3 wasn't used (if applicable)
        """
        record = BlendRecord(
            condition_id=condition_id,
            book_mid=book_mid,
            v3_signal=v3_signal,
            blended=blended,
            v3_used=v3_used,
            skew_cents=skew_cents,
            miss_reason=miss_reason,
        )

        self.records.append(record)

        if not v3_used and miss_reason:
            self.miss_reasons[miss_reason] += 1

    def get_summary(self) -> dict:
        """
        Get metrics summary.

        Returns:
            Dict with:
            - total_quotes: int
            - v3_used_count: int
            - v3_used_pct: float
            - avg_skew_cents: float
            - max_skew_cents: float
            - min_skew_cents: float
            - v3_miss_reasons: dict (reason -> count)
        """
        if not self.records:
            return {
                "total_quotes": 0,
                "v3_used_count": 0,
                "v3_used_pct": 0.0,
                "avg_skew_cents": 0.0,
                "max_skew_cents": 0.0,
                "min_skew_cents": 0.0,
                "v3_miss_reasons": {},
            }

        total = len(self.records)
        v3_used = sum(1 for r in self.records if r.v3_used)
        v3_used_pct = (v3_used / total * 100) if total > 0 else 0.0

        # Skew stats (only for records where V3 was used)
        skews = [r.skew_cents for r in self.records if r.v3_used]

        if skews:
            avg_skew = sum(skews) / len(skews)
            max_skew = max(skews)
            min_skew = min(skews)
        else:
            avg_skew = 0.0
            max_skew = 0.0
            min_skew = 0.0

        return {
            "total_quotes": total,
            "v3_used_count": v3_used,
            "v3_used_pct": round(v3_used_pct, 2),
            "avg_skew_cents": round(avg_skew, 2),
            "max_skew_cents": round(max_skew, 2),
            "min_skew_cents": round(min_skew, 2),
            "v3_miss_reasons": dict(self.miss_reasons),
        }

    def format_telegram_report(self) -> str:
        """
        Format metrics as Telegram message.

        Returns:
            Formatted report string
        """
        summary = self.get_summary()

        lines = [
            "📊 **V3 Canary Metrics**",
            "",
            f"**Total Quotes:** {summary['total_quotes']:,}",
            f"**V3 Used:** {summary['v3_used_count']:,} ({summary['v3_used_pct']}%)",
            "",
            "**Skew Stats:**",
            f"  • Avg: {summary['avg_skew_cents']:+.2f}¢",
            f"  • Max: {summary['max_skew_cents']:+.2f}¢",
            f"  • Min: {summary['min_skew_cents']:+.2f}¢",
        ]

        if summary["v3_miss_reasons"]:
            lines.append("")
            lines.append("**V3 Miss Reasons:**")
            for reason, count in sorted(
                summary["v3_miss_reasons"].items(),
                key=lambda x: x[1],
                reverse=True
            ):
                pct = (count / summary["total_quotes"] * 100) if summary["total_quotes"] > 0 else 0
                lines.append(f"  • {reason}: {count:,} ({pct:.1f}%)")

        return "\n".join(lines)

    def reset(self) -> None:
        """Reset all metrics."""
        self.records.clear()
        self.miss_reasons.clear()
        log.info("canary_metrics_reset")

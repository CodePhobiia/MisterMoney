"""Shadow dashboard — generates Telegram status reports.

Produces human-readable summaries of shadow mode performance:
- How many cycles analyzed
- Positive EV percentage
- Average EV delta per cycle
- Reward market comparison (PMM-2 vs V1)
- Churn reduction estimate
- Launch readiness status (gates passed)
"""

from __future__ import annotations

import os
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class ShadowDashboard:
    """Generate shadow mode status for Telegram reporting."""

    def __init__(self, counterfactual: Any) -> None:
        """Initialize shadow dashboard.

        Args:
            counterfactual: CounterfactualEngine instance
        """
        self.cf = counterfactual
        logger.info("shadow_dashboard_initialized")

    def generate_status(self) -> str:
        """Generate Telegram-friendly shadow status.

        Example:
        🔮 PMM-2 Shadow Mode — Day 3

        📊 150 cycles analyzed
        ✅ 72% positive EV (gate: 70%)
        📈 Avg EV delta: +$0.005/cycle
        🎯 Reward markets: 8 vs V1's 3
        ♻️ Churn: -15% vs V1

        Launch readiness: ✅ READY (4/4 gates passed)

        Returns:
            Formatted status string
        """
        summary = self.cf.get_summary()
        gates = self.cf.get_gates_status()
        promotion = self.cf.get_promotion_diagnostics()

        # Count gates passed
        gates_passed = sum(1 for g in gates.values() if g)
        total_gates = len(gates)

        # Build status message
        lines = [
            "🔮 *PMM-2 Shadow Mode*",
            "",
            f"📊 {summary['cycles_run']} cycles analyzed",
        ]

        # Positive EV gate
        ev_emoji = "✅" if gates.get("gate_ev_positive", False) else "⏳"
        lines.append(
            f"{ev_emoji} {summary['positive_ev_pct']:.1f}% positive EV "
            f"({summary['ev_sample_count']} samples)"
        )

        # EV delta
        ev_delta = summary["avg_ev_delta_usdc"]
        ev_sign = "+" if ev_delta >= 0 else ""
        lines.append(f"📈 Avg EV delta: {ev_sign}${ev_delta:.4f}/cycle")

        reward_emoji = "✅" if gates.get("gate_reward_capture", False) else "⏳"
        reward_improvement = summary["avg_reward_market_delta"]
        reward_sign = "+" if reward_improvement >= 0 else ""
        lines.append(
            f"{reward_emoji} Reward delta: "
            f"{reward_sign}{reward_improvement:.1f} markets "
            f"/ ${summary['avg_reward_ev_delta_usdc']:+.4f}"
        )

        # Churn reduction
        churn_emoji = "✅" if gates.get("gate_churn", False) else "⏳"
        churn_delta = summary["avg_churn_delta_per_order_min"]
        lines.append(
            f"{churn_emoji} Churn delta: {churn_delta:+.4f} cancels/order-min"
        )

        # Market overlap
        overlap = summary["avg_market_overlap"]
        lines.append(f"🎯 Market overlap: {overlap:.1%}")
        lines.append(
            f"🪞 Quote gap: "
            f"{summary['avg_overlap_quote_distance_bps']:.1f} bps "
            f"on overlapping markets"
        )

        # Shadow diagnostics
        lines.append("")
        if summary["diagnostic_ready"]:
            lines.append(
                f"Shadow diagnostics: ✅ Ready "
                f"({gates_passed}/{total_gates} gates passed)"
            )
        else:
            lines.append(
                f"Shadow diagnostics: ⏳ Not ready ({gates_passed}/{total_gates} gates passed)"
            )

            # Show which gates are blocking
            blocking = []
            if not gates.get("gate_ev_positive", False):
                blocking.append("rolling EV")
            if not gates.get("gate_reward_capture", False):
                blocking.append("reward capture")
            if not gates.get("gate_churn", False):
                blocking.append("churn")
            if not gates.get("gate_sample_size", False):
                blocking.append("sample size")

            if blocking:
                lines.append(f"Blocking: {', '.join(blocking)}")

        lines.append(
            f"Promotion gate: {'✅ READY' if summary['ready_for_live'] else '⏳ Not ready'} "
            f"({summary['shadow_days_observed']:.1f}/10.0 days observed)"
        )
        lines.append(
            f"Observed fills: {summary['fill_count_observed']} "
            f"across {len(summary['unique_fill_markets_observed'])} markets"
        )
        if promotion["blocking_gates"]:
            lines.append(f"Promotion blockers: {', '.join(promotion['blocking_gates'])}")

        return "\n".join(lines)

    async def send_daily_shadow_report(
        self,
        chat_id: str = os.getenv("TELEGRAM_CHAT_ID", ""),
    ) -> None:
        """Send daily shadow mode report via Telegram.

        Args:
            chat_id: Telegram chat ID to send to
        """
        try:
            from pmm1.notifications import send_telegram

            status = self.generate_status()
            await send_telegram(status)

            logger.info(
                "shadow_daily_report_sent",
                chat_id=chat_id,
                cycles=self.cf.cycle_count,
            )

        except Exception as e:
            logger.error(
                "shadow_daily_report_failed",
                error=str(e),
                exc_info=True,
            )

    async def send_milestone_report(self, milestone: int) -> None:
        """Send a report when reaching a cycle milestone.

        Args:
            milestone: cycle number reached (e.g., 100, 250, 500)
        """
        try:
            from pmm1.notifications import send_telegram

            summary = self.cf.get_summary()

            message = (
                f"🎯 *PMM-2 Milestone: {milestone} Cycles*\n\n"
                f"{self.generate_status()}"
            )

            await send_telegram(message)

            logger.info(
                "shadow_milestone_report_sent",
                milestone=milestone,
                ready=summary["ready_for_live"],
            )

        except Exception as e:
            logger.error(
                "shadow_milestone_report_failed",
                error=str(e),
                exc_info=True,
            )

    def get_detailed_metrics(self) -> dict[str, Any]:
        """Get detailed metrics for programmatic access.

        Returns:
            Dict with all metrics and rolling averages
        """
        summary = self.cf.get_summary()
        gates = self.cf.get_gates_status()

        return {
            "summary": summary,
            "gates": gates,
            "raw_metrics": {
                "market_overlap_history": self.cf.metrics["market_overlap_pct"][-20:],
                "reward_market_delta_history": self.cf.metrics["reward_market_delta"][-20:],
                "reward_ev_delta_history": self.cf.metrics["reward_ev_delta_usdc"][-20:],
                "churn_delta_history": self.cf.metrics["churn_delta_per_order_min"][-20:],
                "ev_delta_history": self.cf.metrics["ev_delta_usdc"][-20:],
            },
        }

"""
Weekly Evaluator — Analyze resolved markets and generate calibration labels

Runs weekly to:
1. Calculate Brier scores by route
2. Identify patterns in successes/failures
3. Generate calibration labels for retraining
4. Send summary report
"""

import json
import os
from datetime import datetime, timedelta

import structlog

from v3.evidence.db import Database
from v3.providers.registry import ProviderRegistry

from .prompts import WEEKLY_EVALUATION_SYSTEM, build_weekly_eval_prompt

log = structlog.get_logger()


class WeeklyEvaluator:
    """
    Weekly job: GPT-5.4-pro reviews all recently resolved markets.
    Generates calibration labels for retraining.
    """

    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    def __init__(self, db: Database, registry: ProviderRegistry):
        """
        Initialize weekly evaluator

        Args:
            db: Database instance
            registry: Provider registry
        """
        self.db = db
        self.registry = registry

    async def _get_resolved_markets(self, days: int = 7) -> list[dict]:
        """
        Get markets resolved in the last N days

        Args:
            days: Number of days to look back

        Returns:
            List of dicts with {
                condition_id, question, route, p_hat, uncertainty,
                outcome, resolved_at, brier_score
            }
        """
        cutoff = datetime.utcnow() - timedelta(days=days)

        # Query resolved markets
        # NOTE: In production, this would join with a markets table
        # For now, we'll query signals and mock the resolution data
        query = """
            SELECT
                fv.condition_id,
                fv.route,
                fv.p_calibrated as p_hat,
                fv.uncertainty,
                fv.generated_at
            FROM fair_value_signals fv
            WHERE fv.generated_at >= $1
            ORDER BY fv.condition_id, fv.generated_at DESC
        """

        rows = await self.db.fetch(query, cutoff)

        # Group by condition_id and take most recent signal per market
        markets_by_id = {}
        for row in rows:
            cid = row["condition_id"]
            if cid not in markets_by_id:
                markets_by_id[cid] = row

        # Mock resolution outcomes (in production, fetch from resolved_markets table)
        resolved = []
        for cid, signal in markets_by_id.items():
            # Mock outcome (50/50 for testing)
            import random
            outcome = random.choice([0, 1])

            # Calculate Brier score
            p_hat = signal["p_hat"]
            brier = (p_hat - outcome) ** 2

            resolved.append({
                "condition_id": cid,
                "question": f"Market {cid}",  # Would fetch from markets table
                "route": signal["route"],
                "p_hat": p_hat,
                "uncertainty": signal["uncertainty"],
                "outcome": outcome,
                "resolved_at": signal["generated_at"],
                "brier_score": brier,
            })

        return resolved

    async def evaluate_resolved_markets(self) -> dict:
        """
        Run weekly evaluation. Returns summary stats.

        Returns:
            Dict with {
                total_markets, by_route: {route: {count, avg_brier}},
                insights: {...}
            }
        """
        log.info("starting_weekly_evaluation")

        # Get resolved markets
        resolved = await self._get_resolved_markets(days=7)

        if not resolved:
            log.warning("no_resolved_markets_found")
            return {
                "total_markets": 0,
                "by_route": {},
                "insights": {}
            }

        log.info("resolved_markets_found", count=len(resolved))

        # Calculate route statistics
        by_route = {}
        for market in resolved:
            route = market["route"]
            if route not in by_route:
                by_route[route] = {
                    "count": 0,
                    "total_brier": 0.0,
                    "markets": []
                }

            by_route[route]["count"] += 1
            by_route[route]["total_brier"] += market["brier_score"]
            by_route[route]["markets"].append(market)

        # Calculate averages
        for route, stats in by_route.items():
            stats["avg_brier"] = stats["total_brier"] / stats["count"]

        log.info(
            "route_statistics_calculated",
            by_route={k: {"count": v["count"], "avg_brier": round(v["avg_brier"], 4)}
                      for k, v in by_route.items()}
        )

        # Get insights from GPT-5.4-pro
        insights = await self._generate_insights(resolved)

        return {
            "total_markets": len(resolved),
            "by_route": by_route,
            "insights": insights,
        }

    async def _generate_insights(self, resolved: list[dict]) -> dict:
        """
        Use GPT-5.4-pro to analyze patterns

        Args:
            resolved: List of resolved market dicts

        Returns:
            Insights dict from GPT-5.4-pro
        """
        # Get provider
        provider = await self.registry.get("gpt54pro")
        if not provider:
            provider = await self.registry.get("gpt54")

        if not provider:
            log.warning("no_provider_for_insights_generation")
            return {
                "route_insights": {},
                "systematic_biases": [],
                "failure_patterns": [],
                "calibration_recommendations": []
            }

        # Build prompt
        user_prompt = build_weekly_eval_prompt(resolved)

        try:
            messages = [
                {"role": "system", "content": WEEKLY_EVALUATION_SYSTEM},
                {"role": "user", "content": user_prompt}
            ]

            response = await provider.complete(
                messages=messages,
                response_format={"type": "json_object"},
            )

            insights = json.loads(response.text)
            log.info("insights_generated", insights=insights)
            return insights

        except Exception as e:
            log.error("insights_generation_failed", error=str(e))
            return {
                "route_insights": {},
                "systematic_biases": [],
                "failure_patterns": [],
                "calibration_recommendations": []
            }

    async def generate_calibration_labels(self, resolved: list[dict]) -> list[dict]:
        """
        Generate (features, outcome) pairs for calibrator retraining

        Args:
            resolved: List of resolved market dicts

        Returns:
            List of calibration label dicts with {
                condition_id, route, features: {...}, outcome
            }
        """
        labels = []

        for market in resolved:
            # Extract features for calibration
            features = {
                "p_raw": market["p_hat"],
                "uncertainty": market["uncertainty"],
                "route": market["route"],
                # Would include more features in production:
                # - evidence count, reliability scores
                # - time to resolution
                # - market volume/liquidity
                # - model agreement scores
            }

            label = {
                "condition_id": market["condition_id"],
                "route": market["route"],
                "features": features,
                "outcome": market["outcome"],
                "brier_score": market["brier_score"],
            }

            labels.append(label)

        log.info("calibration_labels_generated", count=len(labels))
        return labels

    def format_weekly_report(self, stats: dict) -> str:
        """
        Format weekly evaluation report

        Args:
            stats: Stats dict from evaluate_resolved_markets()

        Returns:
            Formatted report string
        """
        total = stats["total_markets"]
        by_route = stats["by_route"]
        insights = stats["insights"]

        # Header
        week_start = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        report = f"""📊 V3 Weekly Evaluation — Week of {week_start}

Markets resolved: {total}

Route performance (Brier score, lower=better):"""

        # Route stats
        sorted_routes = sorted(
            by_route.items(),
            key=lambda x: x[1]["avg_brier"]
        )

        for route, stats_dict in sorted_routes:
            count = stats_dict["count"]
            avg_brier = stats_dict["avg_brier"]
            star = " ⭐" if avg_brier < 0.15 else ""
            report += f"\n• {route.capitalize()}: {avg_brier:.2f} ({count} markets){star}"

        # Insights
        if insights.get("route_insights"):
            report += "\n\nGPT-5.4-pro insights:"
            for route, insight in insights["route_insights"].items():
                report += f"\n• {route.capitalize()}: {insight[:100]}..."

        # Systematic biases
        if insights.get("systematic_biases"):
            report += "\n\nSystematic biases detected:"
            for bias in insights["systematic_biases"][:3]:
                report += f"\n• {bias}"

        # Calibration recommendations
        if insights.get("calibration_recommendations"):
            report += "\n\nCalibration recommendations:"
            for rec in insights["calibration_recommendations"][:3]:
                report += f"\n• {rec}"

        report += f"\n\nCalibration labels generated: {total}"

        return report

    async def send_report(self, report: str) -> None:
        """
        Send report via Telegram

        Args:
            report: Formatted report string
        """
        try:
            log.info(
                "would_send_telegram_report",
                chat_id=self.TELEGRAM_CHAT_ID,
                report_length=len(report)
            )
            # In production, use message tool or Telegram API
            log.info("weekly_report", report=report)
        except Exception as e:
            log.error("report_send_failed", error=str(e))

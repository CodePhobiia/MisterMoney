"""Counterfactual comparison engine for PMM-1 actuals vs PMM-2 shadow plans."""

from __future__ import annotations

import statistics
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class CounterfactualEngine:
    """Compare V1 actuals vs PMM-2 using real state and rolling diagnostics."""

    ROLLING_WINDOW = 100
    MIN_EV_SAMPLES = 60
    MIN_REWARD_SAMPLES = 60
    MIN_CHURN_SAMPLES = 60
    MIN_POSITIVE_EV_PCT = 55.0

    def __init__(self, shadow_logger):
        self.shadow_logger = shadow_logger
        self.cycle_count: int = 0
        self.history: list[dict[str, Any]] = []
        self.metrics: dict[str, list[float]] = {
            "market_overlap_pct": [],
            "reward_market_delta": [],
            "reward_ev_delta_usdc": [],
            "churn_delta_per_order_min": [],
            "ev_delta_usdc": [],
            "overlap_quote_distance_bps": [],
        }

        logger.info("counterfactual_engine_initialized")

    def compare_cycle(
        self,
        v1_state: dict[str, Any],
        pmm2_plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Compare one allocator cycle."""

        self.cycle_count += 1

        v1_markets = set(v1_state.get("markets", []))
        pmm2_markets = set(pmm2_plan.get("markets", []))
        overlap = v1_markets & pmm2_markets
        union = v1_markets | pmm2_markets
        overlap_pct = len(overlap) / len(union) if union else 1.0

        v1_total_ev = float(v1_state.get("total_expected_ev_usdc", 0.0) or 0.0)
        pmm2_total_ev = float(pmm2_plan.get("total_expected_ev_usdc", 0.0) or 0.0)
        ev_sample_valid = bool(
            v1_state.get("ev_sample_valid", True) and pmm2_plan.get("ev_sample_valid", True)
        )
        ev_delta = pmm2_total_ev - v1_total_ev if ev_sample_valid else 0.0

        v1_reward_markets = int(v1_state.get("reward_market_count", 0) or 0)
        pmm2_reward_markets = int(pmm2_plan.get("reward_market_count", 0) or 0)
        reward_market_delta = pmm2_reward_markets - v1_reward_markets
        v1_reward_ev = float(v1_state.get("total_reward_ev_usdc", 0.0) or 0.0)
        pmm2_reward_ev = float(pmm2_plan.get("total_reward_ev_usdc", 0.0) or 0.0)
        reward_ev_delta = pmm2_reward_ev - v1_reward_ev
        reward_sample_valid = ev_sample_valid

        cycle_minutes = max(float(v1_state.get("cycle_minutes", 0.0) or 0.0), 0.0)
        if cycle_minutes <= 0.0:
            cycle_minutes = max(float(pmm2_plan.get("cycle_minutes", 0.0) or 0.0), 0.0)
        if cycle_minutes <= 0.0:
            cycle_minutes = 1.0

        v1_cancel_count = float(v1_state.get("cancel_count_recent", 0.0) or 0.0)
        pmm2_cancel_count = float(pmm2_plan.get("projected_cancel_count", 0.0) or 0.0)
        v1_live_order_minutes = float(v1_state.get("live_order_minutes", 0.0) or 0.0)
        pmm2_live_order_minutes = float(pmm2_plan.get("live_order_minutes", 0.0) or 0.0)
        churn_sample_valid = (v1_live_order_minutes + pmm2_live_order_minutes) > 0.0
        v1_cancel_rate = (
            v1_cancel_count / max(v1_live_order_minutes, 1e-9)
            if churn_sample_valid
            else 0.0
        )
        pmm2_cancel_rate = (
            pmm2_cancel_count / max(pmm2_live_order_minutes, 1e-9)
            if churn_sample_valid
            else 0.0
        )
        churn_delta = v1_cancel_rate - pmm2_cancel_rate if churn_sample_valid else 0.0

        quote_distance_bps = self._quote_distance_bps(
            v1_state.get("market_evaluations", []),
            pmm2_plan.get("market_evaluations", []),
            overlap,
        )

        comparison = {
            "cycle_num": self.cycle_count,
            "market_overlap_pct": overlap_pct,
            "overlap_quote_distance_bps": quote_distance_bps,
            "v1_markets": sorted(v1_markets),
            "pmm2_markets": sorted(pmm2_markets),
            "pmm2_only_markets": sorted(pmm2_markets - v1_markets),
            "v1_only_markets": sorted(v1_markets - pmm2_markets),
            "ev_sample_valid": ev_sample_valid,
            "reward_sample_valid": reward_sample_valid,
            "churn_sample_valid": churn_sample_valid,
            "v1_total_ev_usdc": v1_total_ev,
            "pmm2_total_ev_usdc": pmm2_total_ev,
            "ev_delta_usdc": ev_delta,
            "v1_reward_market_count": v1_reward_markets,
            "pmm2_reward_market_count": pmm2_reward_markets,
            "reward_market_delta": reward_market_delta,
            "v1_reward_ev_usdc": v1_reward_ev,
            "pmm2_reward_ev_usdc": pmm2_reward_ev,
            "reward_ev_delta_usdc": reward_ev_delta,
            "v1_cancel_count": v1_cancel_count,
            "pmm2_cancel_count": pmm2_cancel_count,
            "v1_cancel_rate_per_order_min": v1_cancel_rate,
            "pmm2_cancel_rate_per_order_min": pmm2_cancel_rate,
            "churn_delta_per_order_min": churn_delta,
            "divergences": [],
        }

        comparison["divergences"] = self._log_divergences(comparison)

        self.history.append(comparison)
        self.metrics["market_overlap_pct"].append(overlap_pct)
        self.metrics["reward_market_delta"].append(float(reward_market_delta))
        self.metrics["reward_ev_delta_usdc"].append(reward_ev_delta)
        self.metrics["churn_delta_per_order_min"].append(churn_delta)
        self.metrics["ev_delta_usdc"].append(ev_delta)
        self.metrics["overlap_quote_distance_bps"].append(quote_distance_bps)

        gate_diagnostics = self.get_gate_diagnostics()
        summary = self.get_summary()
        comparison["gate_diagnostics"] = gate_diagnostics
        comparison["summary"] = summary

        logger.info(
            "counterfactual_cycle_compared",
            cycle=self.cycle_count,
            overlap_pct=overlap_pct,
            ev_delta_usdc=ev_delta,
            reward_ev_delta_usdc=reward_ev_delta,
            churn_delta_per_order_min=churn_delta,
            ready_for_live=summary["ready_for_live"],
        )

        return comparison

    def _recent_history(self, window: int | None = None) -> list[dict[str, Any]]:
        sample_window = window or self.ROLLING_WINDOW
        if sample_window <= 0:
            return list(self.history)
        return self.history[-sample_window:]

    def _mean(self, values: list[float]) -> float:
        return statistics.mean(values) if values else 0.0

    def _quote_distance_bps(
        self,
        v1_market_evaluations: list[dict[str, Any]],
        pmm2_market_evaluations: list[dict[str, Any]],
        overlapping_markets: set[str],
    ) -> float:
        if not overlapping_markets:
            return 0.0

        v1_map = {
            evaluation.get("condition_id"): evaluation
            for evaluation in v1_market_evaluations
            if evaluation.get("condition_id")
        }
        pmm2_map = {
            evaluation.get("condition_id"): evaluation
            for evaluation in pmm2_market_evaluations
            if evaluation.get("condition_id")
        }

        quote_diffs = []
        for condition_id in overlapping_markets:
            v1_eval = v1_map.get(condition_id)
            pmm2_eval = pmm2_map.get(condition_id)
            if not v1_eval or not pmm2_eval:
                continue
            for key in ("best_bid", "best_ask"):
                v1_px = float(v1_eval.get(key, 0.0) or 0.0)
                pmm2_px = float(pmm2_eval.get(key, 0.0) or 0.0)
                if v1_px > 0.0 and pmm2_px > 0.0:
                    quote_diffs.append(abs(v1_px - pmm2_px) * 10000.0)

        return self._mean(quote_diffs)

    def _log_divergences(self, comparison: dict[str, Any]) -> list[dict[str, Any]]:
        divergences: list[dict[str, Any]] = []

        if comparison["market_overlap_pct"] < 0.5:
            details = {
                "overlap_pct": comparison["market_overlap_pct"],
                "pmm2_only": comparison["pmm2_only_markets"],
                "v1_only": comparison["v1_only_markets"],
            }
            self.shadow_logger.log_divergence("market_selection", details)
            divergences.append({"type": "market_selection", "details": details})

        if comparison["ev_sample_valid"] and comparison["ev_delta_usdc"] < 0.0:
            details = {
                "v1_ev": comparison["v1_total_ev_usdc"],
                "pmm2_ev": comparison["pmm2_total_ev_usdc"],
                "delta": comparison["ev_delta_usdc"],
            }
            self.shadow_logger.log_divergence("economic_regression", details)
            divergences.append({"type": "economic_regression", "details": details})

        if comparison["reward_sample_valid"] and comparison["reward_ev_delta_usdc"] < 0.0:
            details = {
                "v1_reward_ev": comparison["v1_reward_ev_usdc"],
                "pmm2_reward_ev": comparison["pmm2_reward_ev_usdc"],
                "delta": comparison["reward_ev_delta_usdc"],
            }
            self.shadow_logger.log_divergence("reward_regression", details)
            divergences.append({"type": "reward_regression", "details": details})

        if comparison["churn_sample_valid"] and comparison["churn_delta_per_order_min"] < 0.0:
            details = {
                "v1_cancel_rate": comparison["v1_cancel_rate_per_order_min"],
                "pmm2_cancel_rate": comparison["pmm2_cancel_rate_per_order_min"],
                "delta": comparison["churn_delta_per_order_min"],
            }
            self.shadow_logger.log_divergence("churn_regression", details)
            divergences.append({"type": "churn_regression", "details": details})

        return divergences

    def get_gate_diagnostics(self, window: int | None = None) -> dict[str, Any]:
        recent = self._recent_history(window)
        ev_samples = [row for row in recent if row.get("ev_sample_valid", False)]
        reward_samples = [row for row in recent if row.get("reward_sample_valid", False)]
        churn_samples = [row for row in recent if row.get("churn_sample_valid", False)]

        positive_ev_pct = (
            (sum(1 for row in ev_samples if row["ev_delta_usdc"] > 0.0) / len(ev_samples)) * 100.0
            if ev_samples
            else 0.0
        )
        avg_ev_delta = self._mean([row["ev_delta_usdc"] for row in ev_samples])
        avg_reward_market_delta = self._mean([row["reward_market_delta"] for row in reward_samples])
        avg_reward_ev_delta = self._mean([row["reward_ev_delta_usdc"] for row in reward_samples])
        avg_churn_delta = self._mean([row["churn_delta_per_order_min"] for row in churn_samples])

        gates = {
            "gate_ev_positive": {
                "pass": len(ev_samples) >= self.MIN_EV_SAMPLES
                and avg_ev_delta > 0.0
                and positive_ev_pct >= self.MIN_POSITIVE_EV_PCT,
                "sample_count": len(ev_samples),
                "threshold_sample_count": self.MIN_EV_SAMPLES,
                "observed_avg_ev_delta_usdc": avg_ev_delta,
                "observed_positive_ev_pct": positive_ev_pct,
                "threshold_positive_ev_pct": self.MIN_POSITIVE_EV_PCT,
            },
            "gate_reward_capture": {
                "pass": len(reward_samples) >= self.MIN_REWARD_SAMPLES
                and avg_reward_ev_delta >= 0.0
                and avg_reward_market_delta >= 0.0,
                "sample_count": len(reward_samples),
                "threshold_sample_count": self.MIN_REWARD_SAMPLES,
                "observed_avg_reward_ev_delta_usdc": avg_reward_ev_delta,
                "observed_avg_reward_market_delta": avg_reward_market_delta,
            },
            "gate_churn": {
                "pass": len(churn_samples) >= self.MIN_CHURN_SAMPLES
                and avg_churn_delta >= 0.0,
                "sample_count": len(churn_samples),
                "threshold_sample_count": self.MIN_CHURN_SAMPLES,
                "observed_avg_churn_delta_per_order_min": avg_churn_delta,
            },
            "gate_sample_size": {
                "pass": len(recent) >= self.ROLLING_WINDOW
                and len(ev_samples) >= self.MIN_EV_SAMPLES
                and len(reward_samples) >= self.MIN_REWARD_SAMPLES
                and len(churn_samples) >= self.MIN_CHURN_SAMPLES,
                "window_cycles": len(recent),
                "threshold_window_cycles": self.ROLLING_WINDOW,
                "ev_sample_count": len(ev_samples),
                "reward_sample_count": len(reward_samples),
                "churn_sample_count": len(churn_samples),
            },
        }

        blocking_gates = [name for name, details in gates.items() if not details["pass"]]
        ready_for_live = all(details["pass"] for details in gates.values())

        return {
            "window_cycles": len(recent),
            "ev_sample_count": len(ev_samples),
            "reward_sample_count": len(reward_samples),
            "churn_sample_count": len(churn_samples),
            "gates": gates,
            "blocking_gates": blocking_gates,
            "ready_for_live": ready_for_live,
        }

    def get_summary(self) -> dict[str, Any]:
        if self.cycle_count == 0:
            return {
                "cycles_run": 0,
                "rolling_window_cycles": 0,
                "positive_ev_pct": 0.0,
                "avg_ev_delta_usdc": 0.0,
                "avg_market_overlap": 0.0,
                "avg_reward_market_delta": 0.0,
                "avg_reward_ev_delta_usdc": 0.0,
                "avg_churn_delta_per_order_min": 0.0,
                "avg_overlap_quote_distance_bps": 0.0,
                "ready_for_live": False,
                "gate_blockers": [],
                "ev_sample_count": 0,
                "reward_sample_count": 0,
                "churn_sample_count": 0,
            }

        recent = self._recent_history()
        ev_samples = [row for row in recent if row.get("ev_sample_valid", False)]
        reward_samples = [row for row in recent if row.get("reward_sample_valid", False)]
        churn_samples = [row for row in recent if row.get("churn_sample_valid", False)]
        gate_diagnostics = self.get_gate_diagnostics()

        positive_ev_pct = (
            (sum(1 for row in ev_samples if row["ev_delta_usdc"] > 0.0) / len(ev_samples)) * 100.0
            if ev_samples
            else 0.0
        )

        return {
            "cycles_run": self.cycle_count,
            "rolling_window_cycles": len(recent),
            "positive_ev_pct": positive_ev_pct,
            "avg_ev_delta_usdc": self._mean([row["ev_delta_usdc"] for row in ev_samples]),
            "avg_market_overlap": self._mean([row["market_overlap_pct"] for row in recent]),
            "avg_reward_market_delta": self._mean(
                [row["reward_market_delta"] for row in reward_samples]
            ),
            "avg_reward_ev_delta_usdc": self._mean(
                [row["reward_ev_delta_usdc"] for row in reward_samples]
            ),
            "avg_churn_delta_per_order_min": self._mean(
                [row["churn_delta_per_order_min"] for row in churn_samples]
            ),
            "avg_overlap_quote_distance_bps": self._mean(
                [row["overlap_quote_distance_bps"] for row in recent]
            ),
            "ready_for_live": gate_diagnostics["ready_for_live"],
            "gate_blockers": gate_diagnostics["blocking_gates"],
            "ev_sample_count": len(ev_samples),
            "reward_sample_count": len(reward_samples),
            "churn_sample_count": len(churn_samples),
        }

    def is_ready_for_live(self) -> bool:
        return self.get_gate_diagnostics()["ready_for_live"]

    def get_gates_status(self) -> dict[str, bool]:
        diagnostics = self.get_gate_diagnostics()
        return {
            gate_name: gate_details["pass"]
            for gate_name, gate_details in diagnostics["gates"].items()
        }

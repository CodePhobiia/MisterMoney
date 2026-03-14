"""Capture an audit-friendly snapshot of PMM-1 state for shadow comparison."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from pmm2.shadow.valuation import (
    MarketShadowContext,
    aggregate_market_evaluations,
    evaluate_quote_set,
    market_context_from_object,
    merge_market_contexts,
    shadow_quote_from_order,
)
from pmm2.v1_views import read_bot_state_nav

logger = structlog.get_logger(__name__)


class V1StateSnapshot:
    """Capture live PMM-1 state in the same valuation space as PMM-2."""

    @staticmethod
    def capture(
        bot_state: Any,
        *,
        market_contexts: dict[str, MarketShadowContext | Any] | None = None,
        queue_estimator: Any = None,
        fill_hazard: Any = None,
        allocator_interval_sec: float = 60.0,
    ) -> dict[str, Any]:
        """Capture a rich V1 state snapshot for counterfactual analysis."""

        snapshot: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "markets": set(),
            "orders": [],
            "positions": [],
            "scoring_count": 0,
            "reward_eligible_count": 0,
            "total_capital_deployed": 0.0,
            "nav": 0.0,
            "nav_valid": False,
            "market_evaluations": [],
            "total_expected_ev_usdc": 0.0,
            "total_reward_ev_usdc": 0.0,
            "total_rebate_ev_usdc": 0.0,
            "reward_market_count": 0,
            "scoring_market_count": 0,
            "reward_pair_mass_total": 0.0,
            "ev_sample_valid": True,
            "invalid_markets": [],
            "live_order_minutes": 0.0,
            "lifecycle_counts": {},
        }

        try:
            context_map = V1StateSnapshot._build_market_context_map(
                bot_state,
                market_contexts=market_contexts,
            )

            nav = V1StateSnapshot._get_nav(bot_state)
            snapshot["nav"] = nav
            snapshot["nav_valid"] = nav > 0.0

            quotes_by_market: dict[str, list[Any]] = {}

            if hasattr(bot_state, "order_tracker"):
                active_orders = list(bot_state.order_tracker.get_active_orders(token_id=None))

                for order in active_orders:
                    condition_id = str(getattr(order, "condition_id", "") or "")
                    context = context_map.get(condition_id)
                    fill_prob = V1StateSnapshot._fill_probability_for_order(
                        order,
                        context,
                        queue_estimator=queue_estimator,
                        fill_hazard=fill_hazard,
                    )
                    queue_state = None
                    if queue_estimator is not None:
                        queue_state = queue_estimator.states.get(getattr(order, "order_id", ""))

                    shadow_quote = shadow_quote_from_order(
                        order,
                        context,
                        fill_prob_30s=fill_prob,
                    )
                    if shadow_quote is None:
                        continue

                    order_dict = {
                        "order_id": getattr(order, "order_id", ""),
                        "token_id": getattr(order, "token_id", ""),
                        "condition_id": condition_id,
                        "side": getattr(order, "side", ""),
                        "raw_price": float(getattr(order, "price_float", 0.0) or 0.0),
                        "price": shadow_quote.price,
                        "size": shadow_quote.size,
                        "capital_usdc": shadow_quote.capital_usdc,
                        "status": getattr(
                            getattr(order, "state", ""),
                            "value",
                            str(getattr(order, "state", "")),
                        ),
                        "is_scoring": bool(getattr(order, "is_scoring", False)),
                        "quote_role": shadow_quote.quote_role,
                        "fill_prob_30s": shadow_quote.fill_prob_30s,
                        "age_sec": float(getattr(order, "age_seconds", 0.0) or 0.0),
                        "queue_ahead_mid": float(getattr(queue_state, "est_ahead_mid", 0.0) or 0.0),
                        "eta_sec": float(getattr(queue_state, "eta_sec", 0.0) or 0.0),
                        "reward_eligible": bool(context.reward_eligible) if context else False,
                    }

                    snapshot["orders"].append(order_dict)
                    snapshot["total_capital_deployed"] += shadow_quote.capital_usdc
                    if condition_id:
                        snapshot["markets"].add(condition_id)
                        quotes_by_market.setdefault(condition_id, []).append(shadow_quote)
                    if order_dict["is_scoring"]:
                        snapshot["scoring_count"] += 1

            if hasattr(bot_state, "position_tracker"):
                positions = list(bot_state.position_tracker.get_active_positions())
                for pos in positions:
                    context = context_map.get(getattr(pos, "condition_id", ""))
                    yes_mark = context.best_bid or context.mid if context else 0.0
                    no_mark = max(0.0, 1.0 - (context.best_ask or context.mid)) if context else 0.0
                    unrealized = 0.0
                    if hasattr(pos, "mark_to_market"):
                        unrealized = float(pos.mark_to_market(yes_mark, no_mark))

                    snapshot["positions"].append(
                        {
                            "condition_id": getattr(pos, "condition_id", ""),
                            "event_id": getattr(pos, "event_id", ""),
                            "yes_size": float(getattr(pos, "yes_size", 0.0) or 0.0),
                            "no_size": float(getattr(pos, "no_size", 0.0) or 0.0),
                            "yes_cost_basis": float(getattr(pos, "yes_cost_basis", 0.0) or 0.0),
                            "no_cost_basis": float(getattr(pos, "no_cost_basis", 0.0) or 0.0),
                            "realized_pnl": float(getattr(pos, "realized_pnl", 0.0) or 0.0),
                            "unrealized_pnl": unrealized,
                        }
                    )

            market_evaluations = []
            for condition_id, quotes in quotes_by_market.items():
                evaluation = evaluate_quote_set(context_map.get(condition_id), quotes)
                market_evaluations.append(evaluation)

            aggregate = aggregate_market_evaluations(market_evaluations)
            snapshot["market_evaluations"] = [
                evaluation.model_dump() for evaluation in market_evaluations
            ]
            snapshot["total_expected_ev_usdc"] = aggregate["total_ev_usdc"]
            snapshot["total_reward_ev_usdc"] = aggregate["total_reward_ev_usdc"]
            snapshot["total_rebate_ev_usdc"] = aggregate["total_rebate_ev_usdc"]
            snapshot["reward_market_count"] = aggregate["reward_market_count"]
            snapshot["reward_eligible_count"] = aggregate["reward_market_count"]
            snapshot["scoring_market_count"] = aggregate["scoring_market_count"]
            snapshot["reward_pair_mass_total"] = aggregate["reward_pair_mass_total"]
            snapshot["ev_sample_valid"] = aggregate["ev_sample_valid"]
            snapshot["invalid_markets"] = aggregate["invalid_markets"]
            snapshot["live_order_minutes"] = len(snapshot["orders"]) * (
                allocator_interval_sec / 60.0
            )

            if hasattr(bot_state, "order_tracker") and hasattr(
                bot_state.order_tracker, "snapshot_lifecycle_counts"
            ):
                snapshot["lifecycle_counts"] = bot_state.order_tracker.snapshot_lifecycle_counts()

            snapshot["markets"] = sorted(snapshot["markets"])

            logger.debug(
                "v1_state_captured",
                markets=len(snapshot["markets"]),
                orders=len(snapshot["orders"]),
                positions=len(snapshot["positions"]),
                total_expected_ev_usdc=snapshot["total_expected_ev_usdc"],
                ev_sample_valid=snapshot["ev_sample_valid"],
            )

        except Exception as e:
            logger.error(
                "v1_state_capture_failed",
                error=str(e),
                exc_info=True,
            )
            snapshot["error"] = str(e)
            snapshot["markets"] = sorted(snapshot["markets"])

        return snapshot

    @staticmethod
    def _build_market_context_map(
        bot_state: Any,
        *,
        market_contexts: dict[str, MarketShadowContext | Any] | None = None,
    ) -> dict[str, MarketShadowContext]:
        context_map: dict[str, MarketShadowContext] = {}

        if market_contexts:
            for condition_id, market in market_contexts.items():
                context = market_context_from_object(market)
                if not context.condition_id:
                    context.condition_id = condition_id
                context_map[condition_id] = merge_market_contexts(
                    context_map.get(condition_id),
                    context,
                ) or context

        reward_set = set(getattr(bot_state, "reward_eligible", set()) or set())
        active_markets = getattr(bot_state, "active_markets", {}) or {}
        for condition_id, market in active_markets.items():
            context = market_context_from_object(
                market,
                reward_eligible_override=condition_id in reward_set,
            )
            if not context.condition_id:
                context.condition_id = condition_id
            context_map[condition_id] = merge_market_contexts(
                context_map.get(condition_id),
                context,
            ) or context

        return context_map

    @staticmethod
    def _fill_probability_for_order(
        order: Any,
        context: MarketShadowContext | None,
        *,
        queue_estimator: Any = None,
        fill_hazard: Any = None,
    ) -> float:
        order_id = getattr(order, "order_id", "")
        if queue_estimator is not None:
            state = queue_estimator.states.get(order_id)
            if state is not None:
                return float(getattr(state, "fill_prob_30s", 0.0) or 0.0)

        if fill_hazard is None or context is None:
            return 0.0

        shadow_quote = shadow_quote_from_order(order, context, fill_prob_30s=0.0)
        if shadow_quote is None:
            return 0.0

        visible_depth = (
            context.depth_at_best_bid
            if shadow_quote.quote_role == "bid"
            else context.depth_at_best_ask
        )
        queue_ahead = max(visible_depth - shadow_quote.size, 0.0)
        depletion_rate = fill_hazard.get_depletion_rate(getattr(order, "token_id", ""))
        return float(
            fill_hazard.fill_probability(
                queue_ahead=queue_ahead,
                order_size=shadow_quote.size,
                horizon_sec=30.0,
                depletion_rate=depletion_rate,
            )
        )

    @staticmethod
    def _get_nav(bot_state: Any) -> float:
        """Get current NAV from bot state."""
        nav = read_bot_state_nav(bot_state)
        if nav <= 0.0:
            logger.warning("nav_not_available_returning_zero")
        return nav

    @staticmethod
    def summarize(snapshot: dict[str, Any]) -> str:
        """Generate a concise human-readable summary of V1 state."""

        lines = [
            f"V1 State @ {snapshot.get('timestamp', 'unknown')}",
            f"Markets: {len(snapshot.get('markets', []))}",
            f"Orders: {len(snapshot.get('orders', []))}",
            f"Scoring orders: {snapshot.get('scoring_count', 0)}",
            f"Reward markets: {snapshot.get('reward_market_count', 0)}",
            f"Capital deployed: ${snapshot.get('total_capital_deployed', 0.0):.2f}",
            f"Expected EV: ${snapshot.get('total_expected_ev_usdc', 0.0):.4f}",
            f"NAV: ${snapshot.get('nav', 0.0):.2f}",
        ]

        invalid_markets = snapshot.get("invalid_markets", [])
        if invalid_markets:
            lines.append(f"Invalid EV markets: {len(invalid_markets)}")

        return "\n".join(lines)

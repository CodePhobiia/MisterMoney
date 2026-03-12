"""PMM-2 runtime loops — the brain of PMM-2.

5 concurrent loops:
1. Event-driven: WS deltas → queue estimator updates (called from callbacks)
2. Fast (250ms): recompute queue states, ETAs, fill probabilities
3. Medium (10s): refresh market EV, bundle values
4. Allocator (60s): full allocation cycle (score → allocate → plan → diff → execute)
5. Slow (5min): universe refresh, metadata updates, depletion calibration

All loops are resilient: catch exceptions, log errors, continue looping.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from typing import Any

import structlog

from pmm2.allocator import AdjustedScorer, AllocationConstraints, CapitalAllocator
from pmm2.config import PMM2Config
from pmm2.persistence.action_ev import ActionEVCalculator
from pmm2.persistence.hysteresis import HysteresisConfig, HysteresisGate
from pmm2.persistence.optimizer import PersistenceOptimizer
from pmm2.persistence.state_machine import StateMachine
from pmm2.persistence.warmup import WarmupEstimator
from pmm2.planner import DiffEngine, OrderMutation, QuotePlanner, TargetQuotePlan
from pmm2.queue import DepletionCalculator, FillHazard, QueueEstimator
from pmm2.scorer.combined import MarketEVScorer
from pmm2.shadow import CounterfactualEngine, ShadowDashboard, ShadowLogger, V1StateSnapshot
from pmm2.shadow.valuation import (
    aggregate_market_evaluations,
    evaluate_quote_set,
    market_context_from_object,
    merge_market_contexts,
    shadow_quote_from_rung,
)
from pmm2.universe.metadata import EnrichedMarket

logger = structlog.get_logger(__name__)


class PMM2Runtime:
    """PMM-2 runtime — manages all concurrent loops.

    Coordinates:
    - Queue estimator (order queue dynamics)
    - Market EV scorer (spread, arb, liq, rebate, costs)
    - Capital allocator (greedy bundle selection)
    - Persistence optimizer (order action decisions)
    - Quote planner (bundle → concrete quotes)
    - Diff engine (target vs live → mutations)
    - V1 bridge (mutations → execution)
    """

    def __init__(self, config: PMM2Config, db, bridge):
        """Initialize PMM-2 runtime.

        Args:
            config: PMM2Config from config file
            db: Database instance (pmm1.storage.database.Database)
            bridge: V1Bridge instance
        """
        self.config = config
        self.db = db
        self.bridge = bridge

        # Components
        self.queue_estimator = QueueEstimator()
        self.fill_hazard = FillHazard()
        self.depletion_calc = DepletionCalculator()
        self.scorer = MarketEVScorer(db, self.fill_hazard, self.queue_estimator)
        allocator_constraints = AllocationConstraints(
            total_capital=100.0,
            total_slots=config.max_slots_total,
            per_market_cap_frac=config.per_market_cap_nav,
            per_market_cap_floor=config.per_market_cap_floor,
            per_event_cap_frac=config.per_event_cap_nav,
            active_cap_frac=config.effective_active_cap_pct,
        )
        adjusted_scorer = AdjustedScorer(
            corr_lambda=config.corr_penalty_lambda / 100.0,
            churn_phi=config.churn_penalty_phi / 100.0,
            queue_psi=config.queue_uncertainty_psi / 100.0,
        )
        self.allocator = CapitalAllocator(
            nav=100.0,
            constraints=allocator_constraints,
            scorer=adjusted_scorer,
            min_positive_return_bps=config.min_positive_return_bps,
        )

        # Persistence optimizer components
        state_machine = StateMachine()
        action_calculator = ActionEVCalculator(self.fill_hazard)
        hysteresis_config = HysteresisConfig(
            base_usdc=config.hysteresis_base_usdc,
            scoring_extra=config.scoring_extra_usdc,
            eta_extra=config.eta_extra_usdc,
        )
        hysteresis_gate = HysteresisGate(hysteresis_config)
        warmup_estimator = WarmupEstimator()

        self.persistence = PersistenceOptimizer(
            state_machine, action_calculator, hysteresis_gate, warmup_estimator
        )

        self.planner = QuotePlanner(max_reprices_per_minute=config.max_reprices_per_minute)
        self.diff_engine = DiffEngine()

        # Shadow mode components (always initialize, active when shadow_mode=True)
        self.shadow_logger = ShadowLogger(db)
        self.counterfactual_engine = CounterfactualEngine(self.shadow_logger)
        self.shadow_dashboard = ShadowDashboard(self.counterfactual_engine)
        self.last_milestone_reported = 0  # Track milestone reports
        self.last_daily_report_time = 0.0  # Track daily shadow reports

        # State
        self.enriched_universe: list[EnrichedMarket] = []
        self.current_plan: dict[str, Any] = {}  # condition_id → TargetQuotePlan
        self.running = False
        self.nav = 100.0
        self.tasks: list[asyncio.Task] = []
        self._recent_v1_cancel_count = 0
        self.bot_state = None
        self.controlled_markets: set[str] = set()
        self.last_execution_summary: dict[str, Any] = {
            "attempted_mutations": 0,
            "executed": 0,
            "failed": 0,
            "shadow": config.shadow_mode,
        }
        self.last_cycle_summary: dict[str, Any] = {}
        self.last_canary_summary: dict[str, Any] = self._blank_canary_summary()

        logger.info(
            "pmm2_runtime_initialized",
            shadow_mode=config.shadow_mode,
            stage=config.stage_name,
            controller=config.controller_label,
            live_capital_pct=config.live_capital_pct,
            effective_active_cap_pct=config.effective_active_cap_pct,
            canary_enabled=config.canary.enabled,
            allocator_interval=config.allocator_interval_sec,
            max_markets=config.max_markets_active,
        )

    async def start(self, bot_state, settings):
        """Start all PMM-2 loops as background tasks.

        Args:
            bot_state: V1 bot state object (has wallet, order_tracker, etc.)
            settings: V1 settings object (has config, etc.)

        Returns:
            List of asyncio tasks
        """
        if self.running:
            logger.warning("pmm2_runtime_already_running")
            return self.tasks

        self.running = True
        self.bot_state = bot_state

        # Update NAV from bot state
        self.nav = self._get_nav(bot_state)
        self._apply_allocator_budget(self.nav)

        # Start all concurrent loops
        self.tasks = [
            asyncio.create_task(self._fast_loop(bot_state)),
            asyncio.create_task(self._medium_loop(bot_state, settings)),
            asyncio.create_task(self._allocator_loop(bot_state, settings)),
            asyncio.create_task(self._slow_loop(bot_state, settings)),
        ]

        logger.info(
            "pmm2_runtime_started",
            tasks=len(self.tasks),
            nav=self.nav,
        )

        return self.tasks

    async def stop(self):
        """Stop all loops gracefully."""
        if not self.running:
            return

        self.running = False

        # Wait for all tasks to complete
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

        logger.info("pmm2_runtime_stopped")

    # ────────────────────────────────────────────────────────────────
    # Event-driven callbacks (called from WS handlers in main.py)
    # ────────────────────────────────────────────────────────────────

    def on_book_delta(
        self, token_id: str, price: float, old_size: float, new_size: float
    ):
        """Called on every market WS book delta.

        Args:
            token_id: token ID
            price: price level that changed
            old_size: previous size at this price
            new_size: new size at this price
        """
        try:
            self.queue_estimator.update_from_book(token_id, price, old_size, new_size)
        except Exception as e:
            logger.error("on_book_delta_error", error=str(e))

    def on_fill(self, order_id: str, fill_size: float, fill_price: float):
        """Called on every user WS fill.

        Args:
            order_id: order ID that filled
            fill_size: size filled
            fill_price: fill price
        """
        try:
            self.queue_estimator.update_from_fill(order_id, fill_size)
            # TODO: Record fill for markout calculation
            # self.allocator.circuit_breaker.record_fill_markout(...)
        except Exception as e:
            logger.error("on_fill_error", error=str(e))

    def on_order_live(
        self,
        order_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        book_depth: float,
    ):
        """Called when our order goes live.

        Args:
            order_id: order ID
            token_id: token ID
            side: BUY or SELL
            price: order price
            size: order size
            book_depth: visible size at this price level
        """
        try:
            self.queue_estimator.initialize_order(
                order_id, token_id, side, price, size, book_depth
            )
            self.persistence.sm.add_order(order_id, "", token_id, side, price, size)
        except Exception as e:
            logger.error("on_order_live_error", error=str(e))

    def on_order_canceled(self, order_id: str):
        """Called when our order is canceled.

        Args:
            order_id: order ID
        """
        try:
            self.queue_estimator.remove_order(order_id)
            self.persistence.sm.remove_order(order_id)
            self._recent_v1_cancel_count += 1
        except Exception as e:
            logger.error("on_order_canceled_error", error=str(e))

    # ────────────────────────────────────────────────────────────────
    # Fast loop: 250ms (queue state recomputation)
    # ────────────────────────────────────────────────────────────────

    async def _fast_loop(self, bot_state):
        """Recompute queue states, ETAs, fill probabilities.

        Runs every 250ms (configurable via queue_update_ms).
        """
        while self.running:
            try:
                # Recompute all queue metrics
                self.queue_estimator.recompute_metrics()

                # Sync queue info to persistence state machine
                for oid, qs in self.queue_estimator.states.items():
                    self.persistence.sm.update_queue(
                        oid, qs.est_ahead_mid, qs.eta_sec, qs.fill_prob_30s
                    )

            except Exception as e:
                logger.error("pmm2_fast_loop_error", error=str(e))

            await asyncio.sleep(self.config.queue_update_ms / 1000.0)

    # ────────────────────────────────────────────────────────────────
    # Medium loop: 10s (market EV refresh)
    # ────────────────────────────────────────────────────────────────

    async def _medium_loop(self, bot_state, settings):
        """Refresh market EV components and bundle values.

        Runs every 10s (configurable via medium_loop_sec).
        """
        while self.running:
            try:
                # For each active market, re-score bundles
                for cid, plan in list(self.current_plan.items()):
                    market = self._find_market(cid)
                    if market:
                        # Re-score this market
                        bundles = await self.scorer.score_market(
                            market,
                            self.nav,
                            per_market_cap_usdc=self.allocator.checker.per_market_cap(self.nav),
                        )
                        # Note: we don't reallocate here, just refresh scores
                        # The allocator loop will pick up new scores on next cycle

                # Persist queue states to database
                await self.queue_estimator.persist(self.db)

            except Exception as e:
                logger.error("pmm2_medium_loop_error", error=str(e))

            await asyncio.sleep(self.config.medium_loop_sec)

    # ────────────────────────────────────────────────────────────────
    # Allocator loop: 60s (full allocation cycle)
    # ────────────────────────────────────────────────────────────────

    async def _allocator_loop(self, bot_state, settings):
        """Full allocation cycle: score → allocate → plan → diff → execute.

        This is the main decision loop:
        1. Capture V1 state snapshot (shadow mode)
        2. Score all markets in universe
        3. Run capital allocator (greedy selection)
        4. Generate quote plans from funded bundles
        5. Diff target vs live orders
        6. Execute mutations via V1 bridge (or log in shadow mode)
        7. Compare counterfactual vs V1 (shadow mode)
        8. Persist decisions

        Runs every 60s (configurable via allocator_interval_sec).
        """
        while self.running:
            try:
                controlled_markets_before = set(self.controlled_markets)

                # 0. Capture V1 state snapshot (for shadow mode comparison)
                shadow_market_contexts = self._build_shadow_market_contexts(bot_state)
                v1_snapshot = V1StateSnapshot.capture(
                    bot_state,
                    market_contexts=shadow_market_contexts,
                    queue_estimator=self.queue_estimator,
                    fill_hazard=self.fill_hazard,
                    allocator_interval_sec=self.config.allocator_interval_sec,
                )
                v1_snapshot["cancel_count_recent"] = self._recent_v1_cancel_count
                v1_snapshot["cycle_minutes"] = self.config.allocator_interval_sec / 60.0
                
                # 1. Enrich depth from V1 book snapshots, then score
                all_bundles = []
                per_market_cap_usdc = self.allocator.checker.per_market_cap(self.nav)
                for market in self.enriched_universe:
                    # Populate depth_at_best from V1 book (T1-01 wiring)
                    if hasattr(bot_state, 'book_manager'):
                        book = bot_state.book_manager.get(market.token_id_yes)
                        if book:
                            bb = book.get_best_bid()
                            ba = book.get_best_ask()
                            market.depth_at_best_bid = float(bb.size) if bb else 0.0
                            market.depth_at_best_ask = float(ba.size) if ba else 0.0
                    bundles = await self.scorer.score_market(
                        market,
                        self.nav,
                        per_market_cap_usdc=per_market_cap_usdc,
                    )
                    all_bundles.extend(bundles)

                logger.info(
                    "allocator_cycle_bundles_scored",
                    universe_size=len(self.enriched_universe),
                    bundles=len(all_bundles),
                )

                # 2. Run allocator against ACTUAL V1 live state, not PMM-2's
                # previous shadow plan. Otherwise shadow scoring becomes
                # self-referential and overstates market-selection divergence.
                current_markets = set(v1_snapshot.get("markets", []))
                event_clusters = {
                    m.condition_id: m.event_id for m in self.enriched_universe
                }
                queue_uncertainties = {
                    oid: qs.queue_uncertainty
                    for oid, qs in self.queue_estimator.states.items()
                }
                current_allocations = {}
                for order in v1_snapshot.get("orders", []):
                    cid = order.get("condition_id")
                    if not cid:
                        continue
                    size = float(order.get("size", 0.0) or 0.0)
                    price = float(order.get("price", 0.0) or 0.0)
                    side = order.get("side", "")
                    if size <= 0:
                        continue
                    capital = size * price if side == "BUY" else size * max(0.0, 1.0 - price)
                    current_allocations[cid] = current_allocations.get(cid, 0.0) + capital

                plan = await self.allocator.run_allocation_cycle(
                    scored_bundles=all_bundles,
                    current_markets=current_markets,
                    event_clusters=event_clusters,
                    queue_uncertainties=queue_uncertainties,
                    net_exposures={},
                    current_allocations=current_allocations,
                )

                logger.info(
                    "allocator_cycle_allocation_complete",
                    funded_bundles=len(plan.funded_bundles),
                    capital_used=plan.total_capital_used,
                )

                # 3. Generate quote plans from funded bundles
                new_plans = {}
                bundles_by_market: dict[str, list] = {}

                # Group bundles by condition_id
                for bundle in plan.funded_bundles:
                    cid = bundle.market_condition_id
                    bundles_by_market.setdefault(cid, []).append(bundle)

                for cid, bundles in bundles_by_market.items():
                    market = self._find_market(cid)
                    if market:
                        target = self.planner.plan_market(
                            bundles,
                            market.token_id_yes,
                            market.token_id_no,
                            cid,
                            market.is_neg_risk,
                            float(market.tick_size or "0.01"),
                        )
                        new_plans[cid] = target

                logger.info(
                    "allocator_cycle_plans_generated",
                    markets=len(new_plans),
                )

                # 4. Diff target vs live orders & collect all mutations
                all_mutations = []
                for cid, target in new_plans.items():
                    market_orders = self._get_live_market_orders(bot_state, cid)
                    matchable_orders = market_orders
                    foreign_orders = []

                    if self.config.is_live:
                        matchable_orders, foreign_orders = self._partition_controlled_orders(
                            market_orders
                        )

                    # Get persistence decisions for these orders
                    persistence_decisions = self.persistence.decide_all(
                        live_orders=[o.order_id for o in matchable_orders if hasattr(o, "order_id")],
                        reservation_prices={},
                        target_prices={},
                        depletion_rates={},
                        inventory_skews={},
                    )

                    # Diff to generate mutations
                    mutations = self.diff_engine.diff(
                        target, matchable_orders, persistence_decisions
                    )
                    if foreign_orders:
                        reason = (
                            "handoff_to_pmm2_control"
                            if cid not in controlled_markets_before
                            else "foreign_order_in_pmm2_market"
                        )
                        mutations.extend(
                            self._build_force_cancel_mutations(
                                foreign_orders,
                                condition_id=cid,
                                reason=reason,
                            )
                        )

                    # Check reprice rate limit
                    if mutations and not self.planner.can_reprice(cid):
                        logger.warning(
                            "allocator_cycle_reprice_rate_limited",
                            condition_id=cid,
                        )
                        continue

                    # Collect mutations for batch execution
                    if mutations:
                        all_mutations.extend(mutations)

                if self.config.is_live:
                    dropped_markets = controlled_markets_before - set(new_plans)
                    for cid in sorted(dropped_markets):
                        market_orders = self._get_live_market_orders(bot_state, cid)
                        if not market_orders:
                            continue
                        cancel_plan = TargetQuotePlan(condition_id=cid)
                        all_mutations.extend(
                            self.diff_engine.diff(
                                cancel_plan,
                                market_orders,
                                persistence_decisions={},
                            )
                        )

                # 5. Execute mutations through bridge (respects shadow mode)
                result = {
                    "executed": 0,
                    "skipped": 0,
                    "failed": 0,
                    "shadow": self.config.shadow_mode,
                    "details": [],
                }
                if all_mutations:
                    result = await self.bridge.execute_mutations(all_mutations)
                    logger.info(
                        "pmm2_allocation_executed",
                        controller=self.config.controller_label,
                        stage=self.config.stage_name,
                        total_mutations=len(all_mutations),
                        **result,
                    )
                self.last_execution_summary = {
                    "attempted_mutations": len(all_mutations),
                    "executed": result.get("executed", 0),
                    "skipped": result.get("skipped", 0),
                    "failed": result.get("failed", 0),
                    "shadow": result.get("shadow", self.config.shadow_mode),
                    "controller": self.config.controller_label,
                    "stage": self.config.stage_name,
                }

                # Update current plan
                self.current_plan = new_plans
                if not self.config.is_live:
                    self.controlled_markets = set()
                elif result.get("failed", 0) > 0:
                    self.controlled_markets = controlled_markets_before
                    logger.warning(
                        "pmm2_control_handoff_preserved_previous_markets",
                        failed_mutations=result.get("failed", 0),
                        previous_controlled_markets=sorted(controlled_markets_before),
                    )
                else:
                    self.controlled_markets = set(new_plans)

                # 6. Shadow mode: counterfactual comparison and logging
                if self.config.shadow_mode:
                    pmm2_plan = self._build_shadow_plan_summary(
                        plan,
                        new_plans,
                        all_mutations,
                        shadow_market_contexts,
                    )

                    # Run counterfactual comparison
                    comparison = self.counterfactual_engine.compare_cycle(v1_snapshot, pmm2_plan)

                    # Log full cycle
                    cycle_data = {
                        "timestamp": v1_snapshot.get("timestamp"),
                        "v1_state": v1_snapshot,
                        "pmm2_plan": pmm2_plan,
                        "comparison": comparison,
                        "v1_markets": v1_snapshot.get("markets", []),
                        "pmm2_markets": pmm2_plan["markets"],
                        "v1_orders": v1_snapshot.get("orders", []),
                        "pmm2_mutations": pmm2_plan["mutations"],
                        "ev_breakdown": [
                            {"condition_id": b.market_condition_id, "ev_bps": b.marginal_return * 10000.0}
                            for b in plan.funded_bundles
                        ],
                        "allocator_output": {
                            "funded_bundles": len(plan.funded_bundles),
                            "total_capital_used": plan.total_capital_used,
                            "reward_markets_funded": plan.reward_markets_funded,
                            "skipped_bundles": list(plan.skipped_bundles),
                        },
                    }

                    self.shadow_logger.log_allocation_cycle(cycle_data)
                    await self.shadow_logger.persist_allocation_cycle(cycle_data)

                    # Check for milestone reports (every 100 cycles)
                    cycle_num = self.counterfactual_engine.cycle_count
                    if cycle_num % 100 == 0 and cycle_num > self.last_milestone_reported:
                        await self.shadow_dashboard.send_milestone_report(cycle_num)
                        self.last_milestone_reported = cycle_num

                    logger.info(
                        "shadow_cycle_logged",
                        cycle=cycle_num,
                        ev_delta_usdc=comparison.get("ev_delta_usdc", 0.0),
                        ready_for_live=self.counterfactual_engine.is_ready_for_live(),
                    )

                    # Reset recent V1 cancel counter after each comparison window.
                    self._recent_v1_cancel_count = 0

                # 7. Persist decisions
                await self.allocator.persist_decisions(self.db, plan)
                await self.scorer.persist_scores(all_bundles)

                logger.info(
                    "pmm2_allocator_cycle_complete",
                    markets=len(new_plans),
                    bundles=len(plan.funded_bundles),
                    capital_used=plan.total_capital_used,
                    controlled_markets=len(self.controlled_markets),
                )
                self.last_cycle_summary = {
                    "markets": len(new_plans),
                    "funded_bundles": len(plan.funded_bundles),
                    "capital_used": plan.total_capital_used,
                    "mutations": len(all_mutations),
                    "controlled_markets": len(self.controlled_markets),
                }

            except Exception as e:
                logger.error("pmm2_allocator_loop_error", error=str(e), exc_info=True)

            await asyncio.sleep(self.config.allocator_interval_sec)

    # ────────────────────────────────────────────────────────────────
    # Slow loop: 5min (universe refresh)
    # ────────────────────────────────────────────────────────────────

    async def _slow_loop(self, bot_state, settings):
        """Universe refresh, metadata updates, depletion rate calibration.

        Runs every 5 minutes (configurable via universe_refresh_sec).
        """
        while self.running:
            try:
                # Refresh enriched universe
                from pmm2.universe.build import build_enriched_universe

                # Get clients from bot_state (or create new ones)
                gamma = getattr(bot_state, "gamma_client", None)
                rewards = getattr(bot_state, "rewards_client", None)

                if gamma and rewards:
                    from pmm2.universe.scorer import UniverseScorer

                    full_universe = await build_enriched_universe(
                        gamma, rewards, settings
                    )
                    selector = UniverseScorer()
                    candidate_count = min(
                        len(full_universe),
                        max(self.config.max_markets_active * 8, self.config.max_markets_active),
                    )
                    candidate_universe = selector.select_top(full_universe, candidate_count)
                    self.last_canary_summary = self._summarize_canary_universe(
                        candidate_universe
                    )
                    self.enriched_universe = self._select_live_universe(candidate_universe)
                    logger.info(
                        "universe_refreshed",
                        universe_size=len(self.enriched_universe),
                        raw_universe_size=len(full_universe),
                        canary_eligible=self.last_canary_summary.get("eligible_markets", 0),
                        canary_selected=self.last_canary_summary.get("selected_markets", 0),
                        canary_rejections=self.last_canary_summary.get("rejection_counts", {}),
                    )
                else:
                    logger.warning(
                        "universe_refresh_skipped_missing_clients",
                        has_gamma=gamma is not None,
                        has_rewards=rewards is not None,
                    )

                # Update NAV from wallet
                new_nav = self._get_nav(bot_state)
                if abs(new_nav - self.nav) > 1.0:  # NAV changed by more than $1
                    old_nav = self.nav
                    self.nav = new_nav
                    self._apply_allocator_budget(self.nav)
                    logger.info(
                        "nav_updated",
                        old_nav=old_nav,
                        new_nav=new_nav,
                        effective_active_cap_pct=self.config.effective_active_cap_pct,
                    )

                # TODO: Refresh depletion rates from historical data
                # active_tokens = list(self.queue_estimator.states.keys())
                # await self.depletion_calc.calibrate(self.db, active_tokens)

                # Shadow mode: send daily report (once per 24 hours)
                if self.config.shadow_mode:
                    current_time = time.time()
                    time_since_last_report = current_time - self.last_daily_report_time
                    
                    # Send report every 24 hours (86400 seconds)
                    if time_since_last_report >= 86400:
                        await self.shadow_dashboard.send_daily_shadow_report()
                        self.last_daily_report_time = current_time
                        logger.info("shadow_daily_report_sent")

                logger.info("pmm2_slow_cycle_complete")

            except Exception as e:
                logger.error("pmm2_slow_loop_error", error=str(e))

            await asyncio.sleep(self.config.universe_refresh_sec)

    # ────────────────────────────────────────────────────────────────
    # Helper methods
    # ────────────────────────────────────────────────────────────────

    def _blank_canary_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.config.canary.enabled,
            "stage": self.config.stage_name,
            "controller": self.config.controller_label,
            "evaluated_markets": 0,
            "eligible_markets": 0,
            "selected_markets": 0,
            "rejected_markets": 0,
            "preview_condition_ids": [],
            "selected_condition_ids": [],
            "rejection_counts": {},
        }

    def _apply_allocator_budget(self, nav: float) -> None:
        """Apply runtime capital limits for shadow, canary, or full live mode."""
        self.allocator.update_nav(nav)
        self.allocator.constraints.total_slots = self.config.max_slots_total
        self.allocator.constraints.per_market_cap_frac = self.config.per_market_cap_nav
        self.allocator.constraints.per_market_cap_floor = self.config.per_market_cap_floor
        self.allocator.constraints.per_event_cap_frac = self.config.per_event_cap_nav
        self.allocator.constraints.active_cap_frac = self.config.effective_active_cap_pct

    def _evaluate_canary_market(self, market: EnrichedMarket) -> tuple[bool, list[str]]:
        """Return whether a market is eligible for live PMM-2 canary control."""
        cfg = self.config.canary
        reasons: list[str] = []

        if not market.active or not market.accepting_orders:
            reasons.append("market_not_live")
        if cfg.require_reward_eligible and not market.reward_eligible:
            reasons.append("not_reward_eligible")
        if cfg.exclude_neg_risk and market.is_neg_risk:
            reasons.append("neg_risk_excluded")
        if cfg.require_clean_outcomes and market.has_placeholder_outcomes:
            reasons.append("placeholder_outcomes")
        if market.ambiguity_score > cfg.max_ambiguity_score:
            reasons.append("ambiguity_above_limit")
        if market.hours_to_resolution < cfg.min_hours_to_resolution:
            reasons.append("too_close_to_resolution")
        if market.volume_24h < cfg.min_volume_24h:
            reasons.append("insufficient_volume_24h")
        if market.liquidity < cfg.min_liquidity:
            reasons.append("insufficient_liquidity")
        if market.spread_cents <= 0.0 or market.spread_cents > cfg.max_spread_cents:
            reasons.append("spread_outside_limit")

        return (len(reasons) == 0, reasons)

    def _summarize_canary_universe(
        self,
        markets: list[EnrichedMarket],
    ) -> dict[str, Any]:
        """Compute a stable operator-facing summary of live canary eligibility."""
        if not markets:
            return self._blank_canary_summary()

        rejection_counts: Counter[str] = Counter()
        eligible: list[EnrichedMarket] = []

        for market in markets:
            is_eligible, reasons = self._evaluate_canary_market(market)
            if is_eligible:
                eligible.append(market)
                continue
            rejection_counts.update(reasons)

        preview = eligible[: self.config.canary.max_markets]
        return {
            "enabled": self.config.canary.enabled,
            "stage": self.config.stage_name,
            "controller": self.config.controller_label,
            "evaluated_markets": len(markets),
            "eligible_markets": len(eligible),
            "selected_markets": len(preview) if self.config.is_canary_live else 0,
            "rejected_markets": len(markets) - len(eligible),
            "preview_condition_ids": [market.condition_id for market in preview],
            "selected_condition_ids": [market.condition_id for market in preview]
            if self.config.is_canary_live
            else [],
            "rejection_counts": dict(rejection_counts),
        }

    def _select_live_universe(
        self,
        candidate_universe: list[EnrichedMarket],
    ) -> list[EnrichedMarket]:
        """Apply live canary restrictions without changing shadow-mode analysis."""
        if not self.config.is_canary_live:
            return candidate_universe

        filtered: list[EnrichedMarket] = []
        for market in candidate_universe:
            is_eligible, _ = self._evaluate_canary_market(market)
            if is_eligible:
                filtered.append(market)
            if len(filtered) >= self.config.canary.max_markets:
                break

        logger.info(
            "pmm2_canary_universe_applied",
            stage=self.config.stage_name,
            requested_max_markets=self.config.canary.max_markets,
            selected_markets=len(filtered),
            selected_condition_ids=[market.condition_id for market in filtered],
        )
        return filtered

    def _get_live_market_orders(self, bot_state, condition_id: str) -> list[Any]:
        if not hasattr(bot_state, "order_tracker"):
            logger.warning("bot_state_missing_order_tracker")
            return []
        live = bot_state.order_tracker.get_active_orders(token_id=None)
        return [
            order
            for order in live
            if getattr(order, "condition_id", "") == condition_id
        ]

    def _is_pmm2_controlled_order(self, order: Any) -> bool:
        strategy = str(getattr(order, "strategy", "") or "")
        return strategy.startswith("pmm2_")

    def _partition_controlled_orders(
        self,
        orders: list[Any],
    ) -> tuple[list[Any], list[Any]]:
        """Separate PMM-2-managed orders from foreign/V1 orders on a controlled market."""
        pmm2_orders: list[Any] = []
        foreign_orders: list[Any] = []
        for order in orders:
            if self._is_pmm2_controlled_order(order):
                pmm2_orders.append(order)
            else:
                foreign_orders.append(order)
        return pmm2_orders, foreign_orders

    def _build_force_cancel_mutations(
        self,
        orders: list[Any],
        *,
        condition_id: str,
        reason: str,
    ) -> list[OrderMutation]:
        mutations: list[OrderMutation] = []
        for order in orders:
            order_id = getattr(order, "order_id", "")
            if not order_id:
                continue
            mutations.append(
                OrderMutation(
                    action="cancel",
                    order_id=order_id,
                    token_id=getattr(order, "token_id", ""),
                    condition_id=condition_id,
                    reason=reason,
                )
            )
        return mutations

    def get_controlled_markets(self) -> set[str]:
        if not self.config.is_live:
            return set()
        return set(self.controlled_markets)

    def should_v1_skip_market(self, condition_id: str) -> bool:
        return condition_id in self.get_controlled_markets()

    def get_status(self) -> dict[str, Any]:
        """Return operator-facing PMM-2 runtime status for logs and ops snapshots."""
        active_pmm2_orders = 0
        if self.bot_state is not None and hasattr(self.bot_state, "order_tracker"):
            try:
                active_pmm2_orders = sum(
                    1
                    for order in self.bot_state.order_tracker.get_active_orders(token_id=None)
                    if self._is_pmm2_controlled_order(order)
                )
            except Exception:
                active_pmm2_orders = 0

        return {
            "enabled": self.config.enabled,
            "shadow_mode": self.config.shadow_mode,
            "live_enabled": self.config.live_enabled,
            "is_live": self.config.is_live,
            "is_canary_live": self.config.is_canary_live,
            "controller": self.config.controller_label,
            "strategy": self.config.controller_strategy,
            "stage": self.config.stage_name,
            "live_capital_pct": self.config.live_capital_pct,
            "effective_active_cap_pct": self.config.effective_active_cap_pct,
            "bridge_has_order_manager": getattr(self.bridge, "order_manager", None) is not None,
            "controlled_market_count": len(self.controlled_markets),
            "controlled_markets": sorted(self.controlled_markets),
            "active_pmm2_orders": active_pmm2_orders,
            "canary": dict(self.last_canary_summary),
            "last_execution": dict(self.last_execution_summary),
            "last_cycle": dict(self.last_cycle_summary),
            "ready_for_live": self.counterfactual_engine.is_ready_for_live()
            if self.config.shadow_mode
            else None,
        }

    def _find_market(self, condition_id: str) -> EnrichedMarket | None:
        """Find market in universe by condition_id.

        Args:
            condition_id: market condition ID

        Returns:
            EnrichedMarket if found, None otherwise
        """
        for m in self.enriched_universe:
            if m.condition_id == condition_id:
                return m
        return None

    def _build_shadow_market_contexts(self, bot_state) -> dict[str, any]:
        """Merge PMM-2 universe metadata with PMM-1 active market truth."""

        contexts: dict[str, any] = {}
        for market in self.enriched_universe:
            context = market_context_from_object(market)
            contexts[market.condition_id] = merge_market_contexts(
                contexts.get(market.condition_id),
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
            contexts[condition_id] = merge_market_contexts(
                contexts.get(condition_id),
                context,
            ) or context

        return contexts

    def _build_shadow_plan_summary(
        self,
        plan,
        new_plans: dict[str, any],
        all_mutations: list,
        shadow_market_contexts: dict[str, any],
    ) -> dict[str, any]:
        """Build an apples-to-apples PMM-2 plan summary for shadow comparison."""

        market_evaluations = []
        for condition_id, target_plan in new_plans.items():
            shadow_quotes = []
            for rung in target_plan.ladder:
                quote = shadow_quote_from_rung(rung)
                if quote is not None:
                    shadow_quotes.append(quote)

            evaluation = evaluate_quote_set(
                shadow_market_contexts.get(condition_id),
                shadow_quotes,
            )
            market_evaluations.append(evaluation)

        aggregate = aggregate_market_evaluations(market_evaluations)
        cycle_minutes = self.config.allocator_interval_sec / 60.0
        target_order_count = sum(len(tp.ladder) for tp in new_plans.values())
        projected_cancel_count = sum(
            1 for mutation in all_mutations if getattr(mutation, "action", "") == "cancel"
        )

        return {
            "markets": list(new_plans.keys()),
            "bundles": [
                {
                    "market_condition_id": bundle.market_condition_id,
                    "bundle_type": bundle.bundle_type,
                    "capital_usdc": bundle.capital_usdc,
                    "spread_ev_usdc": bundle.spread_ev,
                    "reward_ev_usdc": bundle.liq_ev,
                    "rebate_ev_usdc": bundle.rebate_ev,
                    "total_value_usdc": bundle.total_value,
                    "expected_return_bps": bundle.marginal_return * 10000.0,
                    "is_reward_eligible": getattr(bundle, "is_reward_eligible", False),
                }
                for bundle in plan.funded_bundles
            ],
            "mutations": [
                {
                    "action": mutation.action,
                    "condition_id": mutation.condition_id,
                    "token_id": mutation.token_id,
                    "side": mutation.side,
                    "price": mutation.price,
                    "size": mutation.size,
                    "order_id": mutation.order_id,
                    "reason": mutation.reason,
                }
                for mutation in all_mutations
            ],
            "market_evaluations": [
                evaluation.model_dump() for evaluation in market_evaluations
            ],
            "target_order_count": target_order_count,
            "live_order_minutes": target_order_count * cycle_minutes,
            "cycle_minutes": cycle_minutes,
            "projected_cancel_count": projected_cancel_count,
            "total_capital_usdc": plan.total_capital_used,
            "total_expected_ev_usdc": aggregate["total_ev_usdc"],
            "bundle_total_value_usdc": sum(bundle.total_value for bundle in plan.funded_bundles),
            "total_reward_ev_usdc": aggregate["total_reward_ev_usdc"],
            "total_rebate_ev_usdc": aggregate["total_rebate_ev_usdc"],
            "reward_market_count": aggregate["reward_market_count"],
            "reward_pair_mass_total": aggregate["reward_pair_mass_total"],
            "ev_sample_valid": aggregate["ev_sample_valid"],
            "invalid_markets": aggregate["invalid_markets"],
        }

    def _get_nav(self, bot_state) -> float:
        """Get current NAV from bot state.

        Args:
            bot_state: V1 bot state object

        Returns:
            NAV in USDC (default 100.0 if not available)
        """
        # Try different possible attributes
        if hasattr(bot_state, "nav"):
            return float(bot_state.nav)
        if hasattr(bot_state, "wallet_balance"):
            return float(bot_state.wallet_balance)
        if hasattr(bot_state, "total_equity"):
            return float(bot_state.total_equity)
        if hasattr(bot_state, "inventory_manager") and hasattr(bot_state.inventory_manager, "get_total_nav_estimate"):
            return float(bot_state.inventory_manager.get_total_nav_estimate())

        # Fallback
        logger.warning("nav_not_available_using_default")
        return 100.0

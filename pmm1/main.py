"""PMM-1 Main Loop — production market-making bot entry point from §20.

Startup sequence (§10):
1. Geoblock check
2. Load secrets from environment
3. Sync server time
4. Fetch universe from Gamma + sampling markets
5. Fetch market metadata: tick size, neg-risk, fee rate
6. Load current open orders and balances
7. Connect market WS
8. Connect user WS
9. Start REST heartbeat loop
10. Enter WARMUP → QUOTING
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

from pmm1.api.clob_private import (
    ClobAuthError,
    ClobPausedError,
    ClobPrivateClient,
    ClobRateLimitError,
    ClobRestartError,
    CreateOrderRequest,
    OrderSide,
    OrderType,
)
from pmm1.api.clob_public import ClobPublicClient
from pmm1.api.data_api import DataApiClient
from pmm1.api.gamma import GammaClient
from pmm1.api.geoblock import GeoblockError, check_geoblock
from pmm1.backtest.recorder import LiveRecorder
from pmm1.execution.batcher import OrderBatcher
from pmm1.execution.order_manager import OrderManager
from pmm1.execution.reconciler import Reconciler
from pmm1.logging import get_logger, setup_logging
from pmm1.risk.drawdown import DrawdownGovernor
from pmm1.risk.kill_switch import KillSwitch
from pmm1.risk.limits import RiskLimits
from pmm1.risk.resolution import ResolutionRiskManager
from pmm1.settings import Settings, load_settings
from pmm1.state.books import BookManager
from pmm1.state.heartbeats import HeartbeatState
from pmm1.state.inventory import InventoryManager
from pmm1.state.orders import OrderTracker
from pmm1.state.positions import PositionTracker
from pmm1.storage.parquet import ParquetWriter
from pmm1.strategy.binary_parity import BinaryParityDetector
from pmm1.strategy.fair_value import FairValueModel
from pmm1.strategy.features import FeatureEngine
from pmm1.strategy.neg_risk_arb import NegRiskArbDetector, NegRiskOutcome
from pmm1.strategy.quote_engine import QuoteEngine
from pmm1.strategy.rewards import RewardEstimator
from pmm1.strategy.universe import MarketMetadata, select_universe
from pmm1.ws.market_ws import MarketWebSocket
from pmm1.ws.user_ws import UserWebSocket
from pmm1.analytics.metrics import MetricsCollector
from pmm1.analytics.pnl import PnLTracker
from pmm1.paper.engine import PaperEngine
from pmm1.paper.logger import PaperLogger

logger: structlog.stdlib.BoundLogger = None  # type: ignore


class BotState:
    """Central bot state container."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mode: str = "STARTUP"  # STARTUP, WARMUP, QUOTING, PAUSED, FLATTEN_ONLY, SHUTDOWN

        # State managers
        self.book_manager = BookManager()
        self.order_tracker = OrderTracker()
        self.position_tracker = PositionTracker()
        self.inventory_manager = InventoryManager(self.position_tracker, self.order_tracker)

        # Strategy
        self.feature_engine = FeatureEngine()
        self.fair_value_model = FairValueModel(settings.pricing)
        self.quote_engine = QuoteEngine(settings.pricing)
        self.parity_detector = BinaryParityDetector()
        self.neg_risk_detector = NegRiskArbDetector()
        self.reward_estimator = RewardEstimator()

        # Risk
        self.risk_limits = RiskLimits(
            settings.risk, self.position_tracker, self.inventory_manager
        )
        self.kill_switch = KillSwitch(
            ws_stale_kill_s=settings.execution.ws_stale_kill_s,
        )
        self.drawdown = DrawdownGovernor(settings.risk)
        self.resolution_risk = ResolutionRiskManager()

        # Analytics
        self.metrics = MetricsCollector()
        self.pnl_tracker = PnLTracker()

        # Market universe
        self.universe: list[MarketMetadata] = []
        self.active_markets: dict[str, MarketMetadata] = {}  # condition_id → metadata
        self.tick_sizes: dict[str, Decimal] = {}  # token_id → tick_size
        self.neg_risk_events: dict[str, list[NegRiskOutcome]] = {}  # event_id → outcomes

        # Sampling (reward-eligible) condition IDs
        self.reward_eligible: set[str] = set()

    def eligible_markets(self) -> list[MarketMetadata]:
        """Get markets eligible for quoting in current mode."""
        if self.mode == "FLATTEN_ONLY":
            return []  # No new quotes
        if self.mode in ("SHUTDOWN", "STARTUP"):
            return []
        return list(self.active_markets.values())


async def run(settings: Settings | None = None) -> None:
    """Main bot entry point."""
    global logger

    if settings is None:
        settings = load_settings()

    setup_logging(
        level="DEBUG" if settings.bot.env != "prod" else "INFO",
        json_output=settings.bot.env == "prod",
    )
    logger = get_logger("pmm1.main")
    logger.info("pmm1_starting", name=settings.bot.name, env=settings.bot.env)

    # ── 1. Geoblock check ──
    try:
        await check_geoblock(settings.api.geoblock_url)
    except GeoblockError as e:
        logger.critical("geoblock_failed", error=str(e))
        sys.exit(1)

    # ── 2. Initialize clients ──
    gamma = GammaClient(settings.api.gamma_url)
    clob_public = ClobPublicClient(settings.api.clob_url)
    clob_private = ClobPrivateClient(
        base_url=settings.api.clob_url,
        api_key=settings.api.api_key,
        api_secret=settings.api.api_secret,
        api_passphrase=settings.api.api_passphrase,
        funder=settings.api.funder,
        wallet_address=settings.wallet.address,
        private_key=settings.wallet.private_key,
        chain_id=settings.wallet.chain_id,
    )
    data_api = DataApiClient(settings.api.data_url)

    # ── 3. Sync server time ──
    try:
        server_time = await clob_public.get_server_time()
        local_time = int(time.time())
        time_offset = server_time - local_time
        logger.info("server_time_synced", offset_s=time_offset)
    except Exception as e:
        logger.warning("server_time_sync_failed", error=str(e))
        time_offset = 0

    # ── Initialize state ──
    state = BotState(settings)

    # ── 4. Fetch universe ──
    logger.info("fetching_universe")
    events = await gamma.get_active_events()
    all_markets: list[MarketMetadata] = []

    for event in events:
        for market in event.markets:
            token_ids = market.token_ids
            if len(token_ids) < 2:
                continue
            md = MarketMetadata(
                condition_id=market.condition_id,
                token_id_yes=token_ids[0],
                token_id_no=token_ids[1] if len(token_ids) > 1 else "",
                event_id=event.id,
                question=market.question,
                slug=market.market_slug,
                active=market.active,
                closed=market.closed,
                enable_order_book=market.enable_order_book,
                neg_risk=market.neg_risk,
                neg_risk_market_id=market.neg_risk_market_id,
                end_date=market.end_date,
                game_start_time=None,
                volume_24h=market.volume_24hr,
                liquidity=market.liquidity,
                spread=market.spread,
                best_bid=market.best_bid,
                best_ask=market.best_ask,
                reward_daily_rate=market.rewards_daily_rate,
                reward_min_size=market.rewards_min_size,
                reward_max_spread=market.rewards_max_spread,
            )
            all_markets.append(md)

    # Fetch reward-eligible markets
    try:
        sampling = await clob_public.get_sampling_markets()
        for sm in sampling:
            state.reward_eligible.add(sm.condition_id)
    except Exception as e:
        logger.warning("sampling_markets_fetch_failed", error=str(e))

    # Mark reward eligibility
    for m in all_markets:
        if m.condition_id in state.reward_eligible:
            m.reward_eligible = True

    # ── 5. Select universe ──
    selected = select_universe(
        all_markets,
        settings.market_filters,
        settings.universe_weights,
        max_markets=settings.bot.max_markets,
    )

    for scored in selected:
        md = scored.metadata
        state.active_markets[md.condition_id] = md
        state.position_tracker.register_market(
            md.condition_id, md.token_id_yes, md.token_id_no,
            neg_risk=md.neg_risk, event_id=md.event_id,
        )

        # Group neg-risk outcomes by event
        if md.neg_risk and md.event_id:
            if md.event_id not in state.neg_risk_events:
                state.neg_risk_events[md.event_id] = []
            state.neg_risk_events[md.event_id].append(NegRiskOutcome(
                condition_id=md.condition_id,
                token_id_yes=md.token_id_yes,
                token_id_no=md.token_id_no,
                index=len(state.neg_risk_events[md.event_id]),
            ))

    logger.info("universe_selected", count=len(state.active_markets))

    # ── 6. Fetch tick sizes ──
    for md in state.active_markets.values():
        for token_id in [md.token_id_yes, md.token_id_no]:
            if token_id:
                try:
                    tick = await clob_public.get_tick_size(token_id)
                    state.tick_sizes[token_id] = tick
                except Exception:
                    state.tick_sizes[token_id] = Decimal("0.01")

    # ── Paper mode setup ──
    paper_mode = settings.bot.paper_mode
    paper_engine: PaperEngine | None = None
    paper_logger: PaperLogger | None = None

    if paper_mode:
        paper_engine = PaperEngine(settings.bot.paper_nav)
        paper_logger = PaperLogger()
        # In paper mode, set virtual balance so NAV is correct
        state.inventory_manager.update_balances(settings.bot.paper_nav, settings.bot.paper_nav)
        logger.info(
            "paper_mode_enabled",
            paper_nav=settings.bot.paper_nav,
        )
        print()
        print("=" * 50)
        print("  PMM-1 PAPER MODE")
        print(f"  Starting NAV: ${settings.bot.paper_nav:.2f}")
        print(f"  Markets: {len(state.active_markets)}")
        print(f"  Strategies: parity={'ON' if settings.strategy.enable_binary_parity else 'OFF'}, "
              f"neg_risk={'ON' if settings.strategy.enable_neg_risk_arb else 'OFF'}, "
              f"mm={'ON' if settings.strategy.enable_market_making else 'OFF'}")
        print("=" * 50)
        print()

    # ── 7. Load open orders and balances ──
    if not paper_mode:
        try:
            balances = await clob_private.get_balances()
            raw_balance = float(balances.get("balance", 0))
            raw_allowance = float(balances.get("allowance", 0))
            # CLOB returns raw USDC units (6 decimals) — convert to dollars
            balance = raw_balance / 1e6 if raw_balance > 1000 else raw_balance
            allowance = raw_allowance / 1e6 if raw_allowance > 1000 else raw_allowance
            state.inventory_manager.update_balances(balance, allowance)
            logger.info("balances_loaded", balance=f"${balance:.2f}", allowance=f"${allowance:.2f}", raw=raw_balance)
        except Exception as e:
            logger.warning("balances_load_failed", error=str(e))

        try:
            open_orders = await clob_private.get_open_orders()
            logger.info("open_orders_loaded", count=len(open_orders))
        except Exception as e:
            logger.warning("open_orders_load_failed", error=str(e))
    else:
        logger.info("paper_mode_skip_balances", paper_nav=settings.bot.paper_nav)

    # ── Initialize managers ──
    order_manager: OrderManager | None = None
    heartbeat: HeartbeatState | None = None
    reconciler: Reconciler | None = None

    if not paper_mode:
        order_manager = OrderManager(
            client=clob_private,
            order_tracker=state.order_tracker,
            order_ttl_s=settings.execution.order_ttl_effective_s,
            post_only=settings.execution.post_only,
        )
        order_manager.set_server_time_offset(server_time if server_time else int(time.time()))

        heartbeat = HeartbeatState(
            client=clob_private,
            interval_s=settings.execution.heartbeat_s,
        )

        reconciler = Reconciler(
            clob_client=clob_private,
            data_client=data_api,
            order_tracker=state.order_tracker,
            position_tracker=state.position_tracker,
            wallet_address=settings.wallet.address,
            reconcile_orders_s=settings.bot.reconcile_orders_s,
            reconcile_positions_s=settings.bot.reconcile_positions_s,
        )

    recorder = LiveRecorder()
    parquet_writer = ParquetWriter(settings.storage.parquet_dir)

    # ── 8 & 9. Connect WebSockets ──
    all_token_ids = []
    for md in state.active_markets.values():
        if md.token_id_yes:
            all_token_ids.append(md.token_id_yes)
        if md.token_id_no:
            all_token_ids.append(md.token_id_no)

    async def on_book_update(token_id: str) -> None:
        """Callback when book is updated from WS."""
        book = state.book_manager.get(token_id)
        if book:
            recorder.record_book_snapshot(token_id, book)

    async def on_trade(token_id: str, price: float) -> None:
        """Callback when a trade occurs."""
        state.feature_engine.record_trade(token_id, price, 0.0, "UNKNOWN")
        recorder.record_trade(token_id, price, 0.0, "UNKNOWN")

    async def on_reconnect() -> None:
        """Callback after user WS reconnect — trigger reconciliation."""
        if reconciler:
            logger.info("post_reconnect_reconciliation")
            await reconciler.full_reconciliation()

    market_ws = MarketWebSocket(
        ws_url=settings.api.ws_market_url,
        book_manager=state.book_manager,
        on_book_update=on_book_update,
        on_trade=on_trade,
    )

    user_ws: UserWebSocket | None = None
    if not paper_mode:
        user_ws = UserWebSocket(
            ws_url=settings.api.ws_user_url,
            api_key=settings.api.api_key,
            api_secret=settings.api.api_secret,
            api_passphrase=settings.api.api_passphrase,
            wallet_address=settings.wallet.address,
            order_tracker=state.order_tracker,
            on_reconnect=on_reconnect,
        )

    # ── 10. Start all background tasks ──
    shutdown_event = asyncio.Event()

    def handle_signal(sig: int, frame: Any) -> None:
        logger.info("shutdown_signal_received", signal=sig)
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    market_ws_task = market_ws.start(all_token_ids)
    if user_ws:
        user_ws_task = user_ws.start()
    if heartbeat:
        heartbeat_task = heartbeat.start()
    if reconciler:
        reconcile_task = reconciler.start()
    recorder_task = recorder.start()

    # Wait briefly for WS connections to establish
    warmup_s = 5.0 if paper_mode else 2.0
    logger.info("warmup_wait", seconds=warmup_s, paper_mode=paper_mode)
    await asyncio.sleep(warmup_s)

    # Initialize drawdown with current NAV
    if paper_mode and paper_engine:
        nav = paper_engine.get_nav(state.book_manager)
    else:
        nav = state.inventory_manager.get_total_nav_estimate()
    state.drawdown.initialize(nav)
    state.risk_limits.update_nav(nav)

    state.mode = "QUOTING"
    logger.info("pmm1_entering_quoting_mode", markets=len(state.active_markets))

    # ── Main quote loop ──
    cycle_count = 0
    quote_interval = settings.bot.quote_cycle_ms / 1000.0

    try:
        while not shutdown_event.is_set():
            cycle_start = time.time()
            cycle_count += 1

            # ── Kill switch check (skip in paper mode) ──
            if not paper_mode:
                state.kill_switch.check_stale_feed(market_ws.seconds_since_last_message)
                state.kill_switch.check_heartbeat(
                    heartbeat.is_healthy, heartbeat.consecutive_failures
                )

                if state.kill_switch.is_triggered:
                    if state.mode != "FLATTEN_ONLY":
                        logger.critical(
                            "kill_switch_active",
                            reasons=[r.value for r in state.kill_switch.active_reasons],
                        )
                        if order_manager:
                            await order_manager.cancel_all()
                        state.mode = "FLATTEN_ONLY"
                    await asyncio.sleep(1.0)
                    continue
                else:
                    # Kill switch cleared — resume quoting
                    if state.mode == "FLATTEN_ONLY":
                        logger.info("kill_switch_cleared_resuming_quoting")
                        state.mode = "QUOTING"

            # ── Drawdown check ──
            if state.drawdown.should_check_daily_reset():
                if paper_mode and paper_engine:
                    nav = paper_engine.get_nav(state.book_manager)
                else:
                    nav = state.inventory_manager.get_total_nav_estimate()
                state.drawdown.reset_daily(nav)

            if paper_mode and paper_engine:
                nav = paper_engine.get_nav(state.book_manager)
            else:
                nav = state.inventory_manager.get_total_nav_estimate()
            dd_state = state.drawdown.update(nav)
            state.risk_limits.update_nav(nav)

            if dd_state.should_flatten_only:
                if state.mode != "FLATTEN_ONLY":
                    logger.critical("drawdown_flatten_only", tier=dd_state.tier.value)
                    if order_manager:
                        await order_manager.cancel_all()
                    if paper_engine:
                        paper_engine.cancel_all_orders()
                    state.mode = "FLATTEN_ONLY"
                    state.kill_switch.trigger_drawdown()
                await asyncio.sleep(1.0)
                continue

            # Adjust risk limits based on drawdown
            if dd_state.should_widen_quotes:
                state.risk_limits.set_dynamic_multiplier(0.5)
            else:
                state.risk_limits.set_dynamic_multiplier(1.0)

            # ── Paper mode: check fills from previous cycle ──
            if paper_mode and paper_engine and paper_logger:
                new_fills = paper_engine.check_fills(state.book_manager)
                for fill in new_fills:
                    paper_logger.log_fill(fill.to_dict())

            # ── Quote each market ──
            markets_quoted = 0
            orders_submitted = 0
            orders_canceled = 0

            for md in state.eligible_markets():
                # Skip if resolution risk says stop
                if not state.resolution_risk.should_quote(md.condition_id):
                    continue

                token_id = md.token_id_yes
                if not token_id:
                    continue

                book = state.book_manager.get(token_id)
                if book is None:
                    continue
                # In paper mode, relax staleness to 120s (books still valid, just quiet markets)
                stale_threshold = 120.0 if paper_mode else 2.0
                if book.age_seconds > stale_threshold:
                    continue

                tick_size = state.tick_sizes.get(token_id, Decimal("0.01"))

                # ── Compute features ──
                features = state.feature_engine.compute(
                    token_id=token_id,
                    book=book,
                    condition_id=md.condition_id,
                    end_date=md.end_date,
                )

                # ── Check arb opportunities first ──
                if settings.strategy.enable_binary_parity:
                    book_no = state.book_manager.get(md.token_id_no)
                    book_no_fresh = book_no and book_no.age_seconds <= stale_threshold if book_no else False
                    if book and book_no and book_no_fresh:
                        arb_orders = state.parity_detector.scan(
                            book, book_no, md.condition_id,
                            md.token_id_yes, md.token_id_no,
                        )
                        if arb_orders and not dd_state.should_pause_taker:
                            if paper_mode and paper_engine and paper_logger:
                                # Log arb detection and submit to paper engine
                                paper_logger.log_arb(
                                    arb_type="binary_parity",
                                    condition_id=md.condition_id,
                                    details={"num_orders": len(arb_orders)},
                                )
                                paper_engine.submit_arb_orders([
                                    {
                                        "token_id": o.token_id,
                                        "condition_id": md.condition_id,
                                        "side": o.side,
                                        "price": o.price,
                                        "size": o.size,
                                        "neg_risk": o.neg_risk,
                                    }
                                    for o in arb_orders
                                ])
                            elif order_manager:
                                arb_reqs = [
                                    CreateOrderRequest(
                                        token_id=o.token_id,
                                        price=o.price,
                                        size=o.size,
                                        side=OrderSide(o.side),
                                        order_type=OrderType.FOK,
                                        neg_risk=o.neg_risk,
                                        post_only=False,
                                    )
                                    for o in arb_orders
                                ]
                                await order_manager.execute_arb(arb_reqs)
                            continue

                # ── Neg-risk arb ──
                if settings.strategy.enable_neg_risk_arb and md.neg_risk and md.event_id:
                    outcomes = state.neg_risk_events.get(md.event_id, [])
                    if len(outcomes) > 1:
                        books = {}
                        for outcome in outcomes:
                            for tid in [outcome.token_id_yes, outcome.token_id_no]:
                                b = state.book_manager.get(tid)
                                if b:
                                    books[tid] = b
                        arb_orders = state.neg_risk_detector.scan_event(
                            outcomes, books, md.event_id
                        )
                        if arb_orders and not dd_state.should_pause_taker:
                            if paper_mode and paper_engine and paper_logger:
                                paper_logger.log_arb(
                                    arb_type="neg_risk",
                                    event_id=md.event_id,
                                    details={"num_orders": len(arb_orders)},
                                )
                                paper_engine.submit_arb_orders([
                                    {
                                        "token_id": o.token_id,
                                        "condition_id": md.condition_id,
                                        "side": o.side,
                                        "price": o.price,
                                        "size": o.size,
                                        "neg_risk": True,
                                    }
                                    for o in arb_orders
                                ])
                            elif order_manager:
                                arb_reqs = [
                                    CreateOrderRequest(
                                        token_id=o.token_id,
                                        price=o.price,
                                        size=o.size,
                                        side=OrderSide(o.side),
                                        order_type=OrderType.FOK,
                                        neg_risk=True,
                                        post_only=False,
                                    )
                                    for o in arb_orders
                                ]
                                await order_manager.execute_arb(arb_reqs)
                            continue

                # ── Market making ──
                if settings.strategy.enable_market_making:
                    # Fair value
                    fv_estimate = state.fair_value_model.compute_fair_value(features)

                    # Inventory
                    pos = state.position_tracker.get(md.condition_id)
                    market_inv = pos.net_exposure if pos else 0.0
                    cluster_inv = state.position_tracker.get_event_net_exposure(md.event_id) if md.event_id else 0.0

                    # Reward EV
                    reward_ev = state.reward_estimator.compute_reward_ev_for_universe(md.condition_id)

                    # Quote
                    quote_intent = state.quote_engine.compute_quote(
                        token_id=token_id,
                        features=features,
                        fair_value=fv_estimate.fair_value,
                        haircut=fv_estimate.haircut,
                        confidence=fv_estimate.confidence,
                        market_inventory=market_inv,
                        cluster_inventory=cluster_inv,
                        tick_size=float(tick_size),
                        reward_ev=reward_ev,
                        neg_risk=md.neg_risk,
                        condition_id=md.condition_id,
                    )

                    # Apply drawdown adjustments
                    if dd_state.should_widen_quotes:
                        if quote_intent.bid_size:
                            quote_intent.bid_size *= dd_state.size_multiplier
                        if quote_intent.ask_size:
                            quote_intent.ask_size *= dd_state.size_multiplier

                    # Apply risk limits
                    quote_intent = state.risk_limits.apply_to_quote(
                        quote_intent, event_id=md.event_id
                    )

                    # Apply resolution risk size multiplier
                    res_mult = state.resolution_risk.get_size_multiplier(md.condition_id)
                    if res_mult < 1.0:
                        if quote_intent.bid_size:
                            quote_intent.bid_size *= res_mult
                        if quote_intent.ask_size:
                            quote_intent.ask_size *= res_mult

                    # Execute
                    if quote_intent.has_bid or quote_intent.has_ask:
                        if paper_mode and paper_engine and paper_logger:
                            # Paper mode: submit to paper engine, log the quote
                            paper_engine.cancel_all_orders(token_id)  # replace previous quotes
                            paper_engine.submit_quote(
                                token_id=token_id,
                                condition_id=md.condition_id,
                                bid_price=quote_intent.bid_price,
                                bid_size=quote_intent.bid_size,
                                ask_price=quote_intent.ask_price,
                                ask_size=quote_intent.ask_size,
                                strategy="mm",
                                neg_risk=md.neg_risk,
                            )
                            paper_logger.log_quote(
                                token_id=token_id,
                                condition_id=md.condition_id,
                                bid_price=quote_intent.bid_price,
                                bid_size=quote_intent.bid_size,
                                ask_price=quote_intent.ask_price,
                                ask_size=quote_intent.ask_size,
                                reservation_price=quote_intent.reservation_price,
                                fair_value=fv_estimate.fair_value,
                                half_spread=quote_intent.half_spread,
                                inventory=market_inv,
                            )
                            orders_submitted += (1 if quote_intent.has_bid else 0) + (1 if quote_intent.has_ask else 0)
                            markets_quoted += 1
                        elif order_manager:
                            result = await order_manager.diff_and_apply(
                                quote_intent, tick_size
                            )
                            orders_submitted += result.get("submitted", 0)
                            orders_canceled += result.get("canceled", 0)
                            markets_quoted += 1

                        # Record for backtest
                        recorder.record_quote_intent(
                            token_id=token_id,
                            bid_price=quote_intent.bid_price,
                            bid_size=quote_intent.bid_size,
                            ask_price=quote_intent.ask_price,
                            ask_size=quote_intent.ask_size,
                            reservation_price=quote_intent.reservation_price,
                            fair_value=fv_estimate.fair_value,
                            half_spread=quote_intent.half_spread,
                            inventory=market_inv,
                        )

                        # Record spread metric
                        if quote_intent.bid_price and quote_intent.ask_price:
                            spread_cents = (quote_intent.ask_price - quote_intent.bid_price) * 100
                            state.metrics.record_quote(token_id, md.condition_id, spread_cents)

            # ── Cycle metrics ──
            cycle_duration = (time.time() - cycle_start) * 1000
            state.metrics.record_quote_cycle(
                cycle_duration, markets_quoted, orders_submitted, orders_canceled
            )

            if cycle_count % 100 == 0:
                if paper_mode and paper_engine and paper_logger:
                    # Paper mode summary with PnL snapshot
                    snap = paper_engine.snapshot(state.book_manager)
                    paper_logger.log_pnl(snap.to_dict())
                    pos_summary = paper_engine.get_position_summary()
                    log_stats = paper_logger.get_stats()

                    logger.info(
                        "paper_cycle_summary",
                        cycle=cycle_count,
                        markets_quoted=markets_quoted,
                        submitted=orders_submitted,
                        nav=f"${snap.nav:.2f}",
                        pnl=f"${snap.pnl:.4f}",
                        pnl_pct=f"{snap.pnl_pct:.2f}%",
                        cash=f"${snap.cash:.2f}",
                        positions=snap.num_positions,
                        open_orders=snap.num_open_orders,
                        fills=snap.total_fills,
                        volume=f"${snap.total_volume:.2f}",
                        quotes_logged=log_stats["quotes_logged"],
                        arbs_logged=log_stats["arbs_logged"],
                        cycle_ms=f"{cycle_duration:.1f}",
                    )
                else:
                    logger.info(
                        "quote_cycle_summary",
                        cycle=cycle_count,
                        markets_quoted=markets_quoted,
                        submitted=orders_submitted,
                        canceled=orders_canceled,
                        cycle_ms=f"{cycle_duration:.1f}",
                        mode=state.mode,
                        nav=f"{nav:.2f}",
                        dd_tier=dd_state.tier.value,
                    )

            # ── Sleep until next cycle ──
            elapsed = time.time() - cycle_start
            sleep_time = max(0.01, quote_interval - elapsed)
            await asyncio.sleep(sleep_time)

    except Exception as e:
        logger.critical("main_loop_crashed", error=str(e), exc_info=True)
        if order_manager:
            try:
                await order_manager.cancel_all()
            except Exception:
                pass
    finally:
        # ── Shutdown ──
        state.mode = "SHUTDOWN"
        logger.info("pmm1_shutting_down")

        # Cancel all orders
        if order_manager:
            try:
                await order_manager.cancel_all()
            except Exception:
                pass

        # Paper mode final summary
        if paper_mode and paper_engine and paper_logger:
            snap = paper_engine.snapshot(state.book_manager)
            paper_logger.log_pnl(snap.to_dict())
            pos_summary = paper_engine.get_position_summary()
            log_stats = paper_logger.get_stats()

            print()
            print("=" * 50)
            print("  PMM-1 PAPER MODE — FINAL SUMMARY")
            print(f"  NAV: ${snap.nav:.2f} (started at ${paper_engine.initial_nav:.2f})")
            print(f"  PnL: ${snap.pnl:.4f} ({snap.pnl_pct:.2f}%)")
            print(f"  Cash: ${snap.cash:.2f}")
            print(f"  Positions: {snap.num_positions}")
            print(f"  Total fills: {snap.total_fills}")
            print(f"  Total volume: ${snap.total_volume:.2f}")
            print(f"  Quotes logged: {log_stats['quotes_logged']}")
            print(f"  Arbs detected: {log_stats['arbs_logged']}")
            print("=" * 50)
            print()

        # Stop all tasks
        if heartbeat:
            await heartbeat.stop()
        await market_ws.stop()
        if user_ws:
            await user_ws.stop()
        if reconciler:
            await reconciler.stop()
        await recorder.stop()

        # Close clients
        await gamma.close()
        await clob_public.close()
        await clob_private.close()
        await data_api.close()

        # Final metrics
        bot_metrics = state.metrics.get_bot_metrics()
        pnl_snapshot = state.pnl_tracker.compute_snapshot("session")
        logger.info(
            "pmm1_shutdown_complete",
            uptime_s=f"{bot_metrics.uptime_seconds:.0f}",
            total_cycles=bot_metrics.total_quote_cycles,
            total_fills=bot_metrics.total_fills,
            total_volume=f"{bot_metrics.total_volume:.2f}",
            net_pnl=f"{pnl_snapshot.net_pnl:.4f}",
        )


def main() -> None:
    """CLI entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()

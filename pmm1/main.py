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

from pmm1.checks.ctf_approval import check_ctf_approvals
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
from pmm1.api.rewards import RewardsClient
from pmm1.api.scoring import check_orders_scoring
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
from pmm1.strategy.fill_escalation import FillEscalator
from pmm1.strategy.neg_risk_arb import NegRiskArbDetector, NegRiskOutcome
from pmm1.strategy.exit_manager import ExitManager
from pmm1.strategy.quote_engine import QuoteEngine
from pmm1.strategy.rewards import RewardEstimator
from pmm1.strategy.universe import MarketMetadata, select_universe
from pmm1.ws.market_ws import MarketWebSocket
from pmm1.ws.user_ws import UserWebSocket
from pmm1.analytics.metrics import MetricsCollector
from pmm1.analytics.pnl import PnLTracker
from pmm1.paper.engine import PaperEngine
from pmm1.paper.logger import PaperLogger
from pmm1.notifications import send_telegram, format_fill_notification, format_exit_notification
from pmm1.storage.database import Database
from pmm1.recorder.fill_recorder import FillRecorder
from pmm1.recorder.book_recorder import BookRecorder

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
        self.quote_engine = QuoteEngine(
            settings.pricing,
            target_dollar_size=settings.pricing.target_dollar_size,
            max_dollar_size=settings.pricing.max_dollar_size,
        )
        self.parity_detector = BinaryParityDetector()
        self.neg_risk_detector = NegRiskArbDetector()
        self.reward_estimator = RewardEstimator()
        self.fill_escalator = FillEscalator(settings.exit.fill_escalation)

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
        self.rest_book_cache: dict[str, Any] = {}  # REST book cache
        self.rest_book_cache_ts: dict[str, float] = {}  # REST book cache timestamps

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

    # ── 1b. CTF approval check (non-blocking) ──
    if settings.wallet.address and not settings.bot.paper_mode:
        await check_ctf_approvals(settings.wallet.address)

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

    # ── 4. Fetch universe (ordered by 24h volume for best market selection) ──
    logger.info("fetching_universe")
    all_markets: list[MarketMetadata] = []

    # Fetch top markets DIRECTLY from Gamma /markets endpoint, ordered by volume
    try:
        raw_markets = await gamma.get_markets(
            active=True, closed=False, order_by="volume24hr", limit=200,
            max_pages=1,  # Just top 200 by volume, don't paginate all 34K
        )
        for market in raw_markets:
            token_ids = market.token_ids
            if len(token_ids) < 2:
                continue
            md = MarketMetadata(
                condition_id=market.condition_id,
                token_id_yes=token_ids[0],
                token_id_no=token_ids[1] if len(token_ids) > 1 else "",
                event_id="",  # Not available from /markets endpoint
                question=market.question,
                slug=market.market_slug,
                active=market.active,
                closed=market.closed,
                accepting_orders=market.accepting_orders,
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
        logger.info("universe_from_markets_api", count=len(all_markets))
    except Exception as e:
        logger.warning("markets_api_failed_falling_back_to_events", error=str(e))
        # Fallback to events endpoint
        events = await gamma.get_active_events()
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
                    accepting_orders=market.accepting_orders,
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
                    fees_enabled=market.fees_enabled,
                )
                # S0-4: Log fee-enabled markets
                if market.fees_enabled:
                    logger.info(
                        "fee_market_found",
                        event="fee_market_found",
                        condition_id=market.condition_id,
                        question=market.question[:50] if market.question else "?",
                    )
                all_markets.append(md)

    # Fetch reward-eligible markets from new rewards API
    rewards_client = RewardsClient(
        base_url=settings.api.clob_url,
        api_key=settings.api.api_key,
        api_secret=settings.api.api_secret,
    )
    try:
        sampling_markets = await rewards_client.fetch_sampling_markets()
        logger.info("sampling_markets_fetched", count=len(sampling_markets))
        
        # Build token_id → RewardInfo lookup (Gamma returns empty condition_ids,
        # so we must match by token_id instead)
        token_to_reward: dict[str, Any] = {}
        for ri in sampling_markets.values():
            for tid in ri.token_ids:
                token_to_reward[tid] = ri
        
        # Mark reward eligibility and update reward data
        matched = 0
        for m in all_markets:
            # Try condition_id match first, then token_id match
            reward_info = sampling_markets.get(m.condition_id)
            if not reward_info:
                reward_info = token_to_reward.get(m.token_id_yes) or token_to_reward.get(m.token_id_no)
            if reward_info:
                m.reward_eligible = True
                m.reward_daily_rate = reward_info.daily_rate
                m.reward_min_size = reward_info.min_size
                m.reward_max_spread = reward_info.max_spread
                state.reward_eligible.add(m.condition_id)
                matched += 1
        logger.info("reward_matching_done", matched=matched, total=len(all_markets), token_index_size=len(token_to_reward))
    except Exception as e:
        logger.warning("sampling_markets_fetch_failed", error=str(e))
    finally:
        await rewards_client.close()

    # Attach clients to state for PMM-2 shadow mode access
    state.gamma_client = gamma
    state.rewards_client = RewardsClient(
        base_url=settings.api.clob_url,
        api_key=settings.api.api_key,
        api_secret=settings.api.api_secret,
    )

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

    # Count reward-eligible markets (S0-5)
    reward_eligible_count = sum(1 for md in state.active_markets.values() if md.reward_eligible)
    logger.info(
        "universe_selected",
        count=len(state.active_markets),
        reward_eligible=reward_eligible_count,
        total_markets=len(state.active_markets),
    )

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
        # Re-sync server time after potentially long universe fetch
        try:
            fresh_server_time = await clob_public.get_server_time()
            order_manager.set_server_time_offset(fresh_server_time)
        except Exception:
            order_manager.set_server_time_offset(int(time.time()))

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

    # ── Initialize ExitManager ──
    exit_manager = ExitManager(
        config=settings.exit,
        position_tracker=state.position_tracker,
        book_manager=state.book_manager,
        kill_switch=state.kill_switch,
        clob_public=clob_public,
    )

    recorder = LiveRecorder()
    parquet_writer = ParquetWriter(settings.storage.parquet_dir)

    # ── Initialize PMM-2 data collection (Sprint 1) ──
    db = Database("data/pmm1.db")
    await db.init()
    fill_recorder = FillRecorder(db, state.book_manager)
    book_recorder = BookRecorder(db)

    # ── Initialize PMM-2 runtime (Sprint 7) ──
    from pmm2.runtime.integration import (
        maybe_init_pmm2, pmm2_on_book_delta, pmm2_on_fill,
        pmm2_on_order_live, pmm2_on_order_canceled,
    )
    pmm2_runtime = await maybe_init_pmm2(settings, db, state)

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
            # PMM-2: forward book deltas to queue estimator
            # (simplified — full delta tracking would need old vs new per level)

    async def on_trade(token_id: str, price: float) -> None:
        """Callback when a trade occurs."""
        state.feature_engine.record_trade(token_id, price, 0.0, "UNKNOWN")
        recorder.record_trade(token_id, price, 0.0, "UNKNOWN")

    async def on_reconnect() -> None:
        """Callback after user WS reconnect — trigger reconciliation."""
        if reconciler:
            logger.info("post_reconnect_reconciliation")
            await reconciler.full_reconciliation()

    # Track notified fills to prevent duplicate notifications
    _notified_fills: set[str] = set()

    async def on_fill(msg: dict[str, Any]) -> None:
        """Callback when a fill/trade is received from UserWebSocket."""
        try:
            # Extract fill details
            token_id = msg.get("asset_id") or msg.get("token_id", "")
            order_id = msg.get("orderID") or msg.get("order_id") or msg.get("id", "")
            side = msg.get("side", "").upper()
            price_str = msg.get("price") or msg.get("matchPrice", "0")
            size_str = msg.get("size") or msg.get("matchSize", "0")
            
            price = float(price_str) if price_str else 0.0
            size = float(size_str) if size_str else 0.0
            
            if size <= 0 or price <= 0:
                return
            
            # ── GATE 1: Must be OUR order (tracked by order manager) ──
            tracked = state.order_tracker.get(order_id)
            if tracked is None:
                # Not our order — ignore silently
                return
            
            # Use tracked order's side if WS didn't provide it
            if not side:
                side = tracked.side
            
            # ── GATE 2: Deduplicate (WS replays the same fill multiple times) ──
            fill_key = f"{order_id}:{size}:{price}"
            if fill_key in _notified_fills:
                return
            _notified_fills.add(fill_key)
            # Cap set size to prevent memory leak
            if len(_notified_fills) > 500:
                _notified_fills.clear()
            
            # Find market info for this token
            market_question = ""
            condition_id = None
            for md in state.active_markets.values():
                if md.token_id_yes == token_id or md.token_id_no == token_id:
                    condition_id = md.condition_id
                    market_question = md.question
                    break
            
            dollar_value = size * price
            
            # Log fill details
            logger.info(
                "fill_confirmed",
                token_id=token_id[:16] if token_id else "?",
                order_id=order_id[:16] if order_id else "?",
                side=side,
                price=price,
                size=size,
                dollar_value=round(dollar_value, 2),
                market=market_question[:40] if market_question else "?",
            )
            
            # Update position tracker
            if condition_id:
                pos = state.position_tracker.get(condition_id)
                if pos:
                    fill_side = "BUY" if side == "BUY" else "SELL"
                    pos.apply_fill(token_id, fill_side, size, price, fee=0.0)
            
            # ── PMM-2: Record fill with markout tracking (S1-2) ──
            if condition_id and token_id:
                tracked_order = state.order_tracker.get(order_id)
                is_scoring = tracked_order.is_scoring if tracked_order else False
                
                # Check if market is reward-eligible
                market_md = state.active_markets.get(condition_id)
                reward_eligible = market_md.reward_eligible if market_md else False
                
                # Get book midpoint at fill time
                book = state.book_manager.get(token_id)
                mid_at_fill = book.get_midpoint() if book else None
                
                asyncio.create_task(
                    fill_recorder.record_fill(
                        ts=datetime.now(timezone.utc),
                        condition_id=condition_id,
                        token_id=token_id,
                        order_id=order_id,
                        side=side,
                        price=price,
                        size=size,
                        fee=0.0,
                        mid_at_fill=mid_at_fill,
                        is_scoring=is_scoring,
                        reward_eligible=reward_eligible,
                    )
                )
            
            # Compute PnL for SELL fills
            pnl_text = ""
            if side == "SELL" and condition_id:
                pos = state.position_tracker.get(condition_id)
                avg_entry = pos.yes_avg_price if pos and pos.yes_avg_price > 0 else 0
                if not avg_entry and pos:
                    avg_entry = pos.no_avg_price
                if avg_entry > 0:
                    pnl = (price - avg_entry) * size
                    pnl_pct = ((price / avg_entry) - 1) * 100
                    pnl_emoji = "📈" if pnl >= 0 else "📉"
                    pnl_text = f"\n{pnl_emoji} PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%)"
            
            # Send ONE Telegram notification per fill
            # Check if order was scoring for rewards (S0-5)
            tracked_order = state.order_tracker.get(order_id)
            is_scoring = tracked_order.is_scoring if tracked_order else False
            scoring_badge = " 💰" if is_scoring else ""
            
            emoji = "🟢" if side == "BUY" else "🔴"
            market_line = f"\n📊 {market_question[:50]}" if market_question else ""
            notification = (
                f"{emoji} *{side or 'FILL'}*{scoring_badge}: {size:.1f} shares @ ${price:.3f}\n"
                f"💵 Value: ${dollar_value:.2f}"
                f"{pnl_text}"
                f"{market_line}"
            )
            asyncio.create_task(send_telegram(notification))
            
            # Record fill for escalation ladder
            if hasattr(state, 'fill_escalator'):
                state.fill_escalator.record_fill()
            
            # PMM-2: forward fill to queue estimator + persistence
            pmm2_on_fill(pmm2_runtime, order_id, size, price)
        except Exception as e:
            logger.error("on_fill_callback_error", error=str(e))

    async def on_order_status(msg: dict[str, Any]) -> None:
        """Callback when order status changes from UserWebSocket."""
        try:
            order_id = msg.get("orderID") or msg.get("order_id") or msg.get("id", "")
            status = msg.get("status", "").upper()
            
            logger.info(
                "order_status_change",
                order_id=order_id[:16] if order_id else "?",
                status=status,
            )
            
            # PMM-2: forward order lifecycle events
            if status == "LIVE":
                token_id = msg.get("asset_id") or msg.get("token_id", "")
                side = msg.get("side", "").upper()
                price = float(msg.get("price", 0))
                size = float(msg.get("original_size") or msg.get("size", 0))
                # Estimate book depth at this price (rough)
                book = state.book_manager.get(token_id) if token_id else None
                book_depth = 0.0
                if book:
                    levels = book._bids if side == "BUY" else book._asks
                    for lvl in levels.values():
                        if abs(lvl.price_float - price) < 0.001:
                            book_depth = lvl.size_float
                            break
                pmm2_on_order_live(pmm2_runtime, order_id, token_id, side, price, size, book_depth)
            elif status in ("CANCELED", "CANCELLED"):
                pmm2_on_order_canceled(pmm2_runtime, order_id)
        except Exception as e:
            logger.error("on_order_status_callback_error", error=str(e))

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
            on_trade_update=on_fill,
            on_order_update=on_order_status,
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
    
    # ── Scoring check loop (S0-2) ──
    async def scoring_check_loop() -> None:
        """Background task to check order scoring status every 30 seconds."""
        while not shutdown_event.is_set():
            try:
                await asyncio.sleep(30)
                if paper_mode:
                    continue
                
                # Get all live order IDs
                live_orders = [
                    order for order in state.order_tracker.get_active_orders()
                    if order.order_id
                ]
                if not live_orders:
                    continue
                
                order_ids = [order.order_id for order in live_orders]
                
                # Check scoring status
                scoring_status = await check_orders_scoring(clob_private, order_ids)
                
                # Update tracked orders
                scoring_count = 0
                for order in live_orders:
                    is_scoring = scoring_status.get(order.order_id, False)
                    order.is_scoring = is_scoring
                    if is_scoring:
                        scoring_count += 1
                
                logger.info(
                    "order_scoring_check",
                    scoring_count=scoring_count,
                    total_checked=len(order_ids),
                )
                
                # ── PMM-2: Persist scoring history (S1-4) ──
                ts = datetime.now(timezone.utc).isoformat()
                scoring_records = []
                for order in live_orders:
                    # Find condition_id for this order
                    condition_id = order.condition_id or "UNKNOWN"
                    scoring_records.append((
                        ts,
                        order.order_id,
                        condition_id,
                        1 if order.is_scoring else 0,
                    ))
                
                if scoring_records:
                    sql = """
                        INSERT OR REPLACE INTO scoring_history (ts, order_id, condition_id, is_scoring)
                        VALUES (?, ?, ?, ?)
                    """
                    await db.execute_many(sql, scoring_records)
            except Exception as e:
                logger.error("scoring_check_loop_error", error=str(e))
    
    if not paper_mode:
        scoring_check_task = asyncio.create_task(scoring_check_loop())

    # ── Book snapshot loop (S1-3) ──
    async def book_snapshot_loop() -> None:
        """Background task to snapshot books every 10 seconds."""
        while not shutdown_event.is_set():
            try:
                await asyncio.sleep(10)
                if state.mode not in ("QUOTING", "PAUSED"):
                    continue
                
                # Snapshot all active market books
                await book_recorder.snapshot_books(state.active_markets, state.book_manager)
            except Exception as e:
                logger.error("book_snapshot_loop_error", error=str(e))
    
    book_snapshot_task = asyncio.create_task(book_snapshot_loop())

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
    last_rebate_check = 0.0  # S0-3: track last rebate check time

    try:
        while not shutdown_event.is_set():
            cycle_start = time.time()
            cycle_count += 1
            
            # ── Rebate check (S0-3) — every hour ──
            if not paper_mode and (time.time() - last_rebate_check) >= 3600:
                try:
                    rebate_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    rewards_client_rebate = RewardsClient(base_url=settings.api.clob_url)
                    rebate_data = await rewards_client_rebate.fetch_rebates(
                        maker_address=settings.wallet.address,
                        date=rebate_date,
                    )
                    await rewards_client_rebate.close()
                    logger.info(
                        "rebate_check",
                        maker_address=settings.wallet.address,
                        date=rebate_date,
                        data=rebate_data,
                    )
                    last_rebate_check = time.time()
                except Exception as e:
                    logger.error("rebate_check_error", error=str(e))
                    last_rebate_check = time.time()  # Don't retry immediately on error

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

            # ── Pre-fetch stale REST books in parallel ──
            stale_threshold = 120.0 if paper_mode else 60.0
            stale_tokens = []
            for md in state.eligible_markets():
                tid = md.token_id_yes
                if not tid:
                    continue
                ws_book = state.book_manager.get(tid)
                ws_ok = ws_book is not None and ws_book.age_seconds <= stale_threshold
                if not ws_ok:
                    cache_key = f"rest_book_{tid}"
                    cache_ts = state.rest_book_cache_ts.get(cache_key, 0)
                    if time.time() - cache_ts > 10.0:
                        stale_tokens.append(tid)

            if stale_tokens:
                async def _fetch_rest_book(tid: str) -> None:
                    try:
                        rest_book = await clob_public.get_order_book(tid)
                        if rest_book and rest_book.bids and rest_book.asks:
                            from pmm1.state.books import OrderBook
                            cb = OrderBook(tid, tick_size=state.tick_sizes.get(tid, Decimal("0.01")))
                            for bid in rest_book.bids:
                                cb.update_level("bid", Decimal(bid.price), Decimal(bid.size))
                            for ask in rest_book.asks:
                                cb.update_level("ask", Decimal(ask.price), Decimal(ask.size))
                            ck = f"rest_book_{tid}"
                            state.rest_book_cache[ck] = cb
                            state.rest_book_cache_ts[ck] = time.time()
                    except Exception:
                        pass

                # Fetch up to 5 books in parallel per cycle
                batch = stale_tokens[:5]
                await asyncio.gather(*[_fetch_rest_book(t) for t in batch])

            # ── Quote each market ──
            markets_quoted = 0
            orders_submitted = 0
            orders_canceled = 0

            for md in state.eligible_markets():
                # Skip if resolution risk says stop
                if not state.resolution_risk.should_quote(md.condition_id):
                    continue

                # For arb detection, we'll use the YES token book (arb logic already handles both)
                token_id = md.token_id_yes
                if not token_id:
                    continue

                book = state.book_manager.get(token_id)
                # Stale threshold: 60s live (WS books are fine within a minute), 120s paper
                stale_threshold = 120.0 if paper_mode else 60.0
                book_usable = book is not None and book.age_seconds <= stale_threshold

                # REST fallback: if no usable WS book, fetch via REST (cached 10s)
                if not book_usable:
                    rest_cache_key = f"rest_book_{token_id}"
                    rest_cache_ts = state.rest_book_cache_ts.get(rest_cache_key, 0)
                    if time.time() - rest_cache_ts > 10.0:  # Refresh every 10s
                        try:
                            rest_book = await clob_public.get_order_book(token_id)
                            if rest_book and rest_book.bids and rest_book.asks:
                                from pmm1.state.books import OrderBook
                                cached_book = OrderBook(token_id, tick_size=state.tick_sizes.get(token_id, Decimal("0.01")))
                                for bid in rest_book.bids:
                                    cached_book.update_level("bid", Decimal(bid.price), Decimal(bid.size))
                                for ask in rest_book.asks:
                                    cached_book.update_level("ask", Decimal(ask.price), Decimal(ask.size))
                                state.rest_book_cache[rest_cache_key] = cached_book
                                state.rest_book_cache_ts[rest_cache_key] = time.time()
                        except Exception:
                            pass
                    book = state.rest_book_cache.get(rest_cache_key)

                if book is None or (not book._bids and not book._asks):
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
                    base_fair_value = fv_estimate.fair_value

                    # Skip extreme prices — no counterparty flow below 15c or above 85c
                    if base_fair_value < 0.15 or base_fair_value > 0.85:
                        continue

                    # Inventory (YES position)
                    pos = state.position_tracker.get(md.condition_id)
                    yes_inventory = pos.yes_size if pos else 0.0
                    no_inventory = pos.no_size if pos else 0.0
                    market_inv = pos.net_exposure if pos else 0.0
                    cluster_inv = state.position_tracker.get_event_net_exposure(md.event_id) if md.event_id else 0.0

                    # Position age for dynamic γ
                    position_age_hours = 0.0
                    if pos and pos.last_update > 0 and pos.net_exposure != 0:
                        position_age_hours = (time.time() - pos.last_update) / 3600.0

                    # Resolution exit: check if we should block new buys
                    block_new_buys = False
                    res_action = exit_manager.get_resolution_action(
                        md.condition_id, state.active_markets
                    )
                    if res_action and res_action.block_new_buys:
                        block_new_buys = True

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
                        position_age_hours=position_age_hours,
                    )

                    # ── FILL-SPEED MODE: Escalation + Top-of-Book Clamp ──
                    # Apply escalation ticks first (improves prices)
                    escalation_ticks = state.fill_escalator.get_escalation_ticks()
                    if escalation_ticks > 0 and book is not None:
                        tick_float = float(tick_size)
                        if quote_intent.bid_price:
                            quote_intent.bid_price += escalation_ticks * tick_float
                        if quote_intent.ask_price:
                            quote_intent.ask_price -= escalation_ticks * tick_float
                        
                        # Clamp to valid price range
                        if quote_intent.bid_price:
                            quote_intent.bid_price = max(tick_float, min(1.0 - tick_float, quote_intent.bid_price))
                        if quote_intent.ask_price:
                            quote_intent.ask_price = max(tick_float, min(1.0 - tick_float, quote_intent.ask_price))

                    # Top-of-book clamp: join the queue, don't sit 5¢ back
                    if book is not None:
                        bb = book.get_best_bid()
                        ba = book.get_best_ask()
                        tick_float = float(tick_size)
                        
                        # Clamp bid: if more than 1 tick below best bid, raise to best bid
                        if bb and quote_intent.bid_price:
                            best_bid_float = bb.price_float
                            if quote_intent.bid_price < best_bid_float - tick_float:
                                quote_intent.bid_price = best_bid_float
                            # Never go above best bid (would cross)
                            if quote_intent.bid_price > best_bid_float:
                                quote_intent.bid_price = best_bid_float
                        
                        # Clamp ask: if more than 1 tick above best ask, lower to best ask
                        if ba and quote_intent.ask_price:
                            best_ask_float = ba.price_float
                            if quote_intent.ask_price > best_ask_float + tick_float:
                                quote_intent.ask_price = best_ask_float
                            # Never go below best ask (would cross)
                            if quote_intent.ask_price < best_ask_float:
                                quote_intent.ask_price = best_ask_float

                    # SELL logic: only post asks when we hold inventory
                    if not paper_mode:
                        if market_inv <= 0:
                            # No inventory — don't post sells
                            quote_intent.ask_size = None
                            quote_intent.ask_price = None
                        else:
                            # Cap sell size to what we actually hold
                            if quote_intent.ask_size and quote_intent.ask_size > market_inv:
                                quote_intent.ask_size = market_inv
                            # Ensure minimum 5 shares for Polymarket
                            if quote_intent.ask_size and quote_intent.ask_size < 5.0:
                                if market_inv >= 5.0:
                                    quote_intent.ask_size = 5.0
                                else:
                                    # Can't meet minimum — skip sell
                                    quote_intent.ask_size = None
                                    quote_intent.ask_price = None

                    # Block new buys during resolution exit window
                    if block_new_buys:
                        quote_intent.bid_size = None
                        quote_intent.bid_price = None

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

                    # Crossing guard: ask must be above best bid, bid below best ask
                    if book is not None:
                        bb = book.get_best_bid()
                        ba = book.get_best_ask()
                        if bb and quote_intent.ask_price and quote_intent.ask_price <= bb.price_float:
                            # Ask would cross the bid — place above best bid + tick
                            quote_intent.ask_price = bb.price_float + float(tick_size)
                        if ba and quote_intent.bid_price and quote_intent.bid_price >= ba.price_float:
                            # Bid would cross the ask — place below best ask - tick
                            quote_intent.bid_price = ba.price_float - float(tick_size)

                    # Final min-size enforcement (after all multipliers)
                    MIN_SHARES_FINAL = 5.0
                    if quote_intent.bid_size is not None and quote_intent.bid_size < MIN_SHARES_FINAL:
                        quote_intent.bid_size = None
                        quote_intent.bid_price = None
                    if quote_intent.ask_size is not None and quote_intent.ask_size < MIN_SHARES_FINAL:
                        quote_intent.ask_size = None
                        quote_intent.ask_price = None

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

                    # ── Market making for NO token ──
                    # Quote the NO token if it exists, using inverted fair value
                    if md.token_id_no and settings.strategy.enable_market_making:
                        token_id_no = md.token_id_no
                        book_no = state.book_manager.get(token_id_no)
                        book_no_usable = book_no is not None and book_no.age_seconds <= stale_threshold

                        # REST fallback for NO token book
                        if not book_no_usable:
                            rest_cache_key_no = f"rest_book_{token_id_no}"
                            rest_cache_ts_no = state.rest_book_cache_ts.get(rest_cache_key_no, 0)
                            if time.time() - rest_cache_ts_no > 10.0:
                                try:
                                    rest_book_no = await clob_public.get_order_book(token_id_no)
                                    if rest_book_no and rest_book_no.bids and rest_book_no.asks:
                                        from pmm1.state.books import OrderBook
                                        cached_book_no = OrderBook(token_id_no, tick_size=state.tick_sizes.get(token_id_no, Decimal("0.01")))
                                        for bid in rest_book_no.bids:
                                            cached_book_no.update_level("bid", Decimal(bid.price), Decimal(bid.size))
                                        for ask in rest_book_no.asks:
                                            cached_book_no.update_level("ask", Decimal(ask.price), Decimal(ask.size))
                                        state.rest_book_cache[rest_cache_key_no] = cached_book_no
                                        state.rest_book_cache_ts[rest_cache_key_no] = time.time()
                                except Exception:
                                    pass
                            book_no = state.rest_book_cache.get(rest_cache_key_no)

                        if book_no and (book_no._bids or book_no._asks):
                            tick_size_no = state.tick_sizes.get(token_id_no, Decimal("0.01"))

                            # Compute features for NO token
                            features_no = state.feature_engine.compute(
                                token_id=token_id_no,
                                book=book_no,
                                condition_id=md.condition_id,
                                end_date=md.end_date,
                            )

                            # Invert fair value for NO token: NO_fv = 1.0 - YES_fv
                            no_fair_value = 1.0 - base_fair_value

                            # Skip if inverted fair value is extreme
                            if no_fair_value >= 0.15 and no_fair_value <= 0.85:
                                # Inventory for NO side
                                no_token_inventory = no_inventory
                                
                                # Position age (same as YES since it's the same market)
                                position_age_hours_no = 0.0
                                if pos and pos.last_update > 0 and pos.net_exposure != 0:
                                    position_age_hours_no = (time.time() - pos.last_update) / 3600.0

                                # Resolution exit check
                                block_new_buys_no = False
                                res_action_no = exit_manager.get_resolution_action(
                                    md.condition_id, state.active_markets
                                )
                                if res_action_no and res_action_no.block_new_buys:
                                    block_new_buys_no = True

                                # Reward EV (same for both sides)
                                reward_ev_no = state.reward_estimator.compute_reward_ev_for_universe(md.condition_id)

                                # Quote NO token
                                quote_intent_no = state.quote_engine.compute_quote(
                                    token_id=token_id_no,
                                    features=features_no,
                                    fair_value=no_fair_value,
                                    haircut=fv_estimate.haircut,  # Use same haircut
                                    confidence=fv_estimate.confidence,  # Use same confidence
                                    market_inventory=-market_inv,  # Invert inventory sign for NO
                                    cluster_inventory=cluster_inv,
                                    tick_size=float(tick_size_no),
                                    reward_ev=reward_ev_no,
                                    neg_risk=md.neg_risk,
                                    condition_id=md.condition_id,
                                    position_age_hours=position_age_hours_no,
                                )

                                # SELL logic for NO: only post asks when we hold NO inventory
                                if not paper_mode:
                                    if no_token_inventory <= 0:
                                        quote_intent_no.ask_size = None
                                        quote_intent_no.ask_price = None
                                    else:
                                        if quote_intent_no.ask_size and quote_intent_no.ask_size > no_token_inventory:
                                            quote_intent_no.ask_size = no_token_inventory
                                        if quote_intent_no.ask_size and quote_intent_no.ask_size < 5.0:
                                            if no_token_inventory >= 5.0:
                                                quote_intent_no.ask_size = 5.0
                                            else:
                                                quote_intent_no.ask_size = None
                                                quote_intent_no.ask_price = None

                                # Block new buys during resolution exit
                                if block_new_buys_no:
                                    quote_intent_no.bid_size = None
                                    quote_intent_no.bid_price = None

                                # Apply drawdown adjustments
                                if dd_state.should_widen_quotes:
                                    if quote_intent_no.bid_size:
                                        quote_intent_no.bid_size *= dd_state.size_multiplier
                                    if quote_intent_no.ask_size:
                                        quote_intent_no.ask_size *= dd_state.size_multiplier

                                # Apply risk limits
                                quote_intent_no = state.risk_limits.apply_to_quote(
                                    quote_intent_no, event_id=md.event_id
                                )

                                # Apply resolution risk size multiplier
                                res_mult_no = state.resolution_risk.get_size_multiplier(md.condition_id)
                                if res_mult_no < 1.0:
                                    if quote_intent_no.bid_size:
                                        quote_intent_no.bid_size *= res_mult_no
                                    if quote_intent_no.ask_size:
                                        quote_intent_no.ask_size *= res_mult_no

                                # Crossing guard for NO token
                                if book_no is not None:
                                    bb_no = book_no.get_best_bid()
                                    ba_no = book_no.get_best_ask()
                                    if bb_no and quote_intent_no.ask_price and quote_intent_no.ask_price <= bb_no.price_float:
                                        quote_intent_no.ask_price = bb_no.price_float + float(tick_size_no)
                                    if ba_no and quote_intent_no.bid_price and quote_intent_no.bid_price >= ba_no.price_float:
                                        quote_intent_no.bid_price = ba_no.price_float - float(tick_size_no)

                                # Final min-size enforcement for NO (after all multipliers)
                                if quote_intent_no.bid_size is not None and quote_intent_no.bid_size < 5.0:
                                    quote_intent_no.bid_size = None
                                    quote_intent_no.bid_price = None
                                if quote_intent_no.ask_size is not None and quote_intent_no.ask_size < 5.0:
                                    quote_intent_no.ask_size = None
                                    quote_intent_no.ask_price = None

                                # Execute NO token quote
                                if quote_intent_no.has_bid or quote_intent_no.has_ask:
                                    if paper_mode and paper_engine and paper_logger:
                                        paper_engine.cancel_all_orders(token_id_no)
                                        paper_engine.submit_quote(
                                            token_id=token_id_no,
                                            condition_id=md.condition_id,
                                            bid_price=quote_intent_no.bid_price,
                                            bid_size=quote_intent_no.bid_size,
                                            ask_price=quote_intent_no.ask_price,
                                            ask_size=quote_intent_no.ask_size,
                                            strategy="mm",
                                            neg_risk=md.neg_risk,
                                        )
                                        paper_logger.log_quote(
                                            token_id=token_id_no,
                                            condition_id=md.condition_id,
                                            bid_price=quote_intent_no.bid_price,
                                            bid_size=quote_intent_no.bid_size,
                                            ask_price=quote_intent_no.ask_price,
                                            ask_size=quote_intent_no.ask_size,
                                            reservation_price=quote_intent_no.reservation_price,
                                            fair_value=no_fair_value,
                                            half_spread=quote_intent_no.half_spread,
                                            inventory=-market_inv,
                                        )
                                        orders_submitted += (1 if quote_intent_no.has_bid else 0) + (1 if quote_intent_no.has_ask else 0)
                                    elif order_manager:
                                        result_no = await order_manager.diff_and_apply(
                                            quote_intent_no, tick_size_no
                                        )
                                        orders_submitted += result_no.get("submitted", 0)
                                        orders_canceled += result_no.get("canceled", 0)

                                    # Record NO token quote
                                    recorder.record_quote_intent(
                                        token_id=token_id_no,
                                        bid_price=quote_intent_no.bid_price,
                                        bid_size=quote_intent_no.bid_size,
                                        ask_price=quote_intent_no.ask_price,
                                        ask_size=quote_intent_no.ask_size,
                                        reservation_price=quote_intent_no.reservation_price,
                                        fair_value=no_fair_value,
                                        half_spread=quote_intent_no.half_spread,
                                        inventory=-market_inv,
                                    )

                                    # Record NO token spread metric
                                    if quote_intent_no.bid_price and quote_intent_no.ask_price:
                                        spread_cents_no = (quote_intent_no.ask_price - quote_intent_no.bid_price) * 100
                                        state.metrics.record_quote(token_id_no, md.condition_id, spread_cents_no)

            # ── Exit Manager: evaluate all sell/exit signals ──
            if not paper_mode and order_manager:
                try:
                    exit_signals = await exit_manager.evaluate_all(state.active_markets)
                    for exit_sig in exit_signals:
                        # Determine tick size for the token
                        sig_tick = state.tick_sizes.get(
                            exit_sig.token_id, Decimal("0.01")
                        )
                        # Look up neg_risk from market metadata
                        sig_md = state.active_markets.get(exit_sig.condition_id)
                        sig_neg_risk = sig_md.neg_risk if sig_md else False

                        if exit_sig.price and exit_sig.price > 0:
                            result = await order_manager.submit_exit(
                                token_id=exit_sig.token_id,
                                condition_id=exit_sig.condition_id,
                                price=exit_sig.price,
                                size=exit_sig.size,
                                tick_size=sig_tick,
                                urgency=exit_sig.urgency,
                                neg_risk=sig_neg_risk,
                            )
                            if result.get("submitted"):
                                orders_submitted += 1
                                logger.info(
                                    "exit_order_submitted",
                                    reason=exit_sig.reason,
                                    token_id=exit_sig.token_id[:16],
                                    size=exit_sig.size,
                                    price=exit_sig.price,
                                    urgency=exit_sig.urgency,
                                )
                                
                                # Send Telegram notification for exit signal
                                notification = format_exit_notification(
                                    exit_type=exit_sig.reason,
                                    token_id=exit_sig.token_id,
                                    price=exit_sig.price,
                                    size=exit_sig.size,
                                )
                                asyncio.create_task(send_telegram(notification))
                            elif result.get("error"):
                                logger.warning(
                                    "exit_order_failed",
                                    reason=exit_sig.reason,
                                    token_id=exit_sig.token_id[:16],
                                    error=result["error"],
                                )
                except Exception as e:
                    logger.error("exit_manager_error", error=str(e), exc_info=True)

            # ── FILL-SPEED MODE: Taker Bootstrap ──
            # After 20 min with no fills, take liquidity with one small order
            if not paper_mode and order_manager and state.fill_escalator.should_take_liquidity():
                try:
                    # Pick highest-scored eligible market
                    eligible = state.eligible_markets()
                    if eligible:
                        # Sort by volume (or use universe scoring if available)
                        best_market = max(
                            eligible,
                            key=lambda m: m.volume_24h_usd if hasattr(m, 'volume_24h_usd') else 0.0
                        )
                        
                        token_id_taker = best_market.token_id_yes
                        book_taker = state.book_manager.get(token_id_taker)
                        
                        if book_taker:
                            ba_taker = book_taker.get_best_ask()
                            if ba_taker and ba_taker.price_float > 0:
                                # Submit small FAK BUY at best ask
                                taker_size = max(5.0, state.fill_escalator.config.taker_min_shares)
                                taker_price = ba_taker.price_float
                                
                                # Ensure dollar value meets minimum
                                MIN_DOLLAR = 1.5
                                if taker_price * taker_size < MIN_DOLLAR:
                                    taker_size = max(taker_size, MIN_DOLLAR / taker_price)
                                
                                tick_taker = state.tick_sizes.get(token_id_taker, Decimal("0.01"))
                                
                                taker_req = CreateOrderRequest(
                                    token_id=token_id_taker,
                                    price=str(taker_price),
                                    size=str(taker_size),
                                    side=OrderSide.BUY,
                                    order_type=OrderType.FAK,
                                    neg_risk=best_market.neg_risk,
                                    post_only=False,
                                    tick_size=str(tick_taker),
                                )
                                
                                logger.info(
                                    "taker_bootstrap_submitting",
                                    token_id=token_id_taker[:16],
                                    price=taker_price,
                                    size=taker_size,
                                    escalation_status=state.fill_escalator.get_status(),
                                )
                                
                                # Submit via order manager (or direct client if needed)
                                # For FAK orders, we may need to use clob_private directly
                                # since order_manager expects GTC/GTD for diff_and_apply
                                try:
                                    resp = await clob_private.create_order(taker_req)
                                    if resp and resp.success:
                                        logger.info(
                                            "taker_bootstrap_submitted",
                                            order_id=resp.order_id[:16] if resp.order_id else "?",
                                            token_id=token_id_taker[:16],
                                        )
                                        # Reset the taker cycle
                                        state.fill_escalator.reset_taker_cycle()
                                        orders_submitted += 1
                                    else:
                                        logger.warning(
                                            "taker_bootstrap_failed",
                                            error=resp.error_msg if resp else "no response",
                                        )
                                except Exception as taker_err:
                                    logger.error("taker_bootstrap_error", error=str(taker_err))
                except Exception as e:
                    logger.error("taker_bootstrap_outer_error", error=str(e), exc_info=True)

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
                    # Count markets in resolution exit window
                    resolution_markets = sum(
                        1 for cid in state.active_markets
                        if exit_manager.get_resolution_action(cid, state.active_markets) is not None
                    )
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
                        resolution_exits=resolution_markets,
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

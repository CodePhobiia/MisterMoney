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
import hashlib
import json
import signal
import sys
import time
from collections import OrderedDict, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from pmm1.analytics.edge_tracker import EdgeTracker
from pmm1.analytics.fv_calibrator import FairValueCalibrator
from pmm1.analytics.metrics import MetricsCollector
from pmm1.analytics.pnl import PnLTracker
from pmm1.analytics.resolution_recorder import ResolutionRecorder
from pmm1.api.clob_private import (
    ClobPrivateClient,
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
from pmm1.checks.ctf_approval import check_ctf_approvals
from pmm1.execution.mutation_guard import LiveMutationGuard
from pmm1.execution.order_manager import OrderManager
from pmm1.execution.reconciler import Reconciler
from pmm1.logging import get_logger, setup_logging
from pmm1.materializers import (
    BookSnapshotFactMaterializer,
    CanaryCycleFactMaterializer,
    FillFactMaterializer,
    OrderFactMaterializer,
    QuoteFactMaterializer,
    ShadowCycleFactMaterializer,
)
from pmm1.notifications import (
    AlertManager,
    format_exit_notification,
    send_telegram,
)
from pmm1.ops import OpsMonitor
from pmm1.paper.engine import PaperEngine
from pmm1.paper.logger import PaperLogger
from pmm1.recorder.book_recorder import BookRecorder
from pmm1.recorder.fill_recorder import FillRecorder
from pmm1.risk.drawdown import DrawdownGovernor
from pmm1.risk.kill_switch import KillSwitch
from pmm1.risk.limits import RiskLimits
from pmm1.risk.resolution import ResolutionRiskManager
from pmm1.settings import Settings, load_settings
from pmm1.state.books import BookManager, build_order_book_from_snapshot
from pmm1.state.heartbeats import HeartbeatState
from pmm1.state.inventory import InventoryManager
from pmm1.state.orders import OrderState, OrderTracker, zero_lifecycle_counts
from pmm1.state.positions import PositionTracker
from pmm1.storage.database import Database
from pmm1.storage.parquet import ParquetWriter
from pmm1.storage.postgres import PostgresStore
from pmm1.storage.redis import RedisStateStore
from pmm1.storage.spine import SpineEmitter, make_session_id, resolve_git_sha
from pmm1.strategy.binary_parity import BinaryParityDetector
from pmm1.strategy.exit_manager import ExitManager, SellSignal
from pmm1.strategy.fair_value import FairValueModel
from pmm1.strategy.features import FeatureEngine
from pmm1.strategy.fill_escalation import FillEscalator
from pmm1.strategy.llm_reasoner import LLMReasoner, ReasonerConfig
from pmm1.strategy.market_sanity import (
    MarketTelemetry,
    MarketTelemetryEvent,
    apply_quote_book_guards,
    assess_live_market,
    audit_active_markets,
    compute_concentration_suppressions,
    inventory_context_for_token,
)
from pmm1.strategy.neg_risk_arb import NegRiskArbDetector, NegRiskOutcome
from pmm1.strategy.quote_engine import QuoteEngine, QuoteIntent
from pmm1.strategy.rewards import RewardEstimator
from pmm1.strategy.universe import MarketMetadata, select_universe
from pmm1.ws.market_ws import MarketWebSocket
from pmm1.ws.user_ws import UserWebSocket

logger: structlog.stdlib.BoundLogger = None  # type: ignore


def _task_done_callback(task: asyncio.Task[Any]) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        structlog.get_logger(__name__).error(
            "background_task_failed",
            task_name=task.get_name(),
            error=str(exc),
            exc_info=exc,
        )


class _LRUDedup:
    """LRU-based deduplication to replace clear-all-at-500 set."""

    def __init__(self, maxsize: int = 2000):
        self._seen: OrderedDict[str, bool] = OrderedDict()
        self._maxsize = maxsize

    def check_and_add(self, key: str) -> bool:
        """Returns True if duplicate (already seen)."""
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        self._seen[key] = True
        while len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)
        return False


def _emit_market_telemetry(
    telemetry: MarketTelemetry,
    *,
    kind: str,
    stage: str,
    reason: str,
    condition_id: str,
    token_id: str = "",
    side: str = "BOTH",
    question: str = "",
    **details: Any,
) -> None:
    """Emit rate-limited structured market telemetry."""
    event = MarketTelemetryEvent(
        kind=kind,
        stage=stage,
        reason=reason,
        condition_id=condition_id,
        token_id=token_id,
        side=side,
        question=question[:120] if question else "",
        details={key: value for key, value in details.items() if value is not None},
    )
    if telemetry.record(event):
        logger.info(
            kind,
            stage=stage,
            reason=reason,
            condition_id=condition_id,
            token_id=token_id[:16] if token_id else "",
            side=side,
            question=event.question,
            **event.details,
        )


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
        self.fill_escalator = FillEscalator(
            settings.exit.fill_escalation  # type: ignore[arg-type]
        )

        # Risk
        from pmm1.risk.correlation import ThematicCorrelation
        self.correlation = ThematicCorrelation(per_theme_nav=0.15)
        self.risk_limits = RiskLimits(
            settings.risk, self.position_tracker, self.inventory_manager,
            correlation=self.correlation,
        )
        self.kill_switch = KillSwitch(
            ws_stale_kill_s=settings.execution.ws_stale_kill_s,
        )
        self.drawdown = DrawdownGovernor(settings.risk)
        self.resolution_risk = ResolutionRiskManager()

        # Analytics
        self.metrics = MetricsCollector()
        self.pnl_tracker = PnLTracker()
        self.market_telemetry = MarketTelemetry()

        # Market universe
        self.universe: list[MarketMetadata] = []
        self.active_markets: dict[str, MarketMetadata] = {}  # condition_id → metadata
        self.tick_sizes: dict[str, Decimal] = {}  # token_id → tick_size
        self.neg_risk_events: dict[str, list[NegRiskOutcome]] = {}  # event_id → outcomes
        self.rest_book_cache: dict[str, Any] = {}  # REST book cache
        self.rest_book_cache_ts: dict[str, float] = {}  # REST book cache timestamps

        # Sampling (reward-eligible) condition IDs
        self.reward_eligible: set[str] = set()
        self.pmm2_runtime: Any = None
        self.gamma_client: Any = None
        self.rewards_client: Any = None
        self.order_manager: OrderManager | None = None
        self.spine: SpineEmitter | None = None
        self.spine_store: PostgresStore | None = None
        self.spine_stream_store: RedisStateStore | None = None
        self.order_fact_materializer: OrderFactMaterializer | None = None
        self.fill_fact_materializer: FillFactMaterializer | None = None
        self.book_snapshot_fact_materializer: BookSnapshotFactMaterializer | None = None
        self.quote_fact_materializer: QuoteFactMaterializer | None = None
        self.shadow_cycle_fact_materializer: ShadowCycleFactMaterializer | None = None
        self.canary_cycle_fact_materializer: CanaryCycleFactMaterializer | None = None
        self.session_id: str = ""
        self.git_sha: str = "unknown"
        self.config_hash: str = ""
        self.nav: float = 0.0
        self.resume_token_valid: bool = False
        self.resume_blocked_reason: str = ""
        self.resume_token_invalidated_at: float = 0.0

    def eligible_markets(self) -> list[MarketMetadata]:
        """Get markets eligible for quoting in current mode."""
        if self.mode in {"FLATTEN_ONLY", "PAUSED"}:
            return []  # No new quotes
        if self.mode in ("SHUTDOWN", "STARTUP"):
            return []
        markets = list(self.active_markets.values())
        runtime = getattr(self, "pmm2_runtime", None)
        if runtime is None:
            return markets
        return [
            md
            for md in markets
            if not runtime.should_v1_skip_market(md.condition_id)
        ]


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
    config_context = {
        "base_config_path": settings.config_base_path,
        "override_config_path": settings.config_override_path,
        "resolved_config_path": settings.resolved_config_path,
        "config_hash": settings.config_hash,
    }
    logger.info("config_loaded", **config_context)
    session_id = make_session_id()
    git_sha = resolve_git_sha()

    # ── Risk overcommit validation ──
    max_possible = settings.bot.max_markets * settings.risk.per_market_gross_nav
    if max_possible > 0.80:
        logger.warning(
            "risk_overcommit_warning",
            max_markets=settings.bot.max_markets,
            per_market_pct=settings.risk.per_market_gross_nav,
            combined_pct=f"{max_possible:.0%}",
            message=(
                "Combined market limits exceed 80% of NAV"
                " — reduce max_markets or per_market_gross_nav"
            ),
        )

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
    state.session_id = session_id
    state.git_sha = git_sha
    state.config_hash = settings.config_hash
    alert_manager = AlertManager(default_cooldown_s=settings.ops.alert_cooldown_s)
    ops_monitor = OpsMonitor(settings.ops, alert_manager=alert_manager)
    ops_monitor.write_lifecycle_status(
        mode=state.mode,
        paper_mode=settings.bot.paper_mode,
        note="startup",
        kill_switch=state.kill_switch.get_status(),
        config_context=config_context,
        runtime_safety={
            "resume_token_valid": state.resume_token_valid,
            "resume_blocked_reason": state.resume_blocked_reason,
            "resume_token_invalidated_at": state.resume_token_invalidated_at,
            "last_successful_full_reconciliation_at": 0.0,
        },
    )

    spine_store: PostgresStore | None = None
    spine_stream_store: RedisStateStore | None = None
    spine: SpineEmitter | None = None
    try:
        spine_store = PostgresStore(settings.storage.postgres_dsn)
        await spine_store.connect()
        try:
            spine_stream_store = RedisStateStore(settings.storage.redis_url)
            await spine_stream_store.connect()
        except Exception as stream_error:
            logger.warning(
                "spine_stream_init_failed",
                redis_url=settings.storage.redis_url,
                error=str(stream_error),
            )
            spine_stream_store = None
        bootstrap_snapshot = await SpineEmitter(
            spine_store,
            session_id=session_id,
            git_sha=git_sha,
            config_hash="bootstrap",
            default_controller="v1",
            default_run_stage="paper" if settings.bot.paper_mode else "production",
            stream_store=spine_stream_store,
        ).persist_config_snapshot(settings.raw_config)
        if bootstrap_snapshot is not None:
            spine = SpineEmitter(
                spine_store,
                session_id=session_id,
                git_sha=git_sha,
                config_hash=bootstrap_snapshot.config_hash,
                default_controller="v1",
                default_run_stage="paper" if settings.bot.paper_mode else "production",
                stream_store=spine_stream_store,
            )
            state.config_hash = bootstrap_snapshot.config_hash
            config_context["config_hash"] = bootstrap_snapshot.config_hash
            state.spine = spine
            state.spine_store = spine_store
            state.spine_stream_store = spine_stream_store
            logger.info(
                "spine_initialized",
                session_id=session_id,
                git_sha=git_sha,
                config_hash=bootstrap_snapshot.config_hash,
                run_stage="paper" if settings.bot.paper_mode else "production",
            )
            if spine_store is not None and spine_stream_store is not None:
                state.order_fact_materializer = OrderFactMaterializer(
                    spine_store,
                    spine_stream_store,
                    consumer_name=f"{settings.bot.name.lower()}-{session_id}",
                )
                state.fill_fact_materializer = FillFactMaterializer(
                    spine_store,
                    spine_stream_store,
                    consumer_name=f"{settings.bot.name.lower()}-{session_id}",
                )
                state.book_snapshot_fact_materializer = BookSnapshotFactMaterializer(
                    spine_store,
                    spine_stream_store,
                    consumer_name=f"{settings.bot.name.lower()}-{session_id}",
                )
                state.quote_fact_materializer = QuoteFactMaterializer(
                    spine_store,
                    spine_stream_store,
                    consumer_name=f"{settings.bot.name.lower()}-{session_id}",
                )
                state.shadow_cycle_fact_materializer = ShadowCycleFactMaterializer(
                    spine_store,
                    spine_stream_store,
                    consumer_name=f"{settings.bot.name.lower()}-{session_id}",
                )
                state.canary_cycle_fact_materializer = CanaryCycleFactMaterializer(
                    spine_store,
                    spine_stream_store,
                    consumer_name=f"{settings.bot.name.lower()}-{session_id}",
                )
    except Exception as e:
        logger.warning(
            "spine_init_failed",
            dsn=settings.storage.postgres_dsn,
            error=str(e),
        )
        if spine_store is not None:
            try:
                await spine_store.close()
            except Exception:
                pass
        if spine_stream_store is not None:
            try:
                await spine_stream_store.close()
            except Exception:
                pass

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
                event_id=market.primary_event_id,
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
                fee_rate=market.fee_rate if market.fee_rate > 0 else 0.0,
                fee_known=not market.fees_enabled or market.fee_rate > 0,
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
                    fee_rate=market.fee_rate if market.fee_rate > 0 else 0.0,
                    fee_known=not market.fees_enabled or market.fee_rate > 0,
                )
                # S0-4: Log fee-enabled markets
                if market.fees_enabled:
                    logger.info(
                        "fee_market_found",
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
                reward_info = (
                    token_to_reward.get(m.token_id_yes)
                    or token_to_reward.get(m.token_id_no)
                )
            if reward_info:
                m.reward_eligible = True
                m.reward_daily_rate = reward_info.daily_rate
                m.reward_min_size = reward_info.min_size
                m.reward_max_spread = reward_info.max_spread
                state.reward_eligible.add(m.condition_id)
                matched += 1
        logger.info(
            "reward_matching_done",
            matched=matched,
            total=len(all_markets),
            token_index_size=len(token_to_reward),
        )
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

    # ── 5. Select + audit universe ──
    selection_pool_size = max(settings.bot.max_markets * 3, settings.bot.max_markets)
    ranked_candidates = select_universe(
        all_markets,
        settings.market_filters,
        settings.universe_weights,
        max_markets=selection_pool_size,
    )
    market_audit = audit_active_markets(
        ranked_candidates,
        settings.market_filters,
        settings.bot.max_markets,
        classify_theme=state.correlation.classify,
    )

    for entry in market_audit.entries:
        if entry.selected:
            continue
        for reason in entry.reasons:
            _emit_market_telemetry(
                state.market_telemetry,
                kind="market_selection_rejected",
                stage="startup_universe_audit",
                reason=reason,
                condition_id=entry.condition_id,
                question=entry.question,
                event_id=entry.event_id,
                theme=entry.theme,
                score=round(entry.score, 4),
                spread_cents=round(entry.spread_cents, 2),
                liquidity=round(entry.liquidity, 2),
                reward_eligible=entry.reward_eligible,
                reward_capture_ok=entry.reward_capture_ok,
                hours_to_end=round(entry.hours_to_end, 2)
                if entry.hours_to_end != float("inf")
                else None,
            )

    logger.info(
        "active_market_audit_summary",
        candidates=len(ranked_candidates),
        selected=len(market_audit.selected),
        reason_counts=market_audit.reason_counts,
        event_counts=market_audit.event_counts,
        theme_counts=market_audit.theme_counts,
        reward_capture_selected=market_audit.reward_capture_selected,
        avg_spread_cents=round(market_audit.avg_selected_spread_cents, 2),
        avg_liquidity=round(market_audit.avg_selected_liquidity, 2),
    )

    for scored in market_audit.selected:
        md = scored.metadata
        md.universe_score = scored.score
        md.universe_rank = scored.rank

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

    # ── Enrich event_ids from individual market data ──
    enriched_event_ids = 0
    for md in list(state.active_markets.values()):
        if not md.event_id:
            try:
                # Gamma /markets?conditionId=X returns market with nested events array
                session = await gamma._get_session()
                url = f"{gamma.base_url}/markets"
                params = {"conditionId": md.condition_id}
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and isinstance(data, list) and len(data) > 0:
                            events = data[0].get("events", [])
                            if events and isinstance(events, list) and len(events) > 0:
                                event_id = str(events[0].get("id", ""))
                                if event_id:
                                    md.event_id = event_id
                                    enriched_event_ids += 1
                                    logger.debug(
                                        "event_id_enriched",
                                        condition_id=md.condition_id[:16],
                                        event_id=event_id,
                                    )
            except Exception as e:
                logger.warning(
                    "event_id_enrichment_failed",
                    condition_id=md.condition_id[:16],
                    error=str(e),
                )

    # Re-register event groupings for any newly enriched event_ids
    for md in state.active_markets.values():
        state.position_tracker.register_market(
            md.condition_id,
            md.token_id_yes,
            md.token_id_no,
            neg_risk=md.neg_risk,
            event_id=md.event_id,
        )
        if md.neg_risk and md.event_id and md.event_id not in state.neg_risk_events:
            state.neg_risk_events[md.event_id] = []
            # Find all markets with this event_id
            for other_md in state.active_markets.values():
                if other_md.event_id == md.event_id and other_md.neg_risk:
                    state.neg_risk_events[md.event_id].append(NegRiskOutcome(
                        condition_id=other_md.condition_id,
                        token_id_yes=other_md.token_id_yes,
                        token_id_no=other_md.token_id_no,
                        index=len(state.neg_risk_events[md.event_id]),
                    ))

    if enriched_event_ids:
        logger.info("event_ids_enriched", count=enriched_event_ids)

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
            # USDC has 6 decimals on Polygon.
            # If raw_balance looks like wei (> 1_000_000), convert to dollars.
            # Threshold 1M avoids misclassifying $1500 as $0.0015.
            balance = raw_balance / 1e6 if raw_balance > 1_000_000 else raw_balance
            allowance = raw_allowance / 1e6 if raw_allowance > 1_000_000 else raw_allowance
            state.inventory_manager.update_balances(balance, allowance)
            logger.info(
                "balances_loaded",
                balance=f"${balance:.2f}",
                allowance=f"${allowance:.2f}",
                raw=raw_balance,
            )
        except Exception as e:
            logger.warning("balances_load_failed", error=str(e))

        try:
            open_orders = await clob_private.get_open_orders()
            logger.info("open_orders_loaded", count=len(open_orders))
            imported_open_orders = state.order_tracker.import_exchange_orders(
                open_orders,
                source="startup_sync",
            )
            if imported_open_orders:
                logger.info(
                    "open_orders_seeded_into_tracker",
                    imported_count=len(imported_open_orders),
                )
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
            spine_emitter=state.spine,
        )
        state.order_manager = order_manager
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
            spine_emitter=state.spine,
        )
        reconciler.set_kill_switch(state.kill_switch)
        reconciler.set_on_mismatch(ops_monitor.record_reconciliation_mismatch)
        mutation_guard = LiveMutationGuard(
            risk_limits=state.risk_limits,
            inventory_manager=state.inventory_manager,
            kill_switch=state.kill_switch,
            drawdown=state.drawdown,
            heartbeat=heartbeat,
            market_getter=state.active_markets.get,
            resume_token_getter=lambda: state.resume_token_valid,
            resume_reason_getter=lambda: state.resume_blocked_reason,
        )
        order_manager.set_mutation_guard(
            lambda request, condition_id, strategy: mutation_guard.evaluate(
                request,
                condition_id=condition_id,
                strategy=strategy,
            )
        )

    def _runtime_safety_context() -> dict[str, Any]:
        reconciler_stats = reconciler.get_stats() if reconciler else {}
        return {
            "resume_token_valid": bool(
                reconciler_stats.get("resume_token_valid", state.resume_token_valid)
            ),
            "resume_blocked_reason": str(
                reconciler_stats.get("resume_invalid_reason", state.resume_blocked_reason) or ""
            ),
            "resume_token_invalidated_at": float(
                reconciler_stats.get(
                    "last_resume_invalidated_at",
                    state.resume_token_invalidated_at,
                )
                or 0.0
            ),
            "last_successful_full_reconciliation_at": float(
                reconciler_stats.get("last_successful_full_reconciliation_at", 0.0) or 0.0
            ),
        }

    # ── Initialize ExitManager ──
    exit_manager = ExitManager(
        config=settings.exit,
        position_tracker=state.position_tracker,
        book_manager=state.book_manager,
        kill_switch=state.kill_switch,
        clob_public=clob_public,
    )

    parquet_writer = ParquetWriter(settings.storage.parquet_dir)
    recorder = LiveRecorder(parquet_writer=parquet_writer)

    async def _on_kill_switch(reason: str, message: str) -> None:
        await alert_manager.critical(
            "KILL SWITCH",
            f"{reason}: {message}",
            dedupe_key=f"kill_switch:{reason}",
        )
        if state.spine is not None:
            await state.spine.emit_event(
                event_type="kill_switch_triggered",
                strategy="ops",
                payload_json={
                    "reason": reason,
                    "message": message,
                    "active_reasons": [r.value for r in state.kill_switch.active_reasons],
                },
            )

    async def _on_drawdown_tier(old_tier: str, new_tier: str, dd_pct: float) -> None:
        details = f"{old_tier}→{new_tier}, DD: {dd_pct:.1f}%"
        if new_tier == "normal":
            await alert_manager.info(
                "DRAWDOWN",
                details,
                dedupe_key="drawdown:recovered",
            )
        elif new_tier == "tier3_flatten_only":
            await alert_manager.critical(
                "DRAWDOWN",
                details,
                dedupe_key="drawdown:tier3",
            )
        else:
            await alert_manager.warning(
                "DRAWDOWN",
                details,
                dedupe_key=f"drawdown:{new_tier}",
            )
        if state.spine is not None:
            await state.spine.emit_event(
                event_type="drawdown_tier_changed",
                strategy="ops",
                payload_json={
                    "old_tier": old_tier,
                    "new_tier": new_tier,
                    "drawdown_pct": dd_pct,
                },
            )

    state.kill_switch.set_on_trigger(_on_kill_switch)
    state.drawdown.set_on_tier_change(_on_drawdown_tier)

    # ── Initialize PMM-2 data collection (Sprint 1) ──
    db = Database("data/pmm1.db")
    await db.init()
    fill_recorder = FillRecorder(db, state.book_manager)
    book_recorder = BookRecorder(db)

    db.set_on_write_failure(ops_monitor.record_db_write_failure)
    fill_recorder.set_on_failure(ops_monitor.record_fill_recorder_failure)

    # ── Initialize PMM-2 runtime (Sprint 7) ──
    from pmm2.runtime.integration import (
        maybe_init_pmm2,
        pmm2_on_book_delta,
        pmm2_on_fill,
        pmm2_on_order_canceled,
        pmm2_on_order_live,
    )
    pmm2_runtime = await maybe_init_pmm2(settings, db, state)
    state.pmm2_runtime = pmm2_runtime

    # ── V3 fair value integration (if enabled) ──
    v3_integrator = None
    if settings.v3.enabled:
        try:
            from v3.canary.integrator import V3Integrator
            v3_integrator = V3Integrator(
                redis_url=settings.v3.redis_url,
                max_skew_cents=settings.v3.max_skew_cents,
                min_confidence=settings.v3.min_confidence,
                max_age_seconds=settings.v3.signal_max_age_seconds,
                blend_weight=settings.v3.blend_weight,
                enabled=True,
            )
            await v3_integrator.connect()
            logger.info("v3_integrator_initialized")
        except Exception as e:
            logger.warning("v3_integrator_init_failed", error=str(e))
            v3_integrator = None

    # ── Paper 2 math wiring: EdgeTracker + FairValueCalibrator ──
    edge_tracker = EdgeTracker(min_trades=50, target_edge=0.05)
    fv_calibrator = FairValueCalibrator(min_samples=100)
    resolution_recorder = ResolutionRecorder(edge_tracker, fv_calibrator)

    # ── Ensure data directory exists for analytics persistence ──
    import os
    os.makedirs("data", exist_ok=True)

    # ── Phase 3-6 analytics wiring ──
    from pmm1.analytics.carry_tracker import InventoryCarryTracker
    from pmm1.analytics.market_profitability import MarketProfitabilityTracker
    from pmm1.analytics.markout_tracker import MarkoutTracker
    from pmm1.analytics.post_mortem import TradePostMortem
    from pmm1.analytics.signal_value import SignalValueTracker
    from pmm1.analytics.spread_optimizer import SpreadOptimizer
    from pmm1.analytics.var_calculator import VaRReporter
    from pmm1.math.changepoint import BayesianChangePointDetector
    from pmm1.math.kelly import shrinkage_factor as _kelly_shrinkage_factor

    carry_tracker = InventoryCarryTracker()
    spread_optimizer = SpreadOptimizer(
        default_spread=settings.pricing.base_half_spread_cents / 100.0,
    )
    spread_optimizer.load("data/spread_optimizer_state.json")
    market_profitability = MarketProfitabilityTracker()
    market_profitability.load("data/market_profitability.json")
    signal_value_tracker = SignalValueTracker()
    signal_value_tracker.load("data/signal_value.json")
    post_mortem = TradePostMortem()
    post_mortem.load("data/post_mortem.json")
    markout_tracker = MarkoutTracker()
    var_reporter = VaRReporter()
    changepoint_detector = BayesianChangePointDetector()

    # CL-03: Load edge tracker state from disk
    edge_tracker.load(settings.analytics.edge_tracker_persist_path)
    logger.info(
        "analytics_modules_initialized",
        edge_tracker_trades=len(edge_tracker.trades),
    )

    # ── Attach analytics instances to state for cross-module access ──
    state.market_profitability = market_profitability  # CL-02: LLM priority scoring
    state._fv_calibrator = fv_calibrator  # CL-04: conditional calibration bias

    # ── Embedded Opus reasoner (OAuth, background loop) ──
    from pmm1.strategy.market_context import MarketContextBuilder
    from pmm1.strategy.news_fetcher import NewsFetcher
    from pmm1.strategy.reasoner_memory import ReasonerMemory

    llm_reasoner_config = ReasonerConfig.from_env()
    llm_context_builder = MarketContextBuilder(
        data_api=data_api,
    )
    llm_news_fetcher = NewsFetcher.from_env()
    llm_memory = ReasonerMemory(
        persist_path="data/reasoner_memory.json",
    )
    llm_reasoner = LLMReasoner(
        llm_reasoner_config,
        bot_state=state,
        context_builder=llm_context_builder,
        memory=llm_memory,
        news_fetcher=llm_news_fetcher,
    )
    if llm_reasoner_config.enabled:
        await llm_reasoner.start()

    async def emit_spine_event(
        *,
        event_type: str,
        strategy: str,
        controller: str | None = None,
        run_stage: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        order_id: str | None = None,
        payload_json: dict[str, Any] | None = None,
    ) -> None:
        if state.spine is None:
            return
        await state.spine.emit_event(
            event_type=event_type,
            strategy=strategy or "system",
            controller=controller,
            run_stage=run_stage,
            condition_id=condition_id,
            token_id=token_id,
            order_id=order_id,
            payload_json=payload_json or {},
        )

    async def emit_quote_intent_events(
        intent: QuoteIntent,
        *,
        fair_value: float,
        inventory: float,
        question: str,
        stage: str,
    ) -> None:
        side_specs = [
            ("BUY", intent.bid_price, intent.bid_size, intent.bid_suppression_reasons),
            ("SELL", intent.ask_price, intent.ask_size, intent.ask_suppression_reasons),
        ]
        for side, price, size, reasons in side_specs:
            deduped_reasons = list(dict.fromkeys(reasons))
            if price is not None and size is not None and size > 0:
                await emit_spine_event(
                    event_type="quote_intent_created",
                    strategy=intent.strategy or "mm",
                    condition_id=intent.condition_id or None,
                    token_id=intent.token_id,
                    payload_json={
                        "stage": stage,
                        "side": side,
                        "intended_price": price,
                        "intended_size": size,
                        "reservation_price": intent.reservation_price,
                        "half_spread": intent.half_spread,
                        "fair_value": fair_value,
                        "confidence": intent.confidence,
                        "inventory": inventory,
                        "neg_risk": intent.neg_risk,
                        "question": question[:120] if question else "",
                    },
                )
            elif deduped_reasons:
                await emit_spine_event(
                    event_type="quote_side_suppressed",
                    strategy=intent.strategy or "mm",
                    condition_id=intent.condition_id or None,
                    token_id=intent.token_id,
                    payload_json={
                        "stage": stage,
                        "side": side,
                        "reasons": deduped_reasons,
                        "reservation_price": intent.reservation_price,
                        "half_spread": intent.half_spread,
                        "fair_value": fair_value,
                        "confidence": intent.confidence,
                        "inventory": inventory,
                        "neg_risk": intent.neg_risk,
                        "question": question[:120] if question else "",
                    },
                )

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

    async def on_book_delta(token_id: str, price: float, old_size: float, new_size: float) -> None:
        """Callback when a single book level changes from WS."""
        pmm2_on_book_delta(pmm2_runtime, token_id, price, old_size, new_size)

    async def on_trade(token_id: str, price: float) -> None:
        """Callback when a trade occurs."""
        state.feature_engine.record_trade(token_id, price, 0.0, "UNKNOWN")
        recorder.record_trade(token_id, price, 0.0, "UNKNOWN")

    async def on_tick_change(token_id: str, tick_size: Decimal) -> None:
        """Callback when a token's tick size changes."""
        condition_id = None
        for md in state.active_markets.values():
            if md.token_id_yes == token_id or md.token_id_no == token_id:
                condition_id = md.condition_id
                break
        await emit_spine_event(
            event_type="tick_size_changed",
            strategy="market_data",
            condition_id=condition_id,
            token_id=token_id,
            payload_json={"tick_size": str(tick_size)},
        )

    def _invalidate_resume_token(reason: str) -> None:
        if reconciler is not None:
            reconciler.invalidate_resume_token(reason)
            reconciler_stats = reconciler.get_stats()
            state.resume_token_valid = bool(reconciler_stats.get("resume_token_valid", False))
            state.resume_blocked_reason = str(
                reconciler_stats.get("resume_invalid_reason", reason) or reason
            )
            state.resume_token_invalidated_at = float(
                reconciler_stats.get("last_resume_invalidated_at", time.time()) or time.time()
            )
        if state.resume_token_valid or state.resume_blocked_reason != reason:
            logger.warning("quote_resume_blocked", reason=reason)
        state.resume_token_valid = False
        state.resume_blocked_reason = reason
        state.resume_token_invalidated_at = time.time()

    def _mark_resume_token_valid(reason: str) -> None:
        if reconciler is not None:
            reconciler_stats = reconciler.get_stats()
            state.resume_token_valid = bool(reconciler_stats.get("resume_token_valid", False))
            state.resume_blocked_reason = str(
                reconciler_stats.get("resume_invalid_reason", "") or ""
            )
            state.resume_token_invalidated_at = float(
                reconciler_stats.get(
                    "last_resume_invalidated_at",
                    state.resume_token_invalidated_at,
                )
                or state.resume_token_invalidated_at
            )
        if not state.resume_token_valid:
            logger.warning(
                "quote_resume_unblock_skipped",
                reason=reason,
                blocked_reason=state.resume_blocked_reason,
            )
            return
        logger.info("quote_resume_unblocked", reason=reason)

    async def _rebuild_rest_books_for_active_tokens() -> bool:
        token_ids = market_ws.subscribed_assets or [
            token_id
            for md in state.active_markets.values()
            for token_id in (md.token_id_yes, md.token_id_no)
            if token_id
        ]
        if not token_ids:
            return True

        failures: list[str] = []
        for token_id in sorted(set(token_ids)):
            try:
                rest_book = await clob_public.get_order_book(token_id)
                if not rest_book or not rest_book.bids or not rest_book.asks:
                    raise RuntimeError("empty_rest_book")
                cache_key = f"rest_book_{token_id}"
                state.rest_book_cache[cache_key] = _build_cached_rest_book(token_id, rest_book)
                state.rest_book_cache_ts[cache_key] = time.time()
            except Exception as book_error:
                failures.append(f"{token_id[:16]}:{book_error}")
                logger.warning(
                    "post_reconnect_rest_book_refresh_failed",
                    token_id=token_id[:16],
                    error=str(book_error),
                )

        return not failures

    async def _handle_transport_recovery(source: str, *, rebuild_market_books: bool) -> None:
        _invalidate_resume_token(f"{source}_reconnect")
        if rebuild_market_books:
            logger.info("post_reconnect_rest_book_refresh_started", source=source)
            books_ok = await _rebuild_rest_books_for_active_tokens()
            if not books_ok:
                logger.warning("post_reconnect_rest_book_refresh_incomplete", source=source)
                return
        if reconciler:
            logger.info("post_reconnect_reconciliation", source=source)
            recovery_result = await reconciler.full_reconciliation()
            if not recovery_result.success:
                logger.warning(
                    "post_reconnect_reconciliation_failed",
                    source=source,
                    errors=recovery_result.errors,
                )
                _invalidate_resume_token(f"{source}_reconciliation_failed")
                return
        _mark_resume_token_valid(source)

    async def on_market_reconnect() -> None:
        """Callback after market WS reconnect — rebuild books and reconcile."""
        await _handle_transport_recovery("market_ws", rebuild_market_books=True)

    async def on_user_reconnect() -> None:
        """Callback after user WS reconnect — reconcile before resuming quotes."""
        await _handle_transport_recovery("user_ws", rebuild_market_books=False)

    # Track notified fills to prevent duplicate notifications (LRU dedup)
    _fill_dedup = _LRUDedup(maxsize=2000)
    _llm_fill_count = 0
    _pending_fill_events: dict[str, dict[str, Any]] = {}
    _pending_fill_drain_task: asyncio.Task[Any] | None = None

    def _fill_fee_details(
        fill_msg: dict[str, Any],
        market_md: MarketMetadata | None,
        *,
        price: float,
        size: float,
    ) -> tuple[float | None, bool, str]:
        payload_fee = fill_msg.get("fee_amount")
        if payload_fee is not None:
            return float(payload_fee), True, str(fill_msg.get("fee_source") or "payload")
        if market_md is not None and getattr(market_md, "fee_known", False):
            fee_rate = float(getattr(market_md, "fee_rate", 0.0) or 0.0)
            return price * size * fee_rate, True, "market_metadata"
        return None, False, str(fill_msg.get("fee_source") or "unknown")

    async def _lookup_trade_fee(fill_msg: dict[str, Any]) -> tuple[float | None, str]:
        token_id = str(fill_msg.get("token_id") or "")
        if not token_id or not settings.wallet.address:
            return None, "unknown"
        try:
            trades = await data_api.get_trades(
                address=settings.wallet.address,
                asset_id=token_id,
                limit=100,
            )
        except Exception as fee_error:
            logger.warning(
                "trade_fee_lookup_failed",
                token_id=token_id[:16],
                order_id=str(fill_msg.get("order_id") or "")[:16],
                error=str(fee_error),
            )
            return None, "unknown"

        exchange_trade_id = str(fill_msg.get("exchange_trade_id") or "")
        target_order_id = str(fill_msg.get("order_id") or "")
        target_price = float(fill_msg.get("price", 0.0) or 0.0)
        target_size = float(fill_msg.get("size", 0.0) or 0.0)
        for trade in trades:
            if exchange_trade_id and trade.id == exchange_trade_id:
                return trade.fee_float, "data_api_trade"
            if (
                trade.asset_id == token_id
                and abs(trade.price_float - target_price) < 1e-9
                and abs(trade.size_float - target_size) < 1e-9
                and (not target_order_id or trade.taker_order_id == target_order_id)
            ):
                return trade.fee_float, "data_api_trade"
        return None, "unknown"

    async def _persist_pending_fill(
        fill_msg: dict[str, Any],
        *,
        tracked: Any | None,
        ingest_state: str,
        fee: float | None,
        fee_known: bool,
        fee_source: str,
    ) -> int:
        order_id = str(fill_msg.get("order_id") or "")
        token_id = str(
            fill_msg.get("token_id")
            or (getattr(tracked, "token_id", "") if tracked else "")
            or ""
        )
        condition_id = str(
            fill_msg.get("condition_id")
            or (getattr(tracked, "condition_id", "") if tracked else "")
            or ""
        )
        side = str(
            fill_msg.get("side")
            or (getattr(tracked, "side", "") if tracked else "")
            or ""
        ).upper()
        market_md = (
            state.active_markets.get(condition_id)
            if condition_id else None
        )
        reward_eligible = (
            market_md.reward_eligible
            if market_md
            else condition_id in state.reward_eligible
        )
        is_scoring = bool(getattr(tracked, "is_scoring", False))
        book = state.book_manager.get(token_id) if token_id else None
        mid_at_fill = book.get_midpoint() if book else None
        return await fill_recorder.record_fill(
            ts=datetime.now(UTC),
            condition_id=condition_id,
            token_id=token_id,
            order_id=order_id,
            side=side or "UNKNOWN",
            price=float(fill_msg.get("price", 0.0) or 0.0),
            size=float(fill_msg.get("size", 0.0) or 0.0),
            fee=fee,
            mid_at_fill=mid_at_fill,
            is_scoring=is_scoring,
            reward_eligible=reward_eligible,
            exchange_trade_id=str(fill_msg.get("exchange_trade_id") or ""),
            fill_identity=str(fill_msg.get("fill_identity") or ""),
            fee_known=fee_known,
            fee_source=fee_source,
            ingest_state=ingest_state,
            raw_event_json=dict(fill_msg.get("raw_msg") or {}),
        )

    async def _apply_fill_once(
        fill_msg: dict[str, Any],
        *,
        tracked: Any,
        fee: float,
        fee_known: bool,
        fee_source: str,
        pending_fill_id: int | None = None,
    ) -> bool:
        order_id = str(fill_msg.get("order_id") or tracked.order_id or "")
        token_id = str(fill_msg.get("token_id") or tracked.token_id or "")
        condition_id = str(fill_msg.get("condition_id") or tracked.condition_id or "")
        side = str(fill_msg.get("side") or tracked.side or "").upper()
        price = float(fill_msg.get("price", 0.0) or 0.0)
        size = float(fill_msg.get("size", 0.0) or 0.0)
        market_md = state.active_markets.get(condition_id) if condition_id else None
        market_question = market_md.question if market_md else (condition_id or "")
        dollar_value = size * price
        reward_eligible = (
            market_md.reward_eligible
            if market_md
            else condition_id in state.reward_eligible
        )
        is_scoring = bool(tracked.is_scoring)
        book = state.book_manager.get(token_id) if token_id else None
        mid_at_fill = book.get_midpoint() if book else None

        logger.info(
            "fill_confirmed",
            token_id=token_id[:16] if token_id else "?",
            order_id=order_id[:16] if order_id else "?",
            side=side,
            price=price,
            size=size,
            dollar_value=round(dollar_value, 2),
            market=market_question[:40] if market_question else "?",
            fee_source=fee_source,
        )

        if condition_id:
            pos = (
                state.position_tracker.get(condition_id)
                or state.position_tracker.get_by_token(token_id)
            )
            if pos:
                pos.apply_fill(token_id, "BUY" if side == "BUY" else "SELL", size, price, fee=fee)
            else:
                logger.warning(
                    "fill_position_untracked",
                    order_id=order_id[:16],
                    condition_id=condition_id[:16],
                    token_id=token_id[:16],
                )

        if pending_fill_id is not None:
            await fill_recorder.resolve_pending_fill(
                pending_fill_id,
                condition_id=condition_id,
                token_id=token_id,
                order_id=order_id,
                side=side,
                fee=fee,
                fee_known=fee_known,
                fee_source=fee_source,
                mid_at_fill=mid_at_fill,
                is_scoring=is_scoring,
                reward_eligible=reward_eligible,
            )
        elif condition_id and token_id:
            _t = asyncio.create_task(
                fill_recorder.record_fill(
                    ts=datetime.now(UTC),
                    condition_id=condition_id,
                    token_id=token_id,
                    order_id=order_id,
                    side=side,
                    price=price,
                    size=size,
                    fee=fee,
                    mid_at_fill=mid_at_fill,
                    is_scoring=is_scoring,
                    reward_eligible=reward_eligible,
                    exchange_trade_id=str(fill_msg.get("exchange_trade_id") or ""),
                    fill_identity=str(fill_msg.get("fill_identity") or ""),
                    fee_known=fee_known,
                    fee_source=fee_source,
                    ingest_state="applied",
                    raw_event_json=dict(fill_msg.get("raw_msg") or {}),
                )
            )
            _t.add_done_callback(_task_done_callback)

        event_type = (
            "order_filled"
            if tracked.state == OrderState.FILLED
            else "order_partially_filled"
        )
        realized_spread_capture = None
        adverse_selection_estimate = None
        if mid_at_fill is not None:
            if side == "BUY":
                realized_spread_capture = mid_at_fill - price
            else:
                realized_spread_capture = price - mid_at_fill
            adverse_selection_estimate = -realized_spread_capture
        await emit_spine_event(
            event_type=event_type,
            strategy=tracked.strategy or "mm",
            condition_id=condition_id,
            token_id=token_id,
            order_id=order_id,
            payload_json={
                "side": side,
                "price": price,
                "size": size,
                "fee": fee,
                "fee_known": fee_known,
                "fee_source": fee_source,
                "exchange_trade_id": str(fill_msg.get("exchange_trade_id") or ""),
                "fill_identity": str(fill_msg.get("fill_identity") or ""),
                "dollar_value": dollar_value,
                "mid_at_fill": mid_at_fill,
                "is_scoring": is_scoring,
                "reward_eligible": reward_eligible,
                "fee_usdc": fee,
                "dollar_value_usdc": dollar_value,
                "realized_spread_capture": realized_spread_capture,
                "adverse_selection_estimate": adverse_selection_estimate,
            },
        )

        pnl_text = ""
        if side == "SELL" and condition_id:
            pos = (
                state.position_tracker.get(condition_id)
                or state.position_tracker.get_by_token(token_id)
            )
            avg_entry = pos.yes_avg_price if pos and pos.yes_avg_price > 0 else 0
            if not avg_entry and pos:
                avg_entry = pos.no_avg_price
            if avg_entry > 0:
                pnl = (price - avg_entry) * size
                pnl_pct = ((price / avg_entry) - 1) * 100
                pnl_emoji = "📈" if pnl >= 0 else "📉"
                pnl_text = f"\n{pnl_emoji} PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%)"

        scoring_badge = " 💰" if is_scoring else ""
        emoji = "🟢" if side == "BUY" else "🔴"
        market_line = f"\n📊 {market_question[:50]}" if market_question else ""
        notification = (
            f"{emoji} *{side or 'FILL'}*{scoring_badge}: {size:.1f} shares @ ${price:.3f}\n"
            f"💵 Value: ${dollar_value:.2f}"
            f"{pnl_text}"
            f"{market_line}"
        )
        _notify_task: asyncio.Task[Any] = asyncio.create_task(
            send_telegram(notification),
        )
        _notify_task.add_done_callback(_task_done_callback)

        if hasattr(state, "fill_escalator"):
            state.fill_escalator.record_fill()

        book_for_pnl = state.book_manager.get(token_id)
        mid_at_fill_pnl = None
        if book_for_pnl:
            bb = book_for_pnl.get_best_bid()
            ba = book_for_pnl.get_best_ask()
            if bb and ba:
                mid_at_fill_pnl = (bb.price_float + ba.price_float) / 2

        from pmm1.analytics.pnl import FillRecord as PnLFillRecord

        pnl_fill = PnLFillRecord(
            order_id=order_id,
            token_id=token_id,
            condition_id=condition_id or "",
            side=side,
            price=price,
            size=size,
            fee=fee,
            strategy=tracked.strategy or "mm",
            fill_timestamp=time.time(),
            mid_at_fill=mid_at_fill_pnl or price,
        )
        if hasattr(state, "pnl_tracker"):
            state.pnl_tracker.record_fill(pnl_fill)

        pmm2_on_fill(
            pmm2_runtime,
            order_id,
            size,
            price,
            token_id=token_id,
            condition_id=condition_id,
        )
        return True

    async def _drain_pending_fill_events(trigger: str) -> None:
        if not _pending_fill_events:
            return
        if reconciler is not None:
            recovery_result = await reconciler.full_reconciliation()
            if not recovery_result.success:
                logger.warning(
                    "pending_fill_reconciliation_failed",
                    trigger=trigger,
                    errors=recovery_result.errors,
                )
                return
        for fill_identity, pending in list(_pending_fill_events.items()):
            tracked = state.order_tracker.get(str(pending.get("order_id") or ""))
            if tracked is None:
                continue
            condition_id = str(pending.get("condition_id") or tracked.condition_id or "")
            market_md = state.active_markets.get(condition_id) if condition_id else None
            fee, fee_known, fee_source = _fill_fee_details(
                pending,
                market_md,
                price=float(pending.get("price", 0.0) or 0.0),
                size=float(pending.get("size", 0.0) or 0.0),
            )
            if not fee_known:
                fee, fee_source = await _lookup_trade_fee(pending)
                fee_known = fee is not None
            if not fee_known or fee is None:
                continue
            applied = await _apply_fill_once(
                pending,
                tracked=tracked,
                fee=float(fee),
                fee_known=True,
                fee_source=fee_source,
                pending_fill_id=int(pending.get("pending_fill_id", 0) or 0) or None,
            )
            if applied:
                _pending_fill_events.pop(fill_identity, None)

    def _schedule_pending_fill_drain(trigger: str) -> None:
        nonlocal _pending_fill_drain_task
        if _pending_fill_drain_task is not None and not _pending_fill_drain_task.done():
            return
        _pending_fill_drain_task = asyncio.create_task(_drain_pending_fill_events(trigger))
        _pending_fill_drain_task.add_done_callback(_task_done_callback)
    unknown_fill_reconciliation_inflight: set[str] = set()

    def _resolve_fill_fee(
        fill_msg: dict[str, Any],
        market_md: MarketMetadata | None,
        *,
        price: float,
        size: float,
    ) -> tuple[float | None, bool, str]:
        payload_fee = fill_msg.get("fee_amount")
        if payload_fee is not None:
            return float(payload_fee), True, str(fill_msg.get("fee_source") or "payload")

        if market_md is None:
            return None, False, "market_missing"

        if not getattr(market_md, "fees_enabled", False):
            return 0.0, True, "zero_fee_market"

        if getattr(market_md, "fee_known", False):
            return (
                price * size * float(getattr(market_md, "fee_rate", 0.0) or 0.0),
                True,
                "market_metadata",
            )

        return None, False, "fee_unknown"

    async def _record_pending_fill(
        fill_msg: dict[str, Any],
        *,
        ingest_state: str,
        tracked: Any | None = None,
    ) -> None:
        order_id = str(fill_msg.get("order_id") or "")
        token_id = str(
            fill_msg.get("token_id")
            or (getattr(tracked, "token_id", "") if tracked else "")
            or ""
        )
        condition_id = str(
            fill_msg.get("condition_id")
            or (getattr(tracked, "condition_id", "") if tracked else "")
            or ""
        )
        side = str(
            fill_msg.get("side")
            or (getattr(tracked, "side", "") if tracked else "")
            or ""
        ).upper()
        price = float(fill_msg.get("price", 0.0) or 0.0)
        size = float(fill_msg.get("size", 0.0) or 0.0)
        market_md = (
            state.active_markets.get(condition_id)
            if condition_id else None
        )
        fee_amount, fee_known, fee_source = _resolve_fill_fee(
            fill_msg,
            market_md,
            price=price,
            size=size,
        )
        book = state.book_manager.get(token_id)
        mid_at_fill = book.get_midpoint() if book else None
        await fill_recorder.record_fill(
            ts=datetime.now(UTC),
            condition_id=condition_id,
            token_id=token_id,
            order_id=order_id,
            side=side,
            price=price,
            size=size,
            fee=fee_amount,
            mid_at_fill=mid_at_fill,
            is_scoring=bool(getattr(tracked, "is_scoring", False)),
            reward_eligible=(
                market_md.reward_eligible
                if market_md
                else condition_id in state.reward_eligible
            ),
            exchange_trade_id=str(fill_msg.get("exchange_trade_id") or ""),
            fill_identity=str(fill_msg.get("fill_identity") or ""),
            fee_known=fee_known,
            fee_source=fee_source,
            ingest_state=ingest_state,
            raw_event_json=fill_msg.get("raw_msg") or {},
        )
        logger.warning(
            "pending_fill_recorded",
            order_id=order_id[:16] if order_id else "?",
            token_id=token_id[:16] if token_id else "?",
            condition_id=condition_id[:16] if condition_id else "?",
            fill_identity=str(fill_msg.get("fill_identity") or "")[:16],
            ingest_state=ingest_state,
        )
        await emit_spine_event(
            event_type=(
                "unknown_order_fill"
                if ingest_state == "pending_unknown_order"
                else "fill_fee_unknown"
            ),
            strategy=(getattr(tracked, "strategy", "") if tracked else "ops") or "ops",
            condition_id=condition_id or None,
            token_id=token_id or None,
            order_id=order_id or None,
            payload_json={
                "exchange_trade_id": fill_msg.get("exchange_trade_id"),
                "fill_identity": fill_msg.get("fill_identity"),
                "side": side,
                "price": price,
                "size": size,
                "fee_amount": fee_amount,
                "fee_known": fee_known,
                "fee_source": fee_source,
                "ingest_state": ingest_state,
            },
        )
        alert_title = (
            "UNKNOWN ORDER FILL"
            if ingest_state == "pending_unknown_order"
            else "FILL FEE UNKNOWN"
        )
        alert_key = (
            "unknown_fill"
            if ingest_state == "pending_unknown_order"
            else "unknown_fill_fee"
        )
        await alert_manager.warning(
            alert_title,
            f"order_id={order_id[:16] or '?'}"
            f" token={token_id[:16] or '?'}"
            f" size={size:.4f} price={price:.4f}",
            dedupe_key=f"{alert_key}:{fill_msg.get('fill_identity', order_id)}",
        )

    async def _record_pending_unknown_fill(fill_msg: dict[str, Any]) -> None:
        await _record_pending_fill(fill_msg, ingest_state="pending_unknown_order")

    async def _apply_fill_effects(
        fill_msg: dict[str, Any],
        tracked: Any,
        *,
        existing_fill_id: int | None = None,
        send_fill_notification: bool = True,
    ) -> None:
        nonlocal _llm_fill_count
        order_id = str(fill_msg.get("order_id") or "")
        token_id = str(fill_msg.get("token_id") or tracked.token_id or "")
        condition_id = str(fill_msg.get("condition_id") or tracked.condition_id or "")
        side = str(fill_msg.get("side") or tracked.side or "").upper()
        price = float(fill_msg.get("price", 0.0) or 0.0)
        size = float(fill_msg.get("size", 0.0) or 0.0)
        market_md = state.active_markets.get(condition_id) if condition_id else None
        market_question = market_md.question if market_md else (condition_id or "")
        fee_amount, fee_known, fee_source = _resolve_fill_fee(
            fill_msg,
            market_md,
            price=price,
            size=size,
        )
        if not fee_known:
            fee_amount, fee_source = await _lookup_trade_fee(fill_msg)
            fee_known = fee_amount is not None
        if not fee_known:
            if existing_fill_id is None:
                await _record_pending_fill(
                    fill_msg,
                    ingest_state="pending_fee_truth",
                    tracked=tracked,
                )
            return
        fee_for_state = float(fee_amount or 0.0)
        dollar_value = size * price

        logger.info(
            "fill_confirmed",
            token_id=token_id[:16] if token_id else "?",
            order_id=order_id[:16] if order_id else "?",
            side=side,
            price=price,
            size=size,
            dollar_value=round(dollar_value, 2),
            market=market_question[:40] if market_question else "?",
            fee_known=fee_known,
            fee_source=fee_source,
        )

        if not fee_known:
            logger.warning(
                "fill_fee_unknown",
                order_id=order_id[:16] if order_id else "?",
                token_id=token_id[:16] if token_id else "?",
                condition_id=condition_id[:16] if condition_id else "?",
                fee_source=fee_source,
            )

        if condition_id:
            pos = (
                state.position_tracker.get(condition_id)
                or state.position_tracker.get_by_token(token_id)
            )
            if pos:
                pos.apply_fill(
                    token_id, "BUY" if side == "BUY" else "SELL",
                    size, price, fee=fee_for_state,
                )
            else:
                logger.warning(
                    "fill_position_untracked",
                    order_id=order_id[:16],
                    condition_id=condition_id[:16] if condition_id else "?",
                    token_id=token_id[:16] if token_id else "?",
                )

        is_scoring = bool(getattr(tracked, "is_scoring", False))
        reward_eligible = (
            market_md.reward_eligible
            if market_md
            else condition_id in state.reward_eligible
        )
        book = state.book_manager.get(token_id)
        mid_at_fill = book.get_midpoint() if book else None

        if existing_fill_id is None:
            existing_fill_id = await fill_recorder.record_fill(
                ts=datetime.now(UTC),
                condition_id=condition_id,
                token_id=token_id,
                order_id=order_id,
                side=side,
                price=price,
                size=size,
                fee=fee_amount,
                mid_at_fill=mid_at_fill,
                is_scoring=is_scoring,
                reward_eligible=reward_eligible,
                exchange_trade_id=str(fill_msg.get("exchange_trade_id") or ""),
                fill_identity=str(fill_msg.get("fill_identity") or ""),
                fee_known=fee_known,
                fee_source=fee_source,
                ingest_state="applied",
                raw_event_json=fill_msg.get("raw_msg") or {},
            )
        else:
            await fill_recorder.resolve_pending_fill(
                existing_fill_id,
                condition_id=condition_id,
                token_id=token_id,
                order_id=order_id,
                side=side,
                fee=fee_amount,
                fee_known=fee_known,
                fee_source=fee_source,
                mid_at_fill=mid_at_fill,
                is_scoring=is_scoring,
                reward_eligible=reward_eligible,
                raw_event_json=fill_msg.get("raw_msg") or {},
            )

        event_type = (
            "order_filled"
            if tracked.state == OrderState.FILLED
            else "order_partially_filled"
        )
        realized_spread_capture = None
        adverse_selection_estimate = None
        if mid_at_fill is not None:
            if side == "BUY":
                realized_spread_capture = mid_at_fill - price
            else:
                realized_spread_capture = price - mid_at_fill
            adverse_selection_estimate = -realized_spread_capture
        await emit_spine_event(
            event_type=event_type,
            strategy=tracked.strategy or "mm",
            condition_id=condition_id,
            token_id=token_id,
            order_id=order_id,
            payload_json={
                "side": side,
                "price": price,
                "size": size,
                "fee": fee_amount,
                "fee_known": fee_known,
                "fee_source": fee_source,
                "exchange_trade_id": fill_msg.get("exchange_trade_id"),
                "fill_identity": fill_msg.get("fill_identity"),
                "dollar_value": dollar_value,
                "mid_at_fill": mid_at_fill,
                "is_scoring": is_scoring,
                "reward_eligible": reward_eligible,
                "fee_usdc": fee_amount,
                "dollar_value_usdc": dollar_value,
                "realized_spread_capture": realized_spread_capture,
                "adverse_selection_estimate": adverse_selection_estimate,
            },
        )

        # Signal attribution: tag fill with LLM state (H5)
        _llm_est = (
            llm_reasoner.get_estimate(condition_id)
            if llm_reasoner else None
        )
        if _llm_est and _llm_est.is_fresh:
            _llm_fill_count += 1
            logger.info(
                "fill_llm_attribution",
                condition_id=condition_id[:16],
                llm_p_calibrated=round(_llm_est.p_calibrated, 4),
                llm_age_s=round(_llm_est.age_seconds, 1),
                fill_price=price,
                side=side,
            )

        # Paper 2 §5: record fill for edge validation AT RESOLUTION TIME
        # (not here -- outcome is unknown until market resolves)
        # F01 fix: previously passed outcome=1.0 if BUY else 0.0 which
        # encoded trade direction, not actual market resolution.
        if resolution_recorder and mid_at_fill is not None:
            resolution_recorder.record_fill(
                condition_id=condition_id,
                predicted_p=mid_at_fill,
                market_p=price,
                pnl=realized_spread_capture or 0.0,
                side=side,
            )

        pnl_text = ""
        if side == "SELL" and condition_id:
            pos = (
                state.position_tracker.get(condition_id)
                or state.position_tracker.get_by_token(token_id)
            )
            avg_entry = (
                pos.yes_avg_price
                if pos and pos.yes_avg_price > 0 else 0
            )
            if not avg_entry and pos:
                avg_entry = pos.no_avg_price
            if avg_entry > 0:
                pnl = (price - avg_entry) * size
                pnl_pct = ((price / avg_entry) - 1) * 100
                pnl_emoji = "📈" if pnl >= 0 else "📉"
                pnl_text = (
                    f"\n{pnl_emoji} PnL:"
                    f" ${pnl:+.2f} ({pnl_pct:+.1f}%)"
                )

        if send_fill_notification:
            scoring_badge = " 💰" if is_scoring else ""
            emoji = "🟢" if side == "BUY" else "🔴"
            market_line = f"\n📊 {market_question[:50]}" if market_question else ""
            notification = (
                f"{emoji} *{side or 'FILL'}*{scoring_badge}: {size:.1f} shares @ ${price:.3f}\n"
                f"💵 Value: ${dollar_value:.2f}"
                f"{pnl_text}"
                f"{market_line}"
            )
            notify_task = asyncio.create_task(send_telegram(notification))
            notify_task.add_done_callback(_task_done_callback)

        if hasattr(state, "fill_escalator"):
            state.fill_escalator.record_fill()

        mid_at_fill_pnl = None
        if book:
            bb = book.get_best_bid()
            ba = book.get_best_ask()
            if bb and ba:
                mid_at_fill_pnl = (bb.price_float + ba.price_float) / 2

        from pmm1.analytics.pnl import FillRecord as PnLFillRecord

        pnl_fill = PnLFillRecord(
            order_id=order_id,
            token_id=token_id,
            condition_id=condition_id or "",
            side=side,
            price=price,
            size=size,
            fee=fee_for_state,
            strategy=tracked.strategy or "mm",
            fill_timestamp=time.time(),
            mid_at_fill=mid_at_fill_pnl or price,
        )
        if hasattr(state, "pnl_tracker"):
            state.pnl_tracker.record_fill(pnl_fill)

        # ── Learning module fill recording ──
        try:
            # CL-02: Market profitability
            if condition_id:
                _fill_vol = price * size
                _fill_pnl = realized_spread_capture or 0.0
                market_profitability.record_fill(
                    condition_id, pnl=_fill_pnl, volume=_fill_vol,
                )

            # CL-06: Signal value tracking
            if condition_id:
                _market_mid = mid_at_fill or price
                _llm_active = bool(_llm_est and _llm_est.is_fresh)
                _blended_fv = _llm_est.p_calibrated if _llm_active else _market_mid
                signal_value_tracker.record_fill(
                    blended_fv=_blended_fv,
                    market_mid=_market_mid,
                    fill_price=price,
                    side=side,
                    pnl=realized_spread_capture or 0.0,
                    llm_used=_llm_active,
                )

            # MM-05: Markout tracking
            if condition_id:
                markout_tracker.record_fill(
                    token_id=token_id,
                    condition_id=condition_id,
                    fill_price=price,
                    fill_side=side,
                    fv_at_fill=mid_at_fill or price,
                )

            # CL-01: Spread optimizer
            if condition_id:
                spread_optimizer.record_fill(
                    condition_id=condition_id,
                    spread_at_fill=spread_optimizer.get_optimal_base_spread(condition_id),
                    spread_capture=realized_spread_capture or 0.0,
                    adverse_selection_5s=adverse_selection_estimate or 0.0,
                    gamma_at_fill=spread_optimizer.get_optimal_gamma(condition_id),
                )

            # CL-05: Post-mortem classification
            post_mortem.classify_fill(
                pnl=realized_spread_capture or 0.0,
                spread_capture=realized_spread_capture or 0.0,
                adverse_selection_5s=adverse_selection_estimate or 0.0,
            )

            # ST-06: Change-point detection
            _cp_outcome = 1.0 if (realized_spread_capture or 0) > 0 else 0.0
            changepoint_detector.update(_cp_outcome)
        except Exception as _learn_err:
            logger.warning("learning_module_fill_error", error=str(_learn_err))

        # PM-10: Cancel opposing side to prevent stale quote exposure
        if order_manager and not paper_mode:
            _cancel_task = asyncio.create_task(
                order_manager.cancel_opposing_on_fill(
                    token_id, side
                )
            )
            _cancel_task.add_done_callback(
                _task_done_callback
            )

        pmm2_on_fill(
            pmm2_runtime,
            order_id,
            size,
            price,
            token_id=token_id,
            condition_id=condition_id,
        )

    async def _replay_pending_fills(order_id: str | None = None) -> None:
        pending_rows = await fill_recorder.get_pending_fills(order_id)
        for row in pending_rows:
            tracked = state.order_tracker.get(str(row.get("order_id") or ""))
            if tracked is None:
                continue
            raw_event_json = row.get("raw_event_json") or "{}"
            try:
                raw_event = json.loads(raw_event_json) if raw_event_json else {}
            except json.JSONDecodeError:
                raw_event = {}
            await _apply_fill_effects(
                {
                    "order_id": row.get("order_id"),
                    "exchange_trade_id": row.get("exchange_trade_id"),
                    "fill_identity": row.get("fill_identity"),
                    "token_id": row.get("token_id"),
                    "condition_id": row.get("condition_id"),
                    "side": row.get("side"),
                    "price": row.get("price"),
                    "size": row.get("size"),
                    "fee_amount": row.get("fee"),
                    "fee_source": row.get("fee_source") or "unknown",
                    "raw_msg": raw_event,
                },
                tracked,
                existing_fill_id=int(row.get("id") or 0),
                send_fill_notification=False,
            )

    async def _reconcile_unknown_fill(order_id: str) -> None:
        try:
            if not reconciler:
                return
            recovery = await reconciler.full_reconciliation()
            if recovery.success:
                await _replay_pending_fills(order_id)
        finally:
            unknown_fill_reconciliation_inflight.discard(order_id)

    async def on_fill(msg: dict[str, Any]) -> None:
        """Callback when a fill/trade is received from UserWebSocket."""
        try:
            order_id = str(msg.get("order_id", "") or "")
            price = float(msg.get("price", 0.0) or 0.0)
            size = float(msg.get("size", 0.0) or 0.0)
            if not order_id or size <= 0 or price <= 0:
                return

            fill_identity = str(msg.get("fill_identity") or "")
            if not fill_identity:
                raw = msg.get("raw_msg") or msg
                fill_identity = hashlib.sha256(
                    json.dumps(raw, sort_keys=True, default=str)
                    .encode("utf-8")
                ).hexdigest()
            if _fill_dedup.check_and_add(fill_identity):
                return

            tracked = state.order_tracker.get(order_id)
            if tracked is None:
                await _record_pending_unknown_fill(msg)
                if reconciler is not None and order_id not in unknown_fill_reconciliation_inflight:
                    unknown_fill_reconciliation_inflight.add(order_id)
                    reconcile_task = asyncio.create_task(_reconcile_unknown_fill(order_id))
                    reconcile_task.add_done_callback(_task_done_callback)
                return

            condition_id = str(msg.get("condition_id") or tracked.condition_id or "")
            market_md = state.active_markets.get(condition_id) if condition_id else None
            _, fee_known, _ = _resolve_fill_fee(
                msg,
                market_md,
                price=price,
                size=size,
            )
            if not fee_known:
                await _record_pending_fill(
                    msg,
                    ingest_state="pending_fee_truth",
                    tracked=tracked,
                )
                if reconciler is not None and order_id not in unknown_fill_reconciliation_inflight:
                    unknown_fill_reconciliation_inflight.add(order_id)
                    reconcile_task = asyncio.create_task(_reconcile_unknown_fill(order_id))
                    reconcile_task.add_done_callback(_task_done_callback)
                return

            await _replay_pending_fills(order_id)
            await _apply_fill_effects(msg, tracked)
        except Exception as e:
            logger.error("on_fill_callback_error", error=str(e))

    async def on_order_status(msg: dict[str, Any]) -> None:
        """Callback when order status changes from UserWebSocket."""
        try:
            order_id = msg.get("orderID") or msg.get("order_id") or msg.get("id", "")
            status = msg.get("status", "").upper()
            tracked = state.order_tracker.get(order_id) if order_id else None

            logger.info(
                "order_status_change",
                order_id=order_id[:16] if order_id else "?",
                status=status,
            )

            token_id = (
                msg.get("asset_id")
                or msg.get("token_id", "")
                or (tracked.token_id if tracked else "")
            )
            side = msg.get("side", "").upper() or (tracked.side if tracked else "")
            price = float(msg.get("price", 0) or (tracked.price_float if tracked else 0))
            size = float(
                msg.get("original_size")
                or msg.get("size", 0)
                or (tracked.original_size_float if tracked else 0)
            )
            condition_id = (
                msg.get("market")
                or msg.get("condition_id", "")
                or (tracked.condition_id if tracked else "")
            )

            event_type = {
                "LIVE": "order_live",
                "FAILED": "order_rejected",
                "EXPIRED": "order_expired",
                "CANCELED": "order_canceled",
                "CANCELLED": "order_canceled",
            }.get(status)
            if event_type:
                await emit_spine_event(
                    event_type=event_type,
                    strategy=(tracked.strategy if tracked else "mm") or "mm",
                    condition_id=condition_id or None,
                    token_id=token_id or None,
                    order_id=order_id or None,
                    payload_json={
                        "status": status,
                        "side": side,
                        "price": price,
                        "size": size,
                        "source": "user_ws",
                    },
                )

            if tracked is not None:
                await _replay_pending_fills(order_id)

            # PMM-2: forward order lifecycle events
            if status == "LIVE":
                # Estimate book depth at this price (rough)
                book = state.book_manager.get(token_id) if token_id else None
                book_depth = 0.0
                if book:
                    levels = book._bids if side == "BUY" else book._asks
                    for lvl_price, lvl_size in levels.items():
                        if abs(float(lvl_price) - price) < 0.001:
                            book_depth = float(lvl_size)
                            break
                pmm2_on_order_live(
                    pmm2_runtime,
                    order_id,
                    token_id,
                    side,
                    price,
                    size,
                    book_depth,
                    condition_id=condition_id,
                )
            elif status in ("CANCELED", "CANCELLED"):
                pmm2_on_order_canceled(pmm2_runtime, order_id)
        except Exception as e:
            logger.error("on_order_status_callback_error", error=str(e))

    market_ws = MarketWebSocket(
        ws_url=settings.api.ws_market_url,
        book_manager=state.book_manager,
        on_book_update=on_book_update,
        on_book_delta=on_book_delta,
        on_trade=on_trade,
        on_tick_change=on_tick_change,
        on_reconnect=on_market_reconnect,
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
            on_reconnect=on_user_reconnect,
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

    _market_ws_task = market_ws.start(all_token_ids)
    if user_ws:
        _user_ws_task = user_ws.start()
    if heartbeat:
        _heartbeat_task = heartbeat.start()
    if reconciler:
        _reconcile_task = reconciler.start()
    _recorder_task = recorder.start()
    order_fact_materializer_task = None
    if state.order_fact_materializer is not None:
        order_fact_materializer_task = await state.order_fact_materializer.start()
        order_fact_materializer_task.add_done_callback(_task_done_callback)
    fill_fact_materializer_task = None
    if state.fill_fact_materializer is not None:
        fill_fact_materializer_task = await state.fill_fact_materializer.start()
        fill_fact_materializer_task.add_done_callback(_task_done_callback)
    book_snapshot_fact_materializer_task = None
    if state.book_snapshot_fact_materializer is not None:
        book_snapshot_fact_materializer_task = await state.book_snapshot_fact_materializer.start()
        book_snapshot_fact_materializer_task.add_done_callback(_task_done_callback)
    quote_fact_materializer_task = None
    if state.quote_fact_materializer is not None:
        quote_fact_materializer_task = await state.quote_fact_materializer.start()
        quote_fact_materializer_task.add_done_callback(_task_done_callback)
    shadow_cycle_fact_materializer_task = None
    if state.shadow_cycle_fact_materializer is not None:
        shadow_cycle_fact_materializer_task = await state.shadow_cycle_fact_materializer.start()
        shadow_cycle_fact_materializer_task.add_done_callback(_task_done_callback)
    canary_cycle_fact_materializer_task = None
    if state.canary_cycle_fact_materializer is not None:
        canary_cycle_fact_materializer_task = await state.canary_cycle_fact_materializer.start()
        canary_cycle_fact_materializer_task.add_done_callback(_task_done_callback)

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
                ts = datetime.now(UTC).isoformat()
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
                        INSERT OR REPLACE INTO scoring_history
                        (ts, order_id, condition_id, is_scoring)
                        VALUES (?, ?, ?, ?)
                    """
                    await db.execute_many(sql, scoring_records)
            except Exception as e:
                logger.error("scoring_check_loop_error", error=str(e))

    if not paper_mode:
        scoring_check_task = asyncio.create_task(scoring_check_loop())
        scoring_check_task.add_done_callback(_task_done_callback)

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
                for condition_id, market in state.active_markets.items():
                    for token_id in (market.token_id_yes, market.token_id_no):
                        if not token_id:
                            continue
                        book = state.book_manager.get(token_id)
                        if not book:
                            continue
                        best_bid = book.get_best_bid()
                        best_ask = book.get_best_ask()
                        await emit_spine_event(
                            event_type="book_snapshot",
                            strategy="market_data",
                            condition_id=condition_id,
                            token_id=token_id,
                            payload_json={
                                "best_bid": best_bid.price_float if best_bid else None,
                                "best_ask": best_ask.price_float if best_ask else None,
                                "mid": book.get_midpoint(),
                                "spread": book.get_spread(),
                                "spread_cents": book.get_spread_cents(),
                                "depth_best_bid": best_bid.size_float if best_bid else None,
                                "depth_best_ask": best_ask.size_float if best_ask else None,
                                "depth_within_1c": book.get_depth_within(1.0),
                                "depth_within_2c": book.get_depth_within(2.0),
                                "depth_within_5c": book.get_depth_within(5.0),
                                "is_stale": book.is_stale,
                                "tick_size": str(book.tick_size),
                            },
                        )
            except Exception as e:
                logger.error("book_snapshot_loop_error", error=str(e))

    book_snapshot_task = asyncio.create_task(book_snapshot_loop())
    book_snapshot_task.add_done_callback(_task_done_callback)

    def _build_cached_rest_book(token_id: str, rest_book: Any) -> Any:
        """Build an OrderBook from REST snapshot levels using the live book API."""
        return build_order_book_from_snapshot(
            token_id,
            bids=list(getattr(rest_book, "bids", []) or []),
            asks=list(getattr(rest_book, "asks", []) or []),
            tick_size=state.tick_sizes.get(token_id, Decimal("0.01")),
        )

    def _build_price_oracle() -> dict[str, float]:
        """Build price oracle from current book midpoints for MTM NAV."""
        oracle = {}
        for md in state.active_markets.values():
            for tid in [md.token_id_yes, md.token_id_no]:
                if not tid:
                    continue
                book = state.book_manager.get(tid)
                if book and book.age_seconds <= 120:
                    bb = book.get_best_bid()
                    ba = book.get_best_ask()
                    if bb and ba:
                        oracle[tid] = (bb.price_float + ba.price_float) / 2
                    elif bb:
                        oracle[tid] = bb.price_float
                    elif ba:
                        oracle[tid] = ba.price_float
        return oracle

    state.risk_limits.set_price_oracle_provider(_build_price_oracle)

    def _add_suppression_reason(intent: Any, side: str, reason: str) -> None:
        """Attach a side-specific suppression reason to a QuoteIntent."""
        if not reason:
            return
        target = (
            intent.bid_suppression_reasons
            if side == "BUY"
            else intent.ask_suppression_reasons
        )
        if reason not in target:
            target.append(reason)

    async def _clear_token_quotes(
        token_id: str,
        condition_id: str,
        tick_size: Decimal,
        neg_risk: bool,
        bid_reasons: list[str],
        ask_reasons: list[str],
    ) -> dict[str, Any]:
        """Cancel any live quotes for a token by diffing to an empty intent."""
        if paper_mode and paper_engine:
            paper_engine.cancel_all_orders(token_id)
            return {
                "canceled": 0,
                "submitted": 0,
                "rejected": 0,
                "unchanged": 0,
                "replacement_reason_counts": {},
                "errors": [],
            }

        if order_manager is None:
            return {
                "canceled": 0,
                "submitted": 0,
                "rejected": 0,
                "unchanged": 0,
                "replacement_reason_counts": {},
                "errors": ["order_manager_unavailable"],
            }

        if bid_reasons:
            await emit_spine_event(
                event_type="quote_side_suppressed",
                strategy="mm",
                condition_id=condition_id,
                token_id=token_id,
                payload_json={
                    "stage": "clear_quotes",
                    "side": "BUY",
                    "reasons": list(dict.fromkeys(bid_reasons)),
                    "neg_risk": neg_risk,
                },
            )
        if ask_reasons:
            await emit_spine_event(
                event_type="quote_side_suppressed",
                strategy="mm",
                condition_id=condition_id,
                token_id=token_id,
                payload_json={
                    "stage": "clear_quotes",
                    "side": "SELL",
                    "reasons": list(dict.fromkeys(ask_reasons)),
                    "neg_risk": neg_risk,
                },
            )

        return await order_manager.diff_and_apply(
            QuoteIntent(
                token_id=token_id,
                condition_id=condition_id,
                strategy="mm",
                neg_risk=neg_risk,
                bid_suppression_reasons=list(dict.fromkeys(bid_reasons)),
                ask_suppression_reasons=list(dict.fromkeys(ask_reasons)),
            ),
            tick_size,
        )

    def _merge_replacement_reasons(
        target_counts: dict[str, int],
        result: dict[str, Any],
    ) -> None:
        for reason, count in result.get("replacement_reason_counts", {}).items():
            target_counts[reason] += count

    # Wait briefly for WS connections to establish
    warmup_s = 5.0 if paper_mode else 2.0
    logger.info("warmup_wait", seconds=warmup_s, paper_mode=paper_mode)
    await asyncio.sleep(warmup_s)

    # Before seeding drawdown, force an initial reconciliation in live mode.
    # Otherwise startup can mark stale/local positions to market, then a first
    # exchange sync zeroes them out and falsely trips flatten-only drawdown.
    if not paper_mode and reconciler:
        logger.info("startup_reconciliation_before_drawdown")
        await reconciler.full_reconciliation()
        _mark_resume_token_valid("startup")

    # Initialize drawdown with current NAV (mark-to-market)
    if paper_mode and paper_engine:
        nav = paper_engine.get_nav(state.book_manager)
    else:
        nav = state.inventory_manager.get_total_nav_estimate(price_oracle=_build_price_oracle())
    state.nav = nav
    state.drawdown.initialize(nav)
    state.risk_limits.update_nav(nav)

    state.mode = "QUOTING" if paper_mode or state.resume_token_valid else "PAUSED"
    logger.info("pmm1_runtime_mode_initialized", mode=state.mode, markets=len(state.active_markets))
    ops_monitor.write_lifecycle_status(
        mode=state.mode,
        paper_mode=paper_mode,
        note="quoting",
        kill_switch=state.kill_switch.get_status(),
        config_context=config_context,
        runtime_safety=_runtime_safety_context(),
    )

    # ── Main quote loop ──
    _toxicity_mute_until: dict[str, float] = {}
    cycle_count = 0
    quote_interval = settings.bot.quote_cycle_ms / 1000.0
    last_rebate_check = 0.0  # S0-3: track last rebate check time
    last_resolution_check = 0.0  # F01: track last resolution outcome check
    last_nav_snapshot_emit = 0.0
    summary_lifecycle_counts = zero_lifecycle_counts()
    summary_replacement_reason_counts: dict[str, int] = defaultdict(int)
    summary_market_telemetry_counts: dict[str, int] = defaultdict(int)

    try:
        while not shutdown_event.is_set():
            cycle_start = time.time()
            cycle_count += 1

            # ── Rebate check (S0-3) — every hour ──
            if not paper_mode and (time.time() - last_rebate_check) >= 3600:
                try:
                    rebate_date = datetime.now(UTC).strftime("%Y-%m-%d")
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

            # ── F01: Check for resolved markets — every 5 minutes ──
            # When a market resolves, flush pending fills to edge_tracker /
            # fv_calibrator with the ACTUAL outcome (not trade side).
            if resolution_recorder and resolution_recorder.pending_count > 0:
                if (time.time() - last_resolution_check) >= 300:
                    last_resolution_check = time.time()
                    try:
                        _pending_cids = list(resolution_recorder._pending.keys())
                        for _cid in _pending_cids[:10]:  # batch: max 10 per cycle
                            _mkt = await gamma.get_market_by_condition(_cid)
                            if _mkt and _mkt.closed:
                                _prices = _mkt.outcome_prices_list
                                if len(_prices) >= 1 and _prices[0] in (0.0, 1.0):
                                    resolution_recorder.on_market_resolved(
                                        _cid,
                                        outcome=_prices[0],  # 1.0=YES, 0.0=NO
                                    )
                                    # KP-03: Update theme correlation model
                                    state.correlation.record_outcome(
                                        _cid, outcome=_prices[0],
                                    )
                    except Exception as _e:
                        logger.warning("resolution_check_error", error=str(_e))

            # ── Kill switch check (skip in paper mode) ──
            if not paper_mode:
                stale_triggered = state.kill_switch.check_stale_feed(
                    market_ws.seconds_since_last_message
                )
                if stale_triggered:
                    _invalidate_resume_token("stale_market_feed")
                if heartbeat is not None:
                    state.kill_switch.check_heartbeat(
                        heartbeat.is_healthy,
                        heartbeat.consecutive_failures,
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
                    ops_monitor.write_lifecycle_status(
                        mode=state.mode,
                        paper_mode=paper_mode,
                        note="kill_switch_active",
                        kill_switch=state.kill_switch.get_status(),
                        config_context=config_context,
                        runtime_safety=_runtime_safety_context(),
                    )
                    await asyncio.sleep(1.0)
                    continue
                else:
                    # Kill switch cleared — resume quoting
                    if state.mode in {"FLATTEN_ONLY", "PAUSED"} and state.resume_token_valid:
                        logger.info("kill_switch_cleared_resuming_quoting")
                        state.mode = "QUOTING"
                    elif state.mode == "FLATTEN_ONLY":
                        state.mode = "PAUSED"

            # ── Drawdown check ──
            if state.drawdown.should_check_daily_reset():
                if paper_mode and paper_engine:
                    nav = paper_engine.get_nav(state.book_manager)
                else:
                    nav = state.inventory_manager.get_total_nav_estimate(
                        price_oracle=_build_price_oracle()
                    )
                state.nav = nav
                state.drawdown.reset_daily(nav)

            if paper_mode and paper_engine:
                nav = paper_engine.get_nav(state.book_manager)
            else:
                nav = state.inventory_manager.get_total_nav_estimate(
                    price_oracle=_build_price_oracle()
                )
            state.nav = nav
            dd_state = state.drawdown.update(nav)
            state.risk_limits.update_nav(nav)
            now_ts = time.time()
            if now_ts - last_nav_snapshot_emit >= 60.0:
                await emit_spine_event(
                    event_type="nav_snapshot",
                    strategy="ops",
                    payload_json={
                        "nav": nav,
                        "mode": state.mode,
                        "drawdown_pct": dd_state.drawdown_pct,
                        "daily_pnl": dd_state.daily_pnl,
                        "tier": dd_state.tier.value,
                    },
                )
                last_nav_snapshot_emit = now_ts

            if dd_state.should_flatten_only:
                if state.mode != "FLATTEN_ONLY":
                    logger.critical("drawdown_flatten_only", tier=dd_state.tier.value)
                    if order_manager:
                        await order_manager.cancel_all()
                    if paper_engine:
                        paper_engine.cancel_all_orders()
                    state.mode = "FLATTEN_ONLY"
                    state.kill_switch.trigger_drawdown()
                ops_monitor.write_lifecycle_status(
                    mode=state.mode,
                    paper_mode=paper_mode,
                    note="drawdown_flatten_only",
                    kill_switch=state.kill_switch.get_status(),
                    config_context=config_context,
                    runtime_safety=_runtime_safety_context(),
                )
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

            exit_signals = []
            tokens_under_exit: set[str] = set()
            if not paper_mode and order_manager:
                try:
                    exit_signals = await exit_manager.evaluate_all(state.active_markets)
                    tokens_under_exit = {
                        signal.token_id
                        for signal in exit_signals
                        if signal.token_id
                    }
                    tokens_under_exit.update(
                        order.token_id
                        for order in state.order_tracker.get_orders_by_strategy("exit")
                        if order.is_active and order.token_id
                    )
                except Exception as e:
                    logger.error("exit_signal_scan_error", error=str(e), exc_info=True)
                    exit_signals = []
                    tokens_under_exit = set()

                # KP-07: Kelly-rational exit signals
                try:
                    if exit_manager and llm_reasoner:
                        for _cid, _md in list(state.active_markets.items()):
                            _pos = state.position_tracker.get(_cid)
                            if not _pos or _pos.is_flat:
                                continue
                            _est = llm_reasoner.get_estimate(_cid)
                            if not (_est and _est.is_fresh):
                                continue
                            _tid = getattr(_md, "token_id_yes", "")
                            _bk = state.book_manager.get(_tid) if _tid else None
                            _p_market = (_bk.get_midpoint() if _bk else None) or 0.5
                            _signal = exit_manager.get_kelly_exit_signal(
                                _cid, _est.p_calibrated, _p_market,
                            )
                            if _signal:
                                logger.info(
                                    "kelly_exit_signal",
                                    cid=_cid[:16],
                                    signal=_signal,
                                )
                                _urgency = (
                                    "high"
                                    if _signal == "kelly_sl"
                                    else "medium"
                                )
                                exit_signals.append(SellSignal(
                                    token_id=_tid,
                                    condition_id=_cid,
                                    size=abs(
                                        _pos.net_exposure
                                    ),
                                    price=_p_market,
                                    urgency=_urgency,
                                    reason=f"kelly_{_signal}",
                                ))
                except Exception as _kelly_exit_err:
                    logger.debug("kelly_exit_check_error", error=str(_kelly_exit_err))

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
                            cb = _build_cached_rest_book(tid, rest_book)
                            ck = f"rest_book_{tid}"
                            state.rest_book_cache[ck] = cb
                            state.rest_book_cache_ts[ck] = time.time()
                    except Exception as e:
                        logger.warning("rest_book_prefetch_failed", token_id=tid[:16], error=str(e))

                # Fetch up to 5 books in parallel per cycle
                batch = stale_tokens[:5]
                await asyncio.gather(*[_fetch_rest_book(t) for t in batch])

            # ── Per-cycle Paper 2 edge confidence ──
            _edge_confidence = edge_tracker.get_edge_confidence() if edge_tracker else 1.0

            # CL-07: Inventory carry snapshot (~every 60s = 240 cycles at 250ms)
            if cycle_count % 240 == 0:
                try:
                    def _get_mid_for_carry(cid: str) -> float | None:
                        _md = state.active_markets.get(cid)
                        if not _md:
                            return None
                        _tid = getattr(_md, "token_id_yes", "")
                        _bk = state.book_manager.get(_tid)
                        return _bk.get_midpoint() if _bk else None

                    carry_tracker.snapshot(
                        state.position_tracker.get_active_positions(),
                        _get_mid_for_carry,
                    )
                    state.pnl_tracker.set_inventory_carry(carry_tracker.total_carry)
                except Exception as _carry_err:
                    logger.warning("carry_snapshot_error", error=str(_carry_err))

            # ST-06: Check for regime change
            if changepoint_detector and changepoint_detector.should_reset_sprt():
                logger.warning(
                    "regime_change_detected",
                    change_prob=round(changepoint_detector.change_probability(within_k=5), 3),
                    run_length=round(changepoint_detector.expected_run_length(), 1),
                )

            # ── Quote each market ──
            markets_quoted = 0
            orders_submitted = 0
            orders_canceled = 0
            cycle_replacement_reason_counts: dict[str, int] = defaultdict(int)
            cycle_lifecycle_baseline = (
                state.order_tracker.snapshot_lifecycle_counts()
                if not paper_mode
                else None
            )
            concentration_suppressions = compute_concentration_suppressions(
                state.eligible_markets(),
                settings.market_filters,
                classify_theme=state.correlation.classify,
            )
            escalation_ticks = state.fill_escalator.get_escalation_ticks()

            for md in state.eligible_markets():
                question = md.question or md.slug or md.condition_id
                token_id = md.token_id_yes
                tick_size = (
                    state.tick_sizes.get(token_id, Decimal("0.01"))
                    if token_id else Decimal("0.01")
                )

                # Hard stop: cancel quotes when resolution risk says stop
                if not state.resolution_risk.should_quote(md.condition_id):
                    for suppress_token in filter(None, [md.token_id_yes, md.token_id_no]):
                        suppress_tick = state.tick_sizes.get(suppress_token, Decimal("0.01"))
                        suppress_result = await _clear_token_quotes(
                            suppress_token,
                            md.condition_id,
                            suppress_tick,
                            md.neg_risk,
                            ["resolution_quote_halt"],
                            ["resolution_quote_halt"],
                        )
                        _merge_replacement_reasons(cycle_replacement_reason_counts, suppress_result)
                    _emit_market_telemetry(
                        state.market_telemetry,
                        kind="quote_market_suppressed",
                        stage="resolution_gate",
                        reason="resolution_quote_halt",
                        condition_id=md.condition_id,
                        question=question,
                        hours_to_end=(
                            round(res_state.hours_remaining, 2)
                            if (res_state := state.resolution_risk.get(
                                md.condition_id
                            )) is not None
                            else None
                        ),
                    )
                    continue

                # For arb detection, we'll use the YES token book (arb logic already handles both)
                if not token_id:
                    _emit_market_telemetry(
                        state.market_telemetry,
                        kind="quote_market_rejected",
                        stage="quote_loop_precheck",
                        reason="missing_yes_token",
                        condition_id=md.condition_id,
                        question=question,
                    )
                    continue

                book = state.book_manager.get(token_id)
                # Stale threshold: 60s live (WS books are fine within a minute), 120s paper
                stale_threshold = 120.0 if paper_mode else 60.0
                book_usable = book is not None and book.age_seconds <= stale_threshold

                # REST fallback: if no usable WS book, fetch via REST (cached 10s)
                if not book_usable:
                    rest_cache_key = f"rest_book_{token_id}"
                    rest_cache_ts = state.rest_book_cache_ts.get(rest_cache_key, 0)
                    # S-H4: Reject REST book cache entries older than 60 seconds
                    if time.time() - rest_cache_ts > 60.0:
                        book = None  # Force fresh fetch
                    else:
                        book = state.rest_book_cache.get(rest_cache_key)
                    if time.time() - rest_cache_ts > 10.0:  # Refresh every 10s
                        try:
                            rest_book = await clob_public.get_order_book(token_id)
                            if rest_book and rest_book.bids and rest_book.asks:
                                cached_book = _build_cached_rest_book(token_id, rest_book)
                                state.rest_book_cache[rest_cache_key] = cached_book
                                state.rest_book_cache_ts[rest_cache_key] = time.time()
                                book = cached_book
                        except Exception as e:
                            logger.warning(
                                "rest_book_refresh_failed",
                                token_id=token_id[:16],
                                error=str(e),
                            )

                if book is None or (not book._bids and not book._asks):
                    suppress_result = await _clear_token_quotes(
                        token_id,
                        md.condition_id,
                        tick_size,
                        md.neg_risk,
                        ["book_missing"],
                        ["book_missing"],
                    )
                    _merge_replacement_reasons(cycle_replacement_reason_counts, suppress_result)
                    if md.token_id_no:
                        suppress_result_no = await _clear_token_quotes(
                            md.token_id_no,
                            md.condition_id,
                            state.tick_sizes.get(md.token_id_no, Decimal("0.01")),
                            md.neg_risk,
                            ["book_missing"],
                            ["book_missing"],
                        )
                        _merge_replacement_reasons(
                            cycle_replacement_reason_counts,
                            suppress_result_no,
                        )
                    _emit_market_telemetry(
                        state.market_telemetry,
                        kind="quote_market_suppressed",
                        stage="book_precheck",
                        reason="book_missing",
                        condition_id=md.condition_id,
                        token_id=token_id,
                        question=question,
                    )
                    continue

                # ── Compute features ──
                features = state.feature_engine.compute(
                    token_id=token_id,
                    book=book,
                    condition_id=md.condition_id,
                    end_date=md.end_date,
                )
                live_assessment = assess_live_market(
                    md,
                    book,
                    features,
                    settings.market_filters,
                )
                if not live_assessment.tradable:
                    for reason in live_assessment.reasons:
                        _emit_market_telemetry(
                            state.market_telemetry,
                            kind="quote_market_suppressed",
                            stage="live_market_sanity",
                            reason=reason,
                            condition_id=md.condition_id,
                            token_id=token_id,
                            question=question,
                            spread_cents=round(live_assessment.spread_cents, 2),
                            min_side_depth=round(live_assessment.min_side_depth, 2),
                            reward_eligible=md.reward_eligible,
                            reward_capture_ok=live_assessment.reward_capture_ok,
                        )
                    suppress_result = await _clear_token_quotes(
                        token_id,
                        md.condition_id,
                        tick_size,
                        md.neg_risk,
                        live_assessment.reasons,
                        live_assessment.reasons,
                    )
                    _merge_replacement_reasons(cycle_replacement_reason_counts, suppress_result)
                    if md.token_id_no:
                        suppress_result_no = await _clear_token_quotes(
                            md.token_id_no,
                            md.condition_id,
                            state.tick_sizes.get(md.token_id_no, Decimal("0.01")),
                            md.neg_risk,
                            live_assessment.reasons,
                            live_assessment.reasons,
                        )
                        _merge_replacement_reasons(
                            cycle_replacement_reason_counts,
                            suppress_result_no,
                        )
                    continue

                # ── Check arb opportunities first ──
                if settings.strategy.enable_binary_parity:
                    book_no = state.book_manager.get(md.token_id_no)
                    book_no_fresh = (
                        book_no and book_no.age_seconds <= stale_threshold
                        if book_no else False
                    )
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
                        neg_arb_orders = state.neg_risk_detector.scan_event(
                            outcomes, books, md.event_id
                        )
                        if neg_arb_orders and not dd_state.should_pause_taker:
                            if paper_mode and paper_engine and paper_logger:
                                paper_logger.log_arb(
                                    arb_type="neg_risk",
                                    event_id=md.event_id,
                                    details={"num_orders": len(neg_arb_orders)},
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
                                    for o in neg_arb_orders
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
                                    for o in neg_arb_orders
                                ]
                                await order_manager.execute_arb(arb_reqs)
                            continue

                # ── Market making ──
                if settings.strategy.enable_market_making:
                    # Fair value
                    fv_estimate = state.fair_value_model.compute_fair_value(features)
                    base_fair_value = fv_estimate.fair_value

                    # V3 fair value integration (if enabled)
                    if v3_integrator and v3_integrator.enabled:
                        try:
                            blended_fv, v3_meta = await v3_integrator.get_blended_fair_value(
                                md.condition_id, base_fair_value
                            )
                            if v3_meta.get("v3_used"):
                                fv_estimate.fair_value = blended_fv
                                base_fair_value = blended_fv
                        except Exception as v3_err:
                            logger.debug("v3_blend_error", error=str(v3_err))

                    # Embedded Opus reasoning (if enabled)
                    if llm_reasoner and llm_reasoner.config.enabled:
                        llm_fv, llm_meta = (
                            llm_reasoner.get_blended_fair_value(
                                md.condition_id,
                                base_fair_value,
                            )
                        )
                        if llm_meta.get("llm_used"):
                            fv_estimate.fair_value = llm_fv
                            base_fair_value = llm_fv

                    # Skip extreme prices — no counterparty flow below 15c or above 85c
                    if base_fair_value < 0.15 or base_fair_value > 0.85:
                        for reason in ("extreme_fair_value",):
                            _emit_market_telemetry(
                                state.market_telemetry,
                                kind="quote_market_suppressed",
                                stage="fair_value_gate",
                                reason=reason,
                                condition_id=md.condition_id,
                                token_id=token_id,
                                question=question,
                                fair_value=round(base_fair_value, 4),
                            )
                        suppress_result = await _clear_token_quotes(
                            token_id,
                            md.condition_id,
                            tick_size,
                            md.neg_risk,
                            ["extreme_fair_value"],
                            ["extreme_fair_value"],
                        )
                        _merge_replacement_reasons(cycle_replacement_reason_counts, suppress_result)
                        if md.token_id_no:
                            suppress_result_no = await _clear_token_quotes(
                                md.token_id_no,
                                md.condition_id,
                                state.tick_sizes.get(md.token_id_no, Decimal("0.01")),
                                md.neg_risk,
                                ["extreme_fair_value"],
                                ["extreme_fair_value"],
                            )
                            _merge_replacement_reasons(
                            cycle_replacement_reason_counts,
                            suppress_result_no,
                        )
                        continue

                    # Inventory (YES position)
                    pos = state.position_tracker.get(md.condition_id)
                    _yes_inventory = pos.yes_size if pos else 0.0
                    no_inventory = pos.no_size if pos else 0.0
                    market_inv = pos.net_exposure if pos else 0.0
                    cluster_inv = (
                        state.position_tracker.get_event_net_exposure(
                            md.event_id
                        ) if md.event_id else 0.0
                    )

                    # Position age for dynamic γ
                    position_age_hours = 0.0
                    if pos and pos.last_update > 0 and pos.net_exposure != 0:
                        position_age_hours = (time.time() - pos.last_update) / 3600.0

                    # Resolution exit: check if we should block new buys
                    block_new_buys = False
                    bid_block_reasons: list[str] = []
                    res_action = exit_manager.get_resolution_action(
                        md.condition_id, state.active_markets
                    )
                    if res_action and res_action.block_new_buys:
                        block_new_buys = True
                        bid_block_reasons.append("resolution_block_new_buys")
                    concentration_reasons = concentration_suppressions.get(md.condition_id, [])
                    if concentration_reasons:
                        block_new_buys = True
                        bid_block_reasons.extend(concentration_reasons)

                    # Reward EV
                    reward_ev = (
                        state.reward_estimator
                        .compute_reward_ev_for_universe(md.condition_id)
                    )
                    market_inventory_yes, cluster_inventory_yes = inventory_context_for_token(
                        is_no_token=False,
                        market_inventory=market_inv,
                        cluster_inventory=cluster_inv,
                    )

                    # Toxicity pause: suppress quoting when VPIN is dangerously high
                    if features.vpin > settings.pricing.toxicity_pause_vpin:
                        _toxicity_mute_until[md.condition_id] = (
                            time.time() + settings.pricing.toxicity_pause_seconds
                        )
                    if time.time() < _toxicity_mute_until.get(md.condition_id, 0):
                        for _sup_tok in filter(None, [md.token_id_yes, md.token_id_no]):
                            suppress_result = await _clear_token_quotes(
                                _sup_tok,
                                md.condition_id,
                                state.tick_sizes.get(_sup_tok, Decimal("0.01")),
                                md.neg_risk,
                                ["toxicity_pause"],
                                ["toxicity_pause"],
                            )
                            _merge_replacement_reasons(
                                cycle_replacement_reason_counts, suppress_result,
                            )
                        continue

                    # ── CL-01/KP-02/KP-04: Compute analytics-derived pricing params ──
                    _optimal_spread = spread_optimizer.get_optimal_base_spread(
                        md.condition_id,
                    )
                    _optimal_gamma = spread_optimizer.get_optimal_gamma(
                        md.condition_id,
                    )
                    _edge = abs(fv_estimate.fair_value - features.midpoint)
                    _n_obs = len(edge_tracker.trades) if edge_tracker else 0
                    _shrinkage = (
                        _kelly_shrinkage_factor(_edge, sigma_p=0.05, n_obs=_n_obs)
                        if _n_obs > 0 and _edge > 0 else None
                    )
                    _dd_cap = state.drawdown.get_proactive_size_cap()

                    # Quote
                    quote_intent = state.quote_engine.compute_quote(
                        token_id=token_id,
                        features=features,
                        fair_value=fv_estimate.fair_value,
                        haircut=fv_estimate.haircut,
                        confidence=fv_estimate.confidence,
                        market_inventory=market_inventory_yes,
                        cluster_inventory=cluster_inventory_yes,
                        tick_size=float(tick_size),
                        reward_ev=reward_ev,
                        neg_risk=md.neg_risk,
                        condition_id=md.condition_id,
                        position_age_hours=position_age_hours,
                        market_price=features.midpoint,
                        nav=nav,
                        edge_confidence=_edge_confidence,
                        n_active_positions=len(state.active_markets),
                        optimal_base_spread=_optimal_spread,
                        optimal_gamma=_optimal_gamma,
                        shrinkage=_shrinkage,
                        dd_size_cap=_dd_cap,
                    )
                    apply_quote_book_guards(
                        quote_intent,
                        book,
                        tick_size,
                        escalation_ticks=escalation_ticks,
                    )

                    # MM-10: Suppress negative-EV quotes
                    _as_cost = markout_tracker.get_as_cost(md.condition_id)
                    if (
                        _as_cost != 0
                        and quote_intent.bid_price
                        and quote_intent.ask_price
                    ):
                        _q_ev = state.quote_engine.compute_quote_ev(
                            reservation_price=fv_estimate.fair_value,
                            bid_price=quote_intent.bid_price,
                            ask_price=quote_intent.ask_price,
                            as_cost=_as_cost,
                        )
                        if state.quote_engine.should_suppress_quotes(_q_ev):
                            quote_intent.bid_price = None
                            quote_intent.bid_size = None
                            quote_intent.ask_price = None
                            quote_intent.ask_size = None
                            _add_suppression_reason(quote_intent, "BUY", "negative_quote_ev")
                            _add_suppression_reason(quote_intent, "SELL", "negative_quote_ev")

                    # SELL logic: only post asks when we hold inventory
                    if not paper_mode:
                        if market_inv <= 0:
                            # No inventory — don't post sells
                            quote_intent.ask_size = None
                            quote_intent.ask_price = None
                            _add_suppression_reason(quote_intent, "SELL", "no_inventory_to_offer")
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
                                    _add_suppression_reason(
                                        quote_intent,
                                        "SELL",
                                        "inventory_below_min_sell_size",
                                    )

                    # Block new buys during resolution exit window
                    if block_new_buys:
                        quote_intent.bid_size = None
                        quote_intent.bid_price = None
                        for reason in bid_block_reasons:
                            _add_suppression_reason(quote_intent, "BUY", reason)

                    # Apply drawdown adjustments
                    if dd_state.should_widen_quotes:
                        if quote_intent.bid_size:
                            quote_intent.bid_size *= dd_state.size_multiplier
                        if quote_intent.ask_size:
                            quote_intent.ask_size *= dd_state.size_multiplier

                    # Apply risk limits
                    quote_intent, risk_diag = state.risk_limits.apply_to_quote_with_diagnostics(
                        quote_intent, event_id=md.event_id
                    )
                    for reason in risk_diag.bid_reasons:
                        _add_suppression_reason(quote_intent, "BUY", reason)
                    for reason in risk_diag.ask_reasons:
                        _add_suppression_reason(quote_intent, "SELL", reason)

                    # Apply resolution risk size multiplier
                    res_mult = state.resolution_risk.get_size_multiplier(md.condition_id)
                    if res_mult < 1.0:
                        if quote_intent.bid_size:
                            quote_intent.bid_size *= res_mult
                        if quote_intent.ask_size:
                            quote_intent.ask_size *= res_mult

                    # Final min-size enforcement (after all multipliers)
                    min_shares_final = 5.0
                    if (
                        quote_intent.bid_size is not None and
                        quote_intent.bid_size < min_shares_final
                    ):
                        quote_intent.bid_size = None
                        quote_intent.bid_price = None
                        _add_suppression_reason(
                            quote_intent, "BUY",
                            "size_below_min_after_adjustment",
                        )
                    if (
                        quote_intent.ask_size is not None and
                        quote_intent.ask_size < min_shares_final
                    ):
                        quote_intent.ask_size = None
                        quote_intent.ask_price = None
                        _add_suppression_reason(
                            quote_intent, "SELL",
                            "size_below_min_after_adjustment",
                        )

                    # Execute
                    if token_id in tokens_under_exit:
                        quote_intent.bid_price = None
                        quote_intent.bid_size = None
                        quote_intent.ask_price = None
                        quote_intent.ask_size = None
                        _add_suppression_reason(quote_intent, "BUY", "exit_in_progress")
                        _add_suppression_reason(quote_intent, "SELL", "exit_in_progress")

                    if quote_intent.bid_price is None and quote_intent.bid_suppression_reasons:
                        for reason in quote_intent.bid_suppression_reasons:
                            _emit_market_telemetry(
                                state.market_telemetry,
                                kind="quote_side_suppressed",
                                stage="yes_quote",
                                reason=reason,
                                condition_id=md.condition_id,
                                token_id=token_id,
                                side="BUY",
                                question=question,
                                fair_value=round(fv_estimate.fair_value, 4),
                                inventory=round(market_inv, 2),
                            )
                    if quote_intent.ask_price is None and quote_intent.ask_suppression_reasons:
                        for reason in quote_intent.ask_suppression_reasons:
                            _emit_market_telemetry(
                                state.market_telemetry,
                                kind="quote_side_suppressed",
                                stage="yes_quote",
                                reason=reason,
                                condition_id=md.condition_id,
                                token_id=token_id,
                                side="SELL",
                                question=question,
                                fair_value=round(fv_estimate.fair_value, 4),
                                inventory=round(market_inv, 2),
                            )

                    await emit_quote_intent_events(
                        quote_intent,
                        fair_value=fv_estimate.fair_value,
                        inventory=market_inv,
                        question=question,
                        stage="yes_quote",
                    )

                    if paper_mode and paper_engine and paper_logger:
                        if quote_intent.has_bid or quote_intent.has_ask:
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
                            orders_submitted += (
                                (1 if quote_intent.has_bid else 0)
                                + (1 if quote_intent.has_ask else 0)
                            )
                            markets_quoted += 1
                        else:
                            paper_engine.cancel_all_orders(token_id)
                    elif order_manager:
                        quote_result = await order_manager.diff_and_apply(
                            quote_intent, tick_size
                        )
                        _merge_replacement_reasons(cycle_replacement_reason_counts, quote_result)
                        if quote_intent.has_bid or quote_intent.has_ask:
                            markets_quoted += 1

                    if quote_intent.has_bid or quote_intent.has_ask:
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
                        book_no_usable = (
                            book_no is not None and
                            book_no.age_seconds <= stale_threshold
                        )

                        # REST fallback for NO token book
                        if not book_no_usable:
                            rest_cache_key_no = f"rest_book_{token_id_no}"
                            rest_cache_ts_no = state.rest_book_cache_ts.get(rest_cache_key_no, 0)
                            if time.time() - rest_cache_ts_no > 10.0:
                                try:
                                    rest_book_no = await clob_public.get_order_book(token_id_no)
                                    if rest_book_no and rest_book_no.bids and rest_book_no.asks:
                                        cached_book_no = _build_cached_rest_book(
                                            token_id_no, rest_book_no,
                                        )
                                        state.rest_book_cache[rest_cache_key_no] = cached_book_no
                                        state.rest_book_cache_ts[rest_cache_key_no] = time.time()
                                except Exception as e:
                                    logger.warning(
                                        "rest_book_refresh_failed",
                                        token_id=token_id_no[:16],
                                        error=str(e),
                                    )
                            book_no = state.rest_book_cache.get(rest_cache_key_no)

                        tick_size_no = state.tick_sizes.get(token_id_no, Decimal("0.01"))

                        if book_no is None or (not book_no._bids and not book_no._asks):
                            suppress_result_no = await _clear_token_quotes(
                                token_id_no,
                                md.condition_id,
                                tick_size_no,
                                md.neg_risk,
                                ["book_missing"],
                                ["book_missing"],
                            )
                            _merge_replacement_reasons(
                            cycle_replacement_reason_counts,
                            suppress_result_no,
                        )
                            _emit_market_telemetry(
                                state.market_telemetry,
                                kind="quote_market_suppressed",
                                stage="no_book_precheck",
                                reason="book_missing",
                                condition_id=md.condition_id,
                                token_id=token_id_no,
                                question=question,
                            )
                            continue

                        features_no = state.feature_engine.compute(
                            token_id=token_id_no,
                            book=book_no,
                            condition_id=md.condition_id,
                            end_date=md.end_date,
                        )
                        live_assessment_no = assess_live_market(
                            md,
                            book_no,
                            features_no,
                            settings.market_filters,
                        )
                        if not live_assessment_no.tradable:
                            for reason in live_assessment_no.reasons:
                                _emit_market_telemetry(
                                    state.market_telemetry,
                                    kind="quote_market_suppressed",
                                    stage="no_live_market_sanity",
                                    reason=reason,
                                    condition_id=md.condition_id,
                                    token_id=token_id_no,
                                    question=question,
                                    spread_cents=round(live_assessment_no.spread_cents, 2),
                                    min_side_depth=round(live_assessment_no.min_side_depth, 2),
                                    reward_eligible=md.reward_eligible,
                                    reward_capture_ok=live_assessment_no.reward_capture_ok,
                                )
                            suppress_result_no = await _clear_token_quotes(
                                token_id_no,
                                md.condition_id,
                                tick_size_no,
                                md.neg_risk,
                                live_assessment_no.reasons,
                                live_assessment_no.reasons,
                            )
                            _merge_replacement_reasons(
                            cycle_replacement_reason_counts,
                            suppress_result_no,
                        )
                            continue

                        no_fair_value = 1.0 - base_fair_value
                        if no_fair_value < 0.15 or no_fair_value > 0.85:
                            _emit_market_telemetry(
                                state.market_telemetry,
                                kind="quote_market_suppressed",
                                stage="no_fair_value_gate",
                                reason="extreme_fair_value",
                                condition_id=md.condition_id,
                                token_id=token_id_no,
                                question=question,
                                fair_value=round(no_fair_value, 4),
                            )
                            suppress_result_no = await _clear_token_quotes(
                                token_id_no,
                                md.condition_id,
                                tick_size_no,
                                md.neg_risk,
                                ["extreme_fair_value"],
                                ["extreme_fair_value"],
                            )
                            _merge_replacement_reasons(
                            cycle_replacement_reason_counts,
                            suppress_result_no,
                        )
                            continue

                        no_token_inventory = no_inventory
                        position_age_hours_no = 0.0
                        if pos and pos.last_update > 0 and pos.net_exposure != 0:
                            position_age_hours_no = (time.time() - pos.last_update) / 3600.0

                        block_new_buys_no = False
                        bid_block_reasons_no: list[str] = []
                        if res_action and res_action.block_new_buys:
                            block_new_buys_no = True
                            bid_block_reasons_no.append("resolution_block_new_buys")
                        if concentration_reasons:
                            block_new_buys_no = True
                            bid_block_reasons_no.extend(concentration_reasons)

                        reward_ev_no = (
                            state.reward_estimator
                            .compute_reward_ev_for_universe(
                                md.condition_id
                            )
                        )
                        market_inventory_no, cluster_inventory_no = inventory_context_for_token(
                            is_no_token=True,
                            market_inventory=market_inv,
                            cluster_inventory=cluster_inv,
                        )

                        # Reuse analytics params computed for YES token
                        quote_intent_no = state.quote_engine.compute_quote(
                            token_id=token_id_no,
                            features=features_no,
                            fair_value=no_fair_value,
                            haircut=fv_estimate.haircut,
                            confidence=fv_estimate.confidence,
                            market_inventory=market_inventory_no,
                            cluster_inventory=cluster_inventory_no,
                            tick_size=float(tick_size_no),
                            reward_ev=reward_ev_no,
                            neg_risk=md.neg_risk,
                            condition_id=md.condition_id,
                            position_age_hours=position_age_hours_no,
                            market_price=(
                                features_no.midpoint
                                if hasattr(features_no, 'midpoint')
                                else 0.0
                            ),
                            nav=nav,
                            edge_confidence=_edge_confidence,
                            n_active_positions=len(state.active_markets),
                            optimal_base_spread=_optimal_spread,
                            optimal_gamma=_optimal_gamma,
                            shrinkage=_shrinkage,
                            dd_size_cap=_dd_cap,
                        )
                        apply_quote_book_guards(
                            quote_intent_no,
                            book_no,
                            tick_size_no,
                            escalation_ticks=escalation_ticks,
                        )

                        # MM-10: Suppress negative-EV quotes (NO token)
                        _as_cost_no = markout_tracker.get_as_cost(md.condition_id)
                        if (
                            _as_cost_no != 0
                            and quote_intent_no.bid_price
                            and quote_intent_no.ask_price
                        ):
                            _q_ev_no = state.quote_engine.compute_quote_ev(
                                reservation_price=no_fair_value,
                                bid_price=quote_intent_no.bid_price,
                                ask_price=quote_intent_no.ask_price,
                                as_cost=_as_cost_no,
                            )
                            if state.quote_engine.should_suppress_quotes(_q_ev_no):
                                quote_intent_no.bid_price = None
                                quote_intent_no.bid_size = None
                                quote_intent_no.ask_price = None
                                quote_intent_no.ask_size = None
                                _add_suppression_reason(
                                    quote_intent_no, "BUY", "negative_quote_ev",
                                )
                                _add_suppression_reason(
                                    quote_intent_no, "SELL", "negative_quote_ev",
                                )

                        if not paper_mode:
                            if no_token_inventory <= 0:
                                quote_intent_no.ask_size = None
                                quote_intent_no.ask_price = None
                                _add_suppression_reason(
                                    quote_intent_no, "SELL",
                                    "no_inventory_to_offer",
                                )
                            else:
                                if (
                                    quote_intent_no.ask_size and
                                    quote_intent_no.ask_size > no_token_inventory
                                ):
                                    quote_intent_no.ask_size = no_token_inventory
                                if quote_intent_no.ask_size and quote_intent_no.ask_size < 5.0:
                                    if no_token_inventory >= 5.0:
                                        quote_intent_no.ask_size = 5.0
                                    else:
                                        quote_intent_no.ask_size = None
                                        quote_intent_no.ask_price = None
                                        _add_suppression_reason(
                                            quote_intent_no,
                                            "SELL",
                                            "inventory_below_min_sell_size",
                                        )

                        if block_new_buys_no:
                            quote_intent_no.bid_size = None
                            quote_intent_no.bid_price = None
                            for reason in bid_block_reasons_no:
                                _add_suppression_reason(quote_intent_no, "BUY", reason)

                        if dd_state.should_widen_quotes:
                            if quote_intent_no.bid_size:
                                quote_intent_no.bid_size *= dd_state.size_multiplier
                            if quote_intent_no.ask_size:
                                quote_intent_no.ask_size *= dd_state.size_multiplier

                        quote_intent_no, risk_diag_no = (
                            state.risk_limits
                            .apply_to_quote_with_diagnostics(
                                quote_intent_no,
                                event_id=md.event_id,
                            )
                        )
                        for reason in risk_diag_no.bid_reasons:
                            _add_suppression_reason(quote_intent_no, "BUY", reason)
                        for reason in risk_diag_no.ask_reasons:
                            _add_suppression_reason(quote_intent_no, "SELL", reason)

                        res_mult_no = state.resolution_risk.get_size_multiplier(md.condition_id)
                        if res_mult_no < 1.0:
                            if quote_intent_no.bid_size:
                                quote_intent_no.bid_size *= res_mult_no
                            if quote_intent_no.ask_size:
                                quote_intent_no.ask_size *= res_mult_no

                        if (
                            quote_intent_no.bid_size is not None and
                            quote_intent_no.bid_size < 5.0
                        ):
                            quote_intent_no.bid_size = None
                            quote_intent_no.bid_price = None
                            _add_suppression_reason(
                                quote_intent_no, "BUY",
                                "size_below_min_after_adjustment",
                            )
                        if (
                            quote_intent_no.ask_size is not None and
                            quote_intent_no.ask_size < 5.0
                        ):
                            quote_intent_no.ask_size = None
                            quote_intent_no.ask_price = None
                            _add_suppression_reason(
                                quote_intent_no, "SELL",
                                "size_below_min_after_adjustment",
                            )

                        if token_id_no in tokens_under_exit:
                            quote_intent_no.bid_price = None
                            quote_intent_no.bid_size = None
                            quote_intent_no.ask_price = None
                            quote_intent_no.ask_size = None
                            _add_suppression_reason(quote_intent_no, "BUY", "exit_in_progress")
                            _add_suppression_reason(quote_intent_no, "SELL", "exit_in_progress")

                        if (
                            quote_intent_no.bid_price is None and
                            quote_intent_no.bid_suppression_reasons
                        ):
                            for reason in quote_intent_no.bid_suppression_reasons:
                                _emit_market_telemetry(
                                    state.market_telemetry,
                                    kind="quote_side_suppressed",
                                    stage="no_quote",
                                    reason=reason,
                                    condition_id=md.condition_id,
                                    token_id=token_id_no,
                                    side="BUY",
                                    question=question,
                                    fair_value=round(no_fair_value, 4),
                                    inventory=round(-market_inv, 2),
                                )
                        if (
                            quote_intent_no.ask_price is None and
                            quote_intent_no.ask_suppression_reasons
                        ):
                            for reason in quote_intent_no.ask_suppression_reasons:
                                _emit_market_telemetry(
                                    state.market_telemetry,
                                    kind="quote_side_suppressed",
                                    stage="no_quote",
                                    reason=reason,
                                    condition_id=md.condition_id,
                                    token_id=token_id_no,
                                    side="SELL",
                                    question=question,
                                    fair_value=round(no_fair_value, 4),
                                    inventory=round(-market_inv, 2),
                                )

                        await emit_quote_intent_events(
                            quote_intent_no,
                            fair_value=no_fair_value,
                            inventory=-market_inv,
                            question=question,
                            stage="no_quote",
                        )

                        if paper_mode and paper_engine and paper_logger:
                            if quote_intent_no.has_bid or quote_intent_no.has_ask:
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
                                orders_submitted += (
                                    (1 if quote_intent_no.has_bid else 0)
                                    + (1 if quote_intent_no.has_ask else 0)
                                )
                            else:
                                paper_engine.cancel_all_orders(token_id_no)
                        elif order_manager:
                            quote_result_no = await order_manager.diff_and_apply(
                                quote_intent_no, tick_size_no
                            )
                            _merge_replacement_reasons(
                                cycle_replacement_reason_counts,
                                quote_result_no,
                            )

                        if quote_intent_no.has_bid or quote_intent_no.has_ask:
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

                            if (
                                quote_intent_no.bid_price and
                                quote_intent_no.ask_price
                            ):
                                spread_cents_no = (
                                    (quote_intent_no.ask_price
                                     - quote_intent_no.bid_price) * 100
                                )
                                state.metrics.record_quote(
                                    token_id_no,
                                    md.condition_id,
                                    spread_cents_no,
                                )

            # ── Exit Manager: evaluate all sell/exit signals ──
            if not paper_mode and order_manager:
                try:
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
                            for reason, count in result.get(
                                "replacement_reason_counts", {}
                            ).items():
                                cycle_replacement_reason_counts[reason] += count
                            if result.get("submitted"):
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
                                _t = asyncio.create_task(send_telegram(notification))
                                _t.add_done_callback(_task_done_callback)
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
                taker_blocked = False
                # Risk checks before taker bootstrap
                if state.kill_switch.is_triggered:
                    logger.info("taker_bootstrap_blocked_kill_switch")
                    taker_blocked = True
                elif dd_state.should_pause_taker:
                    logger.info("taker_bootstrap_blocked_drawdown")
                    taker_blocked = True

                if not taker_blocked:
                    try:
                        # Pick highest-scored eligible market
                        eligible = state.eligible_markets()
                        if eligible:
                            # Sort by volume (or use universe scoring if available)
                            best_market = max(
                                eligible,
                                key=lambda m: (
                                    m.volume_24h_usd
                                    if hasattr(m, 'volume_24h_usd')
                                    else 0.0
                                )
                            )

                            token_id_taker = best_market.token_id_yes
                            book_taker = state.book_manager.get(token_id_taker)

                            if book_taker:
                                ba_taker = book_taker.get_best_ask()
                                if ba_taker and ba_taker.price_float > 0:
                                    # Submit small FAK BUY at best ask
                                    taker_size = max(
                                        5.0,
                                        state.fill_escalator.config.taker_min_shares,
                                    )
                                    taker_price = ba_taker.price_float

                                    # Ensure dollar value meets minimum
                                    min_dollar = 1.5
                                    if taker_price * taker_size < min_dollar:
                                        taker_size = max(taker_size, min_dollar / taker_price)

                                    # Per-market risk check
                                    market_cost = taker_price * taker_size
                                    market_exposure = 0.0
                                    pos = state.position_tracker.get(best_market.condition_id)
                                    if pos:
                                        market_exposure = (
                                            pos.gross_exposure
                                            if hasattr(pos, 'gross_exposure')
                                            else (
                                                pos.yes_size * pos.yes_avg_price
                                                + pos.no_size * pos.no_avg_price
                                            )
                                        )
                                    taker_nav = (
                                        state.inventory_manager
                                        .get_total_nav_estimate(
                                            price_oracle=_build_price_oracle()
                                        )
                                    )
                                    if (
                                        taker_nav > 0 and
                                        (market_exposure + market_cost)
                                        / taker_nav
                                        > settings.risk.per_market_gross_nav
                                    ):
                                        logger.info(
                                            "taker_bootstrap_blocked_risk_limit",
                                            market_exposure=f"{market_exposure:.2f}",
                                            market_cost=f"{market_cost:.2f}",
                                            nav=f"{taker_nav:.2f}",
                                        )
                                        taker_blocked = True

                                    if not taker_blocked:
                                        tick_taker = state.tick_sizes.get(
                                            token_id_taker, Decimal("0.01"),
                                        )

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

                                        try:
                                            submit_result = await order_manager.submit_order(
                                                taker_req,
                                                condition_id=best_market.condition_id,
                                                strategy="taker_bootstrap",
                                            )
                                            if submit_result.get("success"):
                                                logger.info(
                                                    "taker_bootstrap_submitted",
                                                    order_id=(
                                                        submit_result["order_id"][:16]
                                                        if submit_result["order_id"]
                                                        else "?"
                                                    ),
                                                    token_id=token_id_taker[:16],
                                                )
                                            else:
                                                logger.warning(
                                                    "taker_bootstrap_failed",
                                                    error=(
                                                        submit_result.get("error")
                                                        or "no response"
                                                    ),
                                                )
                                        except Exception as taker_err:
                                            logger.error(
                                                "taker_bootstrap_error",
                                                error=str(taker_err),
                                            )
                    except Exception as e:
                        logger.error("taker_bootstrap_outer_error", error=str(e), exc_info=True)

            if paper_mode:
                cycle_lifecycle_counts = zero_lifecycle_counts()
                cycle_lifecycle_counts["submitted"] = orders_submitted
                cycle_lifecycle_counts["canceled"] = orders_canceled
            else:
                cycle_lifecycle_counts = state.order_tracker.diff_lifecycle_counts(
                    cycle_lifecycle_baseline
                )
                orders_submitted = cycle_lifecycle_counts["submitted"]
                orders_canceled = cycle_lifecycle_counts["canceled"]

            for key, value in cycle_lifecycle_counts.items():
                summary_lifecycle_counts[key] += value
            for key, value in cycle_replacement_reason_counts.items():
                summary_replacement_reason_counts[key] += value
            cycle_market_telemetry_counts = state.market_telemetry.consume_reason_counts()
            for key, value in cycle_market_telemetry_counts.items():
                summary_market_telemetry_counts[key] += value

            # ── Cycle metrics ──
            cycle_duration = (time.time() - cycle_start) * 1000
            state.metrics.record_quote_cycle(
                cycle_duration, markets_quoted, orders_submitted, orders_canceled
            )
            await ops_monitor.observe_cycle(
                state=state,
                paper_mode=paper_mode,
                nav=nav,
                dd_state=dd_state,
                market_ws=market_ws,
                user_ws=user_ws,
                heartbeat=heartbeat,
                reconciler=reconciler,
                markets_quoted=markets_quoted,
                cycle_lifecycle_counts=cycle_lifecycle_counts,
                cycle_duration_ms=cycle_duration,
                pmm2_status=pmm2_runtime.get_status() if pmm2_runtime else None,
                config_context=config_context,
                runtime_safety=_runtime_safety_context(),
                edge_tracker_summary=edge_tracker.get_summary() if edge_tracker else None,
                kelly_state={
                    "enabled": settings.pricing.kelly_enabled,
                    "fraction": settings.pricing.kelly_fraction,
                    "edge_confidence": _edge_confidence,
                } if settings.pricing.kelly_enabled else None,
                calibration_state=(
                    fv_calibrator.get_calibration_metrics()
                    if fv_calibrator else None
                ),
                pnl_attribution={
                    "daily_pnl": dd_state.daily_pnl,
                    "session_peak_nav": dd_state.session_peak_nav,
                    "llm_influenced_fills": _llm_fill_count,
                },
                llm_reasoner_status=llm_reasoner.get_status() if llm_reasoner else None,
            )

            if cycle_count % 100 == 0:
                # S-C2: Periodic cleanup of terminal orders to prevent memory leak
                if hasattr(state, 'order_tracker'):
                    state.order_tracker.cleanup_terminal()

                # ── Periodic analytics saves ──
                for _sname, _sfn in [
                    ("edge_tracker", lambda: edge_tracker.save(
                        settings.analytics.edge_tracker_persist_path
                    )),
                    ("spread_optimizer", lambda: (
                        spread_optimizer.save(
                            "data/spread_optimizer_state.json"
                        )
                    )),
                    ("market_profitability", lambda: (
                        market_profitability.save(
                            "data/market_profitability.json"
                        )
                    )),
                    ("signal_value", lambda: (
                        signal_value_tracker.save(
                            "data/signal_value.json"
                        )
                    )),
                    ("post_mortem", lambda: (
                        post_mortem.save(
                            "data/post_mortem.json"
                        )
                    )),
                ]:
                    try:
                        _sfn()
                    except Exception as _save_err:
                        logger.warning(
                            "periodic_analytics_save_error",
                            module=_sname,
                            error=str(_save_err),
                        )

                # CL-06: Update LLM cost tracking
                if llm_reasoner:
                    signal_value_tracker.set_daily_cost(llm_reasoner._daily_cost_usd)

                # KP-08: VaR reporting
                try:
                    _var_positions = []
                    for _pos in state.position_tracker.get_active_positions():
                        _md = state.active_markets.get(_pos.condition_id)
                        _tid = getattr(_md, "token_id_yes", "") if _md else ""
                        _bk = state.book_manager.get(_tid) if _tid else None
                        _mid = _bk.get_midpoint() if _bk else 0.5
                        _var_positions.append({"size": _pos.net_exposure, "price": _mid})
                    _var_report = var_reporter.compute_report(_var_positions)
                    logger.info("portfolio_var", **_var_report)
                except Exception as _var_err:
                    logger.warning("var_report_error", error=str(_var_err))

                # S-H4: Evict stale REST book cache entries
                stale_keys = [
                    k for k, ts in state.rest_book_cache_ts.items()
                    if time.time() - ts > 120.0
                ]
                for k in stale_keys:
                    state.rest_book_cache.pop(k, None)
                    state.rest_book_cache_ts.pop(k, None)

                if paper_mode and paper_engine and paper_logger:
                    # Paper mode summary with PnL snapshot
                    snap = paper_engine.snapshot(state.book_manager)
                    paper_logger.log_pnl(snap.to_dict())
                    _pos_summary = paper_engine.get_position_summary()
                    log_stats = paper_logger.get_stats()

                    logger.info(
                        "paper_cycle_summary",
                        cycle=cycle_count,
                        markets_quoted=markets_quoted,
                        submitted=summary_lifecycle_counts["submitted"],
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
                        submitted=summary_lifecycle_counts["submitted"],
                        canceled=summary_lifecycle_counts["canceled"],
                        filled=summary_lifecycle_counts["filled"],
                        failed=summary_lifecycle_counts["failed"],
                        cycle_ms=f"{cycle_duration:.1f}",
                        summary_window_cycles=100,
                        mode=state.mode,
                        nav=f"{nav:.2f}",
                        dd_tier=dd_state.tier.value,
                        resolution_exits=resolution_markets,
                        replacement_reasons=dict(summary_replacement_reason_counts),
                        market_suppressions=dict(summary_market_telemetry_counts),
                    )
                summary_lifecycle_counts = zero_lifecycle_counts()
                summary_replacement_reason_counts = defaultdict(int)
                summary_market_telemetry_counts = defaultdict(int)

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
        ops_monitor.write_lifecycle_status(
            mode=state.mode,
            paper_mode=paper_mode,
            note="shutdown",
            kill_switch=state.kill_switch.get_status(),
            config_context=config_context,
            runtime_safety=_runtime_safety_context(),
        )

        # Cancel all orders
        if order_manager:
            try:
                await order_manager.cancel_all()
            except Exception:
                pass

        # ── Persist all analytics state on shutdown ──
        for _sname, _sfn in [
            ("edge_tracker", lambda: edge_tracker.save(
                settings.analytics.edge_tracker_persist_path
            )),
            ("spread_optimizer", lambda: (
                spread_optimizer.save(
                    "data/spread_optimizer_state.json"
                )
            )),
            ("market_profitability", lambda: (
                market_profitability.save(
                    "data/market_profitability.json"
                )
            )),
            ("signal_value", lambda: (
                signal_value_tracker.save(
                    "data/signal_value.json"
                )
            )),
            ("post_mortem", lambda: (
                post_mortem.save(
                    "data/post_mortem.json"
                )
            )),
        ]:
            try:
                _sfn()
            except Exception as _save_err:
                logger.error(
                    "analytics_shutdown_save_failed",
                    module=_sname,
                    error=str(_save_err),
                )
        logger.info("analytics_state_saved_on_shutdown")

        # Paper mode final summary
        if paper_mode and paper_engine and paper_logger:
            snap = paper_engine.snapshot(state.book_manager)
            paper_logger.log_pnl(snap.to_dict())
            _pos_summary = paper_engine.get_position_summary()
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

        # Stop LLM reasoner background task + close session
        if llm_reasoner:
            try:
                await llm_reasoner.stop()
            except Exception:
                pass

        # Close v3 integrator Redis connection
        if v3_integrator:
            try:
                await v3_integrator.close()
            except Exception:
                pass

        # Close news fetcher HTTP session
        if llm_news_fetcher:
            try:
                await llm_news_fetcher.close()
            except Exception:
                pass

        # Stop all tasks
        if heartbeat:
            await heartbeat.stop()
        await market_ws.stop()
        if user_ws:
            await user_ws.stop()
        if reconciler:
            await reconciler.stop()
        await recorder.stop()
        if state.order_fact_materializer is not None:
            await state.order_fact_materializer.stop()
        if state.fill_fact_materializer is not None:
            await state.fill_fact_materializer.stop()
        if state.book_snapshot_fact_materializer is not None:
            await state.book_snapshot_fact_materializer.stop()
        if state.quote_fact_materializer is not None:
            await state.quote_fact_materializer.stop()
        if state.shadow_cycle_fact_materializer is not None:
            await state.shadow_cycle_fact_materializer.stop()
        if state.canary_cycle_fact_materializer is not None:
            await state.canary_cycle_fact_materializer.stop()

        # Close clients
        await gamma.close()
        await clob_public.close()
        await clob_private.close()
        await data_api.close()
        await db.close()
        if state.spine_store is not None:
            await state.spine_store.close()
        if state.spine_stream_store is not None:
            await state.spine_stream_store.close()

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

"""Tests for ExitManager — stop-loss, resolution ramp, take-profit, orphan, flatten."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from pmm1.settings import (
    ExitConfig,
    FlattenConfig,
    OrphanConfig,
    ResolutionExitConfig,
    StopLossConfig,
    TakeProfitConfig,
)
from pmm1.state.books import BookManager
from pmm1.state.positions import MarketPosition, PositionTracker
from pmm1.strategy.exit_manager import ExitManager, SellSignal
from pmm1.strategy.universe import MarketMetadata

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_exit_config(**overrides) -> ExitConfig:
    """Build an ExitConfig with sensible test defaults, accepting sub-config overrides."""
    kwargs: dict = {}
    if "stop_loss" in overrides:
        kwargs["stop_loss"] = StopLossConfig(**overrides.pop("stop_loss"))
    if "take_profit" in overrides:
        kwargs["take_profit"] = TakeProfitConfig(**overrides.pop("take_profit"))
    if "resolution" in overrides:
        kwargs["resolution"] = ResolutionExitConfig(**overrides.pop("resolution"))
    if "flatten" in overrides:
        kwargs["flatten"] = FlattenConfig(**overrides.pop("flatten"))
    if "orphan" in overrides:
        kwargs["orphan"] = OrphanConfig(**overrides.pop("orphan"))
    return ExitConfig(**kwargs)


def _make_position(
    condition_id: str = "cond-1",
    token_yes: str = "tok-yes-1",
    token_no: str = "tok-no-1",
    yes_size: float = 100.0,
    no_size: float = 0.0,
    yes_avg_price: float = 0.50,
    no_avg_price: float = 0.0,
    last_update: float | None = None,
) -> MarketPosition:
    return MarketPosition(
        condition_id=condition_id,
        token_id_yes=token_yes,
        token_id_no=token_no,
        yes_size=yes_size,
        no_size=no_size,
        yes_avg_price=yes_avg_price,
        no_avg_price=no_avg_price,
        last_update=last_update if last_update is not None else time.time() - 7200,
    )


def _make_tracker(*positions: MarketPosition) -> PositionTracker:
    tracker = PositionTracker()
    for pos in positions:
        tracker.register_market(
            condition_id=pos.condition_id,
            token_id_yes=pos.token_id_yes,
            token_id_no=pos.token_id_no,
        )
        p = tracker.get(pos.condition_id)
        assert p is not None
        p.yes_size = pos.yes_size
        p.no_size = pos.no_size
        p.yes_avg_price = pos.yes_avg_price
        p.no_avg_price = pos.no_avg_price
        p.last_update = pos.last_update
    return tracker


def _make_book_manager(prices: dict[str, float]) -> BookManager:
    """Build a BookManager with pre-loaded best bids for given token_id -> price."""
    bm = BookManager()
    for token_id, price in prices.items():
        book = bm.get_or_create(token_id)
        ask_price = min(price + 0.01, 0.99)
        book.apply_snapshot(
            bids=[{"price": str(price), "size": "100"}],
            asks=[{"price": str(ask_price), "size": "100"}],
        )
    return bm


def _make_market_metadata(
    condition_id: str = "cond-1",
    end_date: datetime | None = None,
) -> MarketMetadata:
    return MarketMetadata(
        condition_id=condition_id,
        end_date=end_date,
    )


def _build_exit_manager(
    config: ExitConfig | None = None,
    positions: list[MarketPosition] | None = None,
    prices: dict[str, float] | None = None,
    kill_switch: object | None = None,
) -> ExitManager:
    cfg = config or _make_exit_config()
    pos_list = positions or []
    tracker = _make_tracker(*pos_list)
    bm = _make_book_manager(prices or {})
    return ExitManager(
        config=cfg,
        position_tracker=tracker,
        book_manager=bm,
        kill_switch=kill_switch,
    )


# ── Stop-loss tests ─────────────────────────────────────────────────────────


class TestStopLoss:
    """Tests for stop-loss exit layer."""

    def test_loss_exceeds_threshold_generates_signal(self):
        """Position with unrealized loss exceeding threshold -> SellSignal(reason='stop_loss')."""
        # avg_price=0.50, current=0.38 -> unrealized_pct = (0.38-0.50)/0.50 = -24%
        # default threshold_pct = 0.20, so -24% <= -20% triggers soft stop
        # Raise hard_stop_pct and max_loss_per_trade_usd so only soft stop triggers
        pos = _make_position(yes_avg_price=0.50, yes_size=100.0)
        em = _build_exit_manager(
            config=_make_exit_config(stop_loss={
                "enabled": True,
                "threshold_pct": 0.20,
                "hard_stop_pct": 0.90,
                "max_loss_per_trade_usd": 999.0,
            }),
            positions=[pos],
            prices={"tok-yes-1": 0.38},
        )
        sig = em._check_stop_loss(pos, "tok-yes-1", 100.0, 0.50, 0.38)
        assert sig is not None
        assert sig.reason == "stop_loss"
        assert sig.urgency == "high"
        assert sig.size == 100.0

    def test_loss_below_threshold_no_signal(self):
        """Position with small unrealized loss below threshold -> no signal."""
        # avg=0.50, current=0.45 -> unrealized_pct = -10%, threshold is 20%
        pos = _make_position(yes_avg_price=0.50)
        em = _build_exit_manager(
            config=_make_exit_config(stop_loss={"enabled": True, "threshold_pct": 0.20}),
            positions=[pos],
            prices={"tok-yes-1": 0.45},
        )
        sig = em._check_stop_loss(pos, "tok-yes-1", 100.0, 0.50, 0.45)
        assert sig is None

    def test_hard_stop_triggered(self):
        """Loss exceeding hard_stop_pct -> critical urgency with reason='hard_stop'."""
        # avg=0.50, current=0.25 -> unrealized_pct = -50%, hard_stop_pct default 40%
        pos = _make_position(yes_avg_price=0.50)
        em = _build_exit_manager(
            config=_make_exit_config(
                stop_loss={"enabled": True, "threshold_pct": 0.20, "hard_stop_pct": 0.40}
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.25},
        )
        sig = em._check_stop_loss(pos, "tok-yes-1", 100.0, 0.50, 0.25)
        assert sig is not None
        assert sig.reason == "hard_stop"
        assert sig.urgency == "critical"

    def test_max_loss_usd_triggers_hard_stop(self):
        """Dollar-based hard stop triggers when unrealized USD loss exceeds limit."""
        # avg=0.50, current=0.44 -> loss_usd = 100 * (0.44-0.50) = -6.0
        # max_loss_per_trade_usd = 5.0 -> triggers
        pos = _make_position(yes_avg_price=0.50, yes_size=100.0)
        em = _build_exit_manager(
            config=_make_exit_config(
                stop_loss={
                    "enabled": True,
                    "threshold_pct": 0.20,
                    "hard_stop_pct": 0.90,  # high so pct doesn't trigger
                    "max_loss_per_trade_usd": 5.0,
                }
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.44},
        )
        sig = em._check_stop_loss(pos, "tok-yes-1", 100.0, 0.50, 0.44)
        assert sig is not None
        assert sig.reason == "hard_stop"
        assert sig.urgency == "critical"

    @pytest.mark.asyncio
    async def test_stop_loss_respects_priority_continue(self):
        """When stop_loss fires, no further layers should produce signals for that side."""
        # avg=0.50, current=0.38 -> -24% triggers stop_loss
        # Raise hard_stop and USD limits so only soft stop triggers
        pos = _make_position(yes_avg_price=0.50, yes_size=100.0)
        em = _build_exit_manager(
            config=_make_exit_config(
                stop_loss={
                    "enabled": True,
                    "threshold_pct": 0.20,
                    "hard_stop_pct": 0.90,
                    "max_loss_per_trade_usd": 999.0,
                },
                take_profit={"enabled": True, "threshold_pct": 0.01},  # low enough to fire
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.38},
        )
        active_markets = {"cond-1": _make_market_metadata()}
        signals = await em.evaluate_all(active_markets)
        # Should have exactly one signal, and it should be stop_loss (not take_profit)
        assert len(signals) == 1
        assert signals[0].reason == "stop_loss"


# ── Resolution exit ramp tests ──────────────────────────────────────────────


class TestResolutionExit:
    """Tests for resolution time-based exit layer."""

    def test_far_from_resolution_no_signal(self):
        """Market far from resolution (hours_left > exit_start_hours) -> no signal."""
        pos = _make_position()
        end_date = datetime.now(UTC) + timedelta(hours=24)
        md = _make_market_metadata(end_date=end_date)
        em = _build_exit_manager(
            config=_make_exit_config(
                resolution={"enabled": True, "exit_start_hours": 6.0, "exit_complete_hours": 2.0}
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.55},
        )
        sig = em._check_resolution(pos, "tok-yes-1", 100.0, md, 0.55)
        assert sig is None

    def test_gradual_exit_window_partial_sell(self):
        """Market in gradual exit window -> fraction increases, partial sell."""
        pos = _make_position(yes_size=100.0)
        # 4 hours left: midpoint of [exit_complete=2, exit_start=6] -> fraction ~0.5
        end_date = datetime.now(UTC) + timedelta(hours=4)
        md = _make_market_metadata(end_date=end_date)
        em = _build_exit_manager(
            config=_make_exit_config(
                resolution={
                    "enabled": True,
                    "exit_start_hours": 6.0,
                    "exit_complete_hours": 2.0,
                    "aggressive_after_hours": 1.0,
                }
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.55},
        )
        sig = em._check_resolution(pos, "tok-yes-1", 100.0, md, 0.55)
        assert sig is not None
        assert sig.reason == "resolution"
        assert sig.urgency == "medium"
        # fraction ~ 0.5, sell_size ~ 50
        assert 40.0 <= sig.size <= 60.0

    def test_past_exit_complete_full_exit(self):
        """Market past exit_complete_hours -> full exit at current price with urgency='high'."""
        pos = _make_position(yes_size=100.0)
        # 1.5 hours left: < exit_complete_hours=2.0 and > aggressive_after_hours=1.0
        end_date = datetime.now(UTC) + timedelta(hours=1.5)
        md = _make_market_metadata(end_date=end_date)
        em = _build_exit_manager(
            config=_make_exit_config(
                resolution={
                    "enabled": True,
                    "exit_start_hours": 6.0,
                    "exit_complete_hours": 2.0,
                    "aggressive_after_hours": 1.0,
                }
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.55},
        )
        sig = em._check_resolution(pos, "tok-yes-1", 100.0, md, 0.55)
        assert sig is not None
        assert sig.reason == "resolution"
        assert sig.urgency == "high"
        assert sig.size == 100.0
        assert sig.price == 0.55

    def test_resolution_ramp_fraction_at_start_boundary(self):
        """At exit_start boundary -> fraction ~ 0 (minimal sell, clamped to min 5.0)."""
        pos = _make_position(yes_size=200.0)
        # Just barely inside exit window: e.g. 5.9 hours left, exit_start=6.0
        end_date = datetime.now(UTC) + timedelta(hours=5.9)
        md = _make_market_metadata(end_date=end_date)
        em = _build_exit_manager(
            config=_make_exit_config(
                resolution={
                    "enabled": True,
                    "exit_start_hours": 6.0,
                    "exit_complete_hours": 2.0,
                    "aggressive_after_hours": 1.0,
                }
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.55},
        )
        sig = em._check_resolution(pos, "tok-yes-1", 200.0, md, 0.55)
        assert sig is not None
        # fraction ~ (6.0 - 5.9 - 2.0) / (6.0 - 2.0) = (5.9-2)/(6-2)=3.9/4=0.975
        # wait: fraction = 1.0 - (hours_left - exit_complete) / (exit_start - exit_complete)
        # fraction = 1.0 - (5.9 - 2.0) / (6.0 - 2.0) = 1.0 - 3.9/4.0 = 0.025
        # sell_size = max(5.0, round(200 * 0.025, 2)) = max(5.0, 5.0) = 5.0
        assert sig.size == 5.0

    def test_resolution_ramp_fraction_at_complete_boundary(self):
        """At exit_complete boundary -> fraction ~ 1.0 (full sell)."""
        pos = _make_position(yes_size=100.0)
        # Just barely above exit_complete: 2.05 hours left
        end_date = datetime.now(UTC) + timedelta(hours=2.05)
        md = _make_market_metadata(end_date=end_date)
        em = _build_exit_manager(
            config=_make_exit_config(
                resolution={
                    "enabled": True,
                    "exit_start_hours": 6.0,
                    "exit_complete_hours": 2.0,
                    "aggressive_after_hours": 1.0,
                }
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.55},
        )
        sig = em._check_resolution(pos, "tok-yes-1", 100.0, md, 0.55)
        assert sig is not None
        # fraction = 1.0 - (2.05 - 2.0) / (6.0 - 2.0) = 1.0 - 0.05/4.0 = 0.9875
        # sell_size = max(5.0, round(100 * 0.9875, 2)) = 98.75
        assert sig.size >= 95.0

    def test_aggressive_exit_critical_urgency(self):
        """Market within aggressive_after_hours -> critical urgency, slippage-tolerant price."""
        pos = _make_position(yes_size=100.0)
        end_date = datetime.now(UTC) + timedelta(hours=0.5)
        md = _make_market_metadata(end_date=end_date)
        em = _build_exit_manager(
            config=_make_exit_config(
                resolution={
                    "enabled": True,
                    "exit_start_hours": 6.0,
                    "exit_complete_hours": 2.0,
                    "aggressive_after_hours": 1.0,
                }
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.55},
        )
        sig = em._check_resolution(pos, "tok-yes-1", 100.0, md, 0.55)
        assert sig is not None
        assert sig.urgency == "critical"
        assert sig.reason == "resolution"
        # Price accepts 2% slippage: 0.55 * 0.98 = 0.539
        assert sig.price is not None
        assert sig.price == pytest.approx(0.539, abs=0.001)

    def test_market_ended_force_exit(self):
        """Market already past end_date -> critical force exit."""
        pos = _make_position(yes_size=100.0)
        end_date = datetime.now(UTC) - timedelta(hours=1)
        md = _make_market_metadata(end_date=end_date)
        em = _build_exit_manager(
            config=_make_exit_config(
                resolution={
                    "enabled": True,
                    "exit_start_hours": 6.0,
                    "exit_complete_hours": 2.0,
                    "aggressive_after_hours": 1.0,
                }
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.55},
        )
        sig = em._check_resolution(pos, "tok-yes-1", 100.0, md, 0.55)
        assert sig is not None
        assert sig.urgency == "critical"
        assert sig.size == 100.0


# ── Take-profit tests ───────────────────────────────────────────────────────


class TestTakeProfit:
    """Tests for take-profit exit layer."""

    def test_partial_exit_above_threshold(self):
        """Unrealized gain above threshold_pct -> partial exit signal."""
        # avg=0.50, current=0.60 -> unrealized_pct = 20%, threshold=15%
        pos = _make_position(
            yes_avg_price=0.50,
            yes_size=100.0,
            last_update=time.time() - 7200,  # 2 hours ago
        )
        em = _build_exit_manager(
            config=_make_exit_config(
                take_profit={
                    "enabled": True,
                    "threshold_pct": 0.15,
                    "partial_exit_pct": 0.50,
                    "full_exit_pct": 0.30,
                    "min_hold_minutes": 30,
                    "cooldown_minutes": 10,
                }
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.60},
        )
        now = time.time()
        sig = em._check_take_profit(pos, "tok-yes-1", 100.0, 0.50, 0.60, now)
        assert sig is not None
        assert sig.reason == "take_profit"
        assert sig.urgency == "medium"
        # partial_exit_pct=0.50 -> sell 50 shares
        assert sig.size == 50.0

    def test_full_exit_above_full_exit_pct(self):
        """Unrealized gain above full_exit_pct -> full exit signal."""
        # avg=0.50, current=0.70 -> unrealized_pct = 40%, full_exit_pct=30%
        pos = _make_position(
            yes_avg_price=0.50,
            yes_size=100.0,
            last_update=time.time() - 7200,
        )
        em = _build_exit_manager(
            config=_make_exit_config(
                take_profit={
                    "enabled": True,
                    "threshold_pct": 0.15,
                    "partial_exit_pct": 0.50,
                    "full_exit_pct": 0.30,
                    "min_hold_minutes": 30,
                    "cooldown_minutes": 10,
                }
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.70},
        )
        now = time.time()
        sig = em._check_take_profit(pos, "tok-yes-1", 100.0, 0.50, 0.70, now)
        assert sig is not None
        assert sig.reason == "take_profit"
        assert sig.urgency == "high"
        assert sig.size == 100.0  # Full position

    def test_min_hold_minutes_blocks_signal(self):
        """TP respects min_hold_minutes — too early -> no signal."""
        # Position opened 5 minutes ago, min_hold is 30 min
        pos = _make_position(
            yes_avg_price=0.50,
            yes_size=100.0,
            last_update=time.time() - 300,  # 5 minutes ago
        )
        em = _build_exit_manager(
            config=_make_exit_config(
                take_profit={
                    "enabled": True,
                    "threshold_pct": 0.10,
                    "partial_exit_pct": 0.50,
                    "full_exit_pct": 0.30,
                    "min_hold_minutes": 30,
                    "cooldown_minutes": 10,
                }
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.65},
        )
        now = time.time()
        sig = em._check_take_profit(pos, "tok-yes-1", 100.0, 0.50, 0.65, now)
        assert sig is None

    def test_cooldown_blocks_repeat_signal(self):
        """TP respects cooldown_minutes — recent TP signal blocks new one."""
        pos = _make_position(
            yes_avg_price=0.50,
            yes_size=100.0,
            last_update=time.time() - 7200,
        )
        em = _build_exit_manager(
            config=_make_exit_config(
                take_profit={
                    "enabled": True,
                    "threshold_pct": 0.15,
                    "partial_exit_pct": 0.50,
                    "full_exit_pct": 0.30,
                    "min_hold_minutes": 30,
                    "cooldown_minutes": 10,
                }
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.60},
        )
        now = time.time()
        # First signal succeeds
        sig1 = em._check_take_profit(pos, "tok-yes-1", 100.0, 0.50, 0.60, now)
        assert sig1 is not None

        # Second signal 5 minutes later (within 10-min cooldown) -> blocked
        sig2 = em._check_take_profit(pos, "tok-yes-1", 100.0, 0.50, 0.60, now + 300)
        assert sig2 is None

    def test_cooldown_expires_allows_signal(self):
        """After cooldown expires, TP fires again."""
        pos = _make_position(
            yes_avg_price=0.50,
            yes_size=100.0,
            last_update=time.time() - 7200,
        )
        em = _build_exit_manager(
            config=_make_exit_config(
                take_profit={
                    "enabled": True,
                    "threshold_pct": 0.15,
                    "partial_exit_pct": 0.50,
                    "full_exit_pct": 0.30,
                    "min_hold_minutes": 30,
                    "cooldown_minutes": 10,
                }
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.60},
        )
        now = time.time()
        sig1 = em._check_take_profit(pos, "tok-yes-1", 100.0, 0.50, 0.60, now)
        assert sig1 is not None

        # 11 minutes later (past 10-min cooldown) -> allowed
        sig2 = em._check_take_profit(pos, "tok-yes-1", 100.0, 0.50, 0.60, now + 660)
        assert sig2 is not None

    def test_gain_below_threshold_no_signal(self):
        """Unrealized gain below threshold_pct -> no signal."""
        # avg=0.50, current=0.55 -> 10%, threshold=15%
        pos = _make_position(
            yes_avg_price=0.50,
            yes_size=100.0,
            last_update=time.time() - 7200,
        )
        em = _build_exit_manager(
            config=_make_exit_config(
                take_profit={
                    "enabled": True,
                    "threshold_pct": 0.15,
                    "partial_exit_pct": 0.50,
                    "full_exit_pct": 0.30,
                    "min_hold_minutes": 30,
                    "cooldown_minutes": 10,
                }
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.55},
        )
        now = time.time()
        sig = em._check_take_profit(pos, "tok-yes-1", 100.0, 0.50, 0.55, now)
        assert sig is None


# ── Orphan detection tests ───────────────────────────────────────────────────


class TestOrphan:
    """Tests for orphan position detection."""

    @pytest.mark.asyncio
    async def test_position_not_in_active_markets_generates_orphan(self):
        """Position not in active_markets -> orphan signal generated."""
        pos = _make_position(yes_size=50.0)
        em = _build_exit_manager(
            config=_make_exit_config(orphan={"check_interval_s": 0, "min_size_to_unwind": 5.0}),
            positions=[pos],
            prices={"tok-yes-1": 0.55},
        )
        sig = await em._check_orphan(pos, "tok-yes-1", 50.0, 0.55)
        assert sig is not None
        assert sig.reason == "orphan"
        assert sig.urgency == "low"
        assert sig.size == 50.0

    @pytest.mark.asyncio
    async def test_position_in_active_markets_no_orphan(self):
        """Position in active_markets -> no orphan signal via evaluate_all."""
        pos = _make_position(yes_size=50.0, yes_avg_price=0.55)
        em = _build_exit_manager(
            config=_make_exit_config(
                stop_loss={"enabled": False},
                take_profit={"enabled": False},
                resolution={"enabled": False},
                orphan={"check_interval_s": 0, "min_size_to_unwind": 5.0},
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.55},
        )
        # Market IS in active_markets -> not orphan
        active_markets = {"cond-1": _make_market_metadata()}
        signals = await em.evaluate_all(active_markets)
        orphan_signals = [s for s in signals if s.reason == "orphan"]
        assert len(orphan_signals) == 0

    @pytest.mark.asyncio
    async def test_orphan_below_min_size_no_signal(self):
        """Orphan below min_size_to_unwind -> no signal."""
        pos = _make_position(yes_size=3.0)
        em = _build_exit_manager(
            config=_make_exit_config(orphan={"check_interval_s": 0, "min_size_to_unwind": 5.0}),
            positions=[pos],
            prices={"tok-yes-1": 0.55},
        )
        sig = await em._check_orphan(pos, "tok-yes-1", 3.0, 0.55)
        assert sig is None

    @pytest.mark.asyncio
    async def test_orphan_no_price_no_signal(self):
        """Orphan with no available price -> no signal."""
        pos = _make_position(yes_size=50.0)
        em = _build_exit_manager(
            config=_make_exit_config(orphan={"check_interval_s": 0, "min_size_to_unwind": 5.0}),
            positions=[pos],
            prices={},  # No book data
        )
        sig = await em._check_orphan(pos, "tok-yes-1", 50.0, None)
        assert sig is None

    @pytest.mark.asyncio
    async def test_orphan_rest_fallback(self):
        """Orphan uses REST fallback when WS price is None."""
        pos = _make_position(yes_size=50.0)

        mock_clob = MagicMock()
        mock_bid = MagicMock()
        mock_bid.price = "0.45"
        mock_rest_book = MagicMock()
        mock_rest_book.bids = [mock_bid]
        mock_clob.get_order_book = MagicMock(return_value=mock_rest_book)

        # Make the mock awaitable
        async def mock_get_order_book(token_id):
            return mock_rest_book

        mock_clob.get_order_book = mock_get_order_book

        em = _build_exit_manager(
            config=_make_exit_config(orphan={"check_interval_s": 0, "min_size_to_unwind": 5.0}),
            positions=[pos],
            prices={},
        )
        em.clob_public = mock_clob

        sig = await em._check_orphan(pos, "tok-yes-1", 50.0, None)
        assert sig is not None
        assert sig.price == 0.45
        assert sig.reason == "orphan"


# ── evaluate_all tests ───────────────────────────────────────────────────────


class TestEvaluateAll:
    """Tests for the evaluate_all orchestrator method."""

    @pytest.mark.asyncio
    async def test_flatten_all_positions_get_critical_signals(self):
        """Flatten flag -> all positions get critical exit signals."""
        pos1 = _make_position(
            condition_id="cond-1",
            token_yes="tok-yes-1",
            token_no="tok-no-1",
            yes_size=100.0,
            yes_avg_price=0.50,
        )
        pos2 = _make_position(
            condition_id="cond-2",
            token_yes="tok-yes-2",
            token_no="tok-no-2",
            yes_size=80.0,
            yes_avg_price=0.60,
        )
        em = _build_exit_manager(
            positions=[pos1, pos2],
            prices={"tok-yes-1": 0.45, "tok-yes-2": 0.55},
        )

        # Simulate flatten by patching the flag file existence check
        with patch("os.path.exists", return_value=True):
            active_markets = {
                "cond-1": _make_market_metadata("cond-1"),
                "cond-2": _make_market_metadata("cond-2"),
            }
            signals = await em.evaluate_all(active_markets)

        assert len(signals) == 2
        for sig in signals:
            assert sig.urgency == "critical"
            assert sig.reason == "flatten"

    @pytest.mark.asyncio
    async def test_flatten_via_kill_switch(self):
        """Flatten triggered via kill_switch object."""
        pos = _make_position(yes_size=100.0, yes_avg_price=0.50)

        kill_switch = MagicMock()
        kill_switch.is_triggered = True

        em = _build_exit_manager(
            positions=[pos],
            prices={"tok-yes-1": 0.45},
            kill_switch=kill_switch,
        )

        active_markets = {"cond-1": _make_market_metadata("cond-1")}
        signals = await em.evaluate_all(active_markets)

        assert len(signals) == 1
        assert signals[0].reason == "flatten"
        assert signals[0].urgency == "critical"

    @pytest.mark.asyncio
    async def test_priority_ordering_flatten_before_stop_loss(self):
        """Priority ordering: flatten signals sorted before stop_loss."""
        # Create two positions: one triggers flatten, one triggers stop_loss
        pos1 = _make_position(
            condition_id="cond-1",
            token_yes="tok-yes-1",
            token_no="tok-no-1",
            yes_size=100.0,
            yes_avg_price=0.50,
        )
        pos2 = _make_position(
            condition_id="cond-2",
            token_yes="tok-yes-2",
            token_no="tok-no-2",
            yes_size=80.0,
            yes_avg_price=0.50,
        )

        em = _build_exit_manager(
            config=_make_exit_config(
                stop_loss={"enabled": True, "threshold_pct": 0.20},
            ),
            positions=[pos1, pos2],
            prices={"tok-yes-1": 0.45, "tok-yes-2": 0.35},
        )

        # flatten applies to ALL positions
        with patch("os.path.exists", return_value=True):
            active_markets = {
                "cond-1": _make_market_metadata("cond-1"),
                "cond-2": _make_market_metadata("cond-2"),
            }
            signals = await em.evaluate_all(active_markets)

        # All flatten -> all critical -> all come first
        assert all(s.urgency == "critical" for s in signals)
        assert all(s.reason == "flatten" for s in signals)

    @pytest.mark.asyncio
    async def test_priority_ordering_stop_loss_before_resolution(self):
        """Signals are sorted: critical (stop/flatten) > high > medium > low."""
        pos1 = _make_position(
            condition_id="cond-1",
            token_yes="tok-yes-1",
            token_no="tok-no-1",
            yes_size=100.0,
            yes_avg_price=0.50,
        )
        pos2 = _make_position(
            condition_id="cond-2",
            token_yes="tok-yes-2",
            token_no="tok-no-2",
            yes_size=80.0,
            yes_avg_price=0.55,
        )

        end_date = datetime.now(UTC) + timedelta(hours=4)
        em = _build_exit_manager(
            config=_make_exit_config(
                stop_loss={
                    "enabled": True,
                    "threshold_pct": 0.20,
                    "hard_stop_pct": 0.90,
                    "max_loss_per_trade_usd": 999.0,
                },
                resolution={
                    "enabled": True,
                    "exit_start_hours": 6.0,
                    "exit_complete_hours": 2.0,
                    "aggressive_after_hours": 1.0,
                },
            ),
            positions=[pos1, pos2],
            prices={"tok-yes-1": 0.38, "tok-yes-2": 0.55},
        )

        active_markets = {
            "cond-1": _make_market_metadata("cond-1"),  # No end_date -> no resolution
            "cond-2": _make_market_metadata("cond-2", end_date=end_date),
        }
        signals = await em.evaluate_all(active_markets)

        assert len(signals) == 2
        # stop_loss=high should sort before resolution=medium
        assert signals[0].urgency == "high"
        assert signals[0].reason == "stop_loss"
        assert signals[1].urgency == "medium"
        assert signals[1].reason == "resolution"

    @pytest.mark.asyncio
    async def test_positions_below_minimum_size_skipped(self):
        """Positions below 5.0 shares -> skipped entirely."""
        pos = _make_position(
            yes_size=3.0,  # Below 5.0 threshold
            no_size=2.0,   # Also below
            yes_avg_price=0.50,
        )
        em = _build_exit_manager(
            config=_make_exit_config(
                stop_loss={"enabled": True, "threshold_pct": 0.01},  # Very low threshold
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.10},  # Huge loss but small size
        )
        active_markets = {"cond-1": _make_market_metadata()}
        signals = await em.evaluate_all(active_markets)
        assert len(signals) == 0

    @pytest.mark.asyncio
    async def test_both_sides_evaluated_independently(self):
        """YES and NO sides are evaluated independently."""
        pos = _make_position(
            yes_size=100.0,
            no_size=80.0,
            yes_avg_price=0.50,
            no_avg_price=0.50,
        )
        em = _build_exit_manager(
            config=_make_exit_config(
                stop_loss={"enabled": True, "threshold_pct": 0.20},
            ),
            positions=[pos],
            # Both sides have large losses
            prices={"tok-yes-1": 0.38, "tok-no-1": 0.38},
        )
        active_markets = {"cond-1": _make_market_metadata()}
        signals = await em.evaluate_all(active_markets)
        # Both YES and NO should produce stop_loss signals
        assert len(signals) == 2
        token_ids = {s.token_id for s in signals}
        assert "tok-yes-1" in token_ids
        assert "tok-no-1" in token_ids

    @pytest.mark.asyncio
    async def test_disabled_stop_loss_no_signals(self):
        """When stop_loss is disabled, no stop_loss signals are generated."""
        pos = _make_position(yes_avg_price=0.50, yes_size=100.0)
        em = _build_exit_manager(
            config=_make_exit_config(
                stop_loss={"enabled": False},
                take_profit={"enabled": False},
                resolution={"enabled": False},
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.10},
        )
        active_markets = {"cond-1": _make_market_metadata()}
        signals = await em.evaluate_all(active_markets)
        stop_signals = [s for s in signals if s.reason in ("stop_loss", "hard_stop")]
        assert len(stop_signals) == 0


# ── get_resolution_action tests ──────────────────────────────────────────────


class TestGetResolutionAction:
    """Tests for the get_resolution_action helper used by main loop."""

    def test_no_end_date_returns_none(self):
        md = _make_market_metadata(end_date=None)
        em = _build_exit_manager(
            config=_make_exit_config(resolution={"enabled": True}),
        )
        result = em.get_resolution_action("cond-1", {"cond-1": md})
        assert result is None

    def test_resolution_disabled_returns_none(self):
        end_date = datetime.now(UTC) + timedelta(hours=1)
        md = _make_market_metadata(end_date=end_date)
        em = _build_exit_manager(
            config=_make_exit_config(resolution={"enabled": False}),
        )
        result = em.get_resolution_action("cond-1", {"cond-1": md})
        assert result is None

    def test_force_exit_when_past_end(self):
        end_date = datetime.now(UTC) - timedelta(hours=1)
        md = _make_market_metadata(end_date=end_date)
        em = _build_exit_manager(
            config=_make_exit_config(resolution={"enabled": True}),
        )
        result = em.get_resolution_action("cond-1", {"cond-1": md})
        assert result is not None
        assert result.action == "FORCE_EXIT"
        assert result.fraction == 1.0
        assert result.block_new_buys is True

    def test_gradual_exit_in_ramp_window(self):
        end_date = datetime.now(UTC) + timedelta(hours=4)
        md = _make_market_metadata(end_date=end_date)
        em = _build_exit_manager(
            config=_make_exit_config(
                resolution={
                    "enabled": True,
                    "exit_start_hours": 6.0,
                    "exit_complete_hours": 2.0,
                }
            ),
        )
        result = em.get_resolution_action("cond-1", {"cond-1": md})
        assert result is not None
        assert result.action == "GRADUAL_EXIT"
        assert 0.4 <= result.fraction <= 0.6  # ~0.5 at midpoint
        assert result.block_new_buys is True

    def test_no_action_far_from_resolution(self):
        end_date = datetime.now(UTC) + timedelta(hours=100)
        md = _make_market_metadata(end_date=end_date)
        em = _build_exit_manager(
            config=_make_exit_config(
                resolution={
                    "enabled": True,
                    "exit_start_hours": 6.0,
                    "block_new_buys_hours": 8.0,
                }
            ),
        )
        result = em.get_resolution_action("cond-1", {"cond-1": md})
        assert result is None


# ── Flatten signal build tests ───────────────────────────────────────────────


class TestFlattenSignal:
    """Tests for _build_flatten_signal."""

    def test_flatten_signal_price_tolerance(self):
        """Flatten signal applies price tolerance."""
        pos = _make_position(yes_size=100.0)
        em = _build_exit_manager(
            config=_make_exit_config(
                flatten={"config_flag_path": "/tmp/test_flatten", "price_tolerance_pct": 0.05}
            ),
            positions=[pos],
            prices={"tok-yes-1": 0.50},
        )
        sig = em._build_flatten_signal(pos, "tok-yes-1", 100.0, 0.50)
        assert sig is not None
        # price = 0.50 * (1.0 - 0.05) = 0.475
        assert sig.price == pytest.approx(0.475, abs=0.001)

    def test_flatten_signal_below_min_size(self):
        """Flatten for position below 5.0 shares -> None."""
        pos = _make_position(yes_size=3.0)
        em = _build_exit_manager(positions=[pos])
        sig = em._build_flatten_signal(pos, "tok-yes-1", 3.0, 0.50)
        assert sig is None


# ── SellSignal model tests ──────────────────────────────────────────────────


class TestSellSignalModel:
    def test_defaults(self):
        sig = SellSignal(token_id="tok-1", condition_id="cond-1", size=10.0)
        assert sig.price is None
        assert sig.urgency == "low"
        assert sig.reason == ""

    def test_full_fields(self):
        sig = SellSignal(
            token_id="tok-1",
            condition_id="cond-1",
            size=50.0,
            price=0.45,
            urgency="critical",
            reason="flatten",
        )
        assert sig.price == 0.45
        assert sig.urgency == "critical"


# ── R-H2: Empty book emergency exit tests ────────────────────────────────────


class TestEmptyBookEmergencyExit:
    """Tests for R-H2: stop-loss fails on empty book — emergency exit for aged positions."""

    @pytest.mark.asyncio
    async def test_empty_book_emergency_exit_after_8_hours(self):
        """Position held >8h with no book -> exit signal reason='empty_book_aged'."""
        # Position held for 9 hours (32400 seconds) with no book data
        pos = _make_position(
            yes_avg_price=0.50,
            yes_size=100.0,
            last_update=time.time() - 32400,  # 9 hours ago
        )
        em = _build_exit_manager(
            config=_make_exit_config(
                stop_loss={"enabled": True, "threshold_pct": 0.20},
            ),
            positions=[pos],
            prices={},  # No book data at all — empty book
        )
        # Market IS in active_markets (not orphan)
        active_markets = {"cond-1": _make_market_metadata()}
        signals = await em.evaluate_all(active_markets)

        assert len(signals) == 1
        sig = signals[0]
        assert sig.reason == "empty_book_aged"
        assert sig.urgency == "high"
        assert sig.size == 100.0
        # Price should be avg_price * 0.90 = 0.50 * 0.90 = 0.45
        assert sig.price == pytest.approx(0.45, abs=0.001)
        assert sig.token_id == "tok-yes-1"
        assert sig.condition_id == "cond-1"

    @pytest.mark.asyncio
    async def test_empty_book_under_8_hours_no_emergency_exit(self):
        """Position with no book data held < 8 hours does NOT generate emergency exit."""
        # Position held for 5 hours — should warn but not generate signal
        pos = _make_position(
            yes_avg_price=0.50,
            yes_size=100.0,
            last_update=time.time() - 18000,  # 5 hours ago
        )
        em = _build_exit_manager(
            config=_make_exit_config(
                stop_loss={"enabled": True, "threshold_pct": 0.20},
                take_profit={"enabled": False},
                resolution={"enabled": False},
            ),
            positions=[pos],
            prices={},  # No book data
        )
        active_markets = {"cond-1": _make_market_metadata()}
        signals = await em.evaluate_all(active_markets)

        # No emergency exit signal (position is not old enough)
        emergency_signals = [s for s in signals if s.reason == "empty_book_aged"]
        assert len(emergency_signals) == 0

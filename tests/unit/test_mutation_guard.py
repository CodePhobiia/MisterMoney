"""Tests for LiveMutationGuard — safety-critical order gate (FIX-02).

Every guard path must have at least one positive test (blocks when it should)
and one negative test (allows when it shouldn't block).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from pmm1.api.clob_private import CreateOrderRequest, OrderSide
from pmm1.execution.mutation_guard import LiveMutationGuard, MutationGuardDecision
from pmm1.risk.drawdown import DrawdownGovernor, DrawdownState, DrawdownTier
from pmm1.risk.kill_switch import KillSwitch, KillSwitchReason
from pmm1.risk.limits import LimitCheckResult, RiskLimits

# ── Helpers ──────────────────────────────────────────────────────────────

def _buy_request(
    token_id: str = "tok_yes", price: str = "0.50", size: str = "10",
) -> CreateOrderRequest:
    return CreateOrderRequest(
        token_id=token_id, price=price, size=size, side=OrderSide.BUY,
    )


def _sell_request(
    token_id: str = "tok_yes", price: str = "0.50", size: str = "10",
) -> CreateOrderRequest:
    return CreateOrderRequest(
        token_id=token_id, price=price, size=size, side=OrderSide.SELL,
    )


def _make_guard(
    *,
    kill_triggered: bool = False,
    kill_reasons: list[KillSwitchReason] | None = None,
    heartbeat_healthy: bool = True,
    heartbeat_failures: int = 0,
    flatten_only: bool = False,
    pause_taker: bool = False,
    dd_tier: DrawdownTier = DrawdownTier.NORMAL,
    resume_token_valid: bool = True,
    can_buy: bool = True,
    can_sell: bool = True,
    market_check_pass: bool = True,
    cluster_check_pass: bool = True,
    directional_check_pass: bool = True,
    market_event_id: str = "evt_1",
) -> LiveMutationGuard:
    """Build a LiveMutationGuard with controllable mocks."""
    # Kill switch
    ks = MagicMock(spec=KillSwitch)
    ks.is_triggered = kill_triggered
    ks.active_reasons = kill_reasons or []

    # Heartbeat
    hb = MagicMock()
    hb.is_healthy = heartbeat_healthy
    hb.consecutive_failures = heartbeat_failures

    # Drawdown
    dd = MagicMock(spec=DrawdownGovernor)
    dd_state = DrawdownState(tier=dd_tier)
    dd.state = dd_state

    # Inventory
    inv = MagicMock()
    inv.can_place_buy.return_value = can_buy
    inv.can_place_sell.return_value = can_sell

    # Risk limits
    rl = MagicMock(spec=RiskLimits)
    rl.check_per_market_gross.return_value = LimitCheckResult(
        passed=market_check_pass,
        breaches=[] if market_check_pass else ["per_market_gross_breach"],
    )
    rl.check_per_event_cluster.return_value = LimitCheckResult(
        passed=cluster_check_pass,
        breaches=[] if cluster_check_pass else ["event_cluster_breach"],
    )
    rl.check_total_directional.return_value = LimitCheckResult(
        passed=directional_check_pass,
        breaches=[] if directional_check_pass else ["total_directional_breach"],
    )

    # Market getter
    market = SimpleNamespace(event_id=market_event_id)

    return LiveMutationGuard(
        risk_limits=rl,
        inventory_manager=inv,
        kill_switch=ks,
        drawdown=dd,
        heartbeat=hb,
        market_getter=lambda cid: market,
        resume_token_getter=lambda: resume_token_valid,
        resume_reason_getter=lambda: "manual_pause",
    )


def _eval(guard: LiveMutationGuard, request: CreateOrderRequest) -> MutationGuardDecision:
    return guard.evaluate(request, condition_id="cond_abc", strategy="maker")


# ── Guard 1: Kill Switch ────────────────────────────────────────────────

class TestKillSwitchGuard:
    def test_blocks_buy_when_triggered(self):
        guard = _make_guard(
            kill_triggered=True,
            kill_reasons=[KillSwitchReason.MANUAL],
        )
        result = _eval(guard, _buy_request())
        assert result.allowed is False
        assert result.reason == "kill_switch_active"

    def test_blocks_sell_when_triggered(self):
        guard = _make_guard(
            kill_triggered=True,
            kill_reasons=[KillSwitchReason.DRAWDOWN],
        )
        result = _eval(guard, _sell_request())
        assert result.allowed is False
        assert result.reason == "kill_switch_active"

    def test_allows_when_not_triggered(self):
        guard = _make_guard(kill_triggered=False)
        result = _eval(guard, _buy_request())
        assert result.allowed is True


# ── Guard 2: Heartbeat ──────────────────────────────────────────────────

class TestHeartbeatGuard:
    def test_blocks_when_unhealthy(self):
        guard = _make_guard(heartbeat_healthy=False, heartbeat_failures=3)
        result = _eval(guard, _buy_request())
        assert result.allowed is False
        assert result.reason == "heartbeat_unhealthy"

    def test_allows_when_healthy(self):
        guard = _make_guard(heartbeat_healthy=True)
        result = _eval(guard, _buy_request())
        assert result.allowed is True

    def test_allows_when_heartbeat_is_none(self):
        """Guard should pass when no heartbeat state is provided."""
        guard = _make_guard()
        # Replace heartbeat with None
        guard._heartbeat = None
        result = _eval(guard, _buy_request())
        assert result.allowed is True


# ── Guard 3: Drawdown Flatten Only ──────────────────────────────────────

class TestDrawdownFlattenGuard:
    def test_blocks_when_flatten_only(self):
        guard = _make_guard(dd_tier=DrawdownTier.TIER3_FLATTEN_ONLY)
        result = _eval(guard, _buy_request())
        assert result.allowed is False
        assert result.reason == "drawdown_flatten_only"

    def test_allows_in_normal_tier(self):
        guard = _make_guard(dd_tier=DrawdownTier.NORMAL)
        result = _eval(guard, _buy_request())
        assert result.allowed is True


# ── Guard 4: Resume Token ───────────────────────────────────────────────

class TestResumeTokenGuard:
    def test_blocks_when_token_invalid(self):
        guard = _make_guard(resume_token_valid=False)
        result = _eval(guard, _buy_request())
        assert result.allowed is False
        assert result.reason == "resume_token_invalid"
        assert result.details["blocked_reason"] == "manual_pause"

    def test_allows_when_token_valid(self):
        guard = _make_guard(resume_token_valid=True)
        result = _eval(guard, _buy_request())
        assert result.allowed is True

    def test_allows_when_no_resume_getter(self):
        """Guard passes when resume_token_getter is None."""
        guard = _make_guard()
        guard._resume_token_getter = None
        result = _eval(guard, _buy_request())
        assert result.allowed is True


# ── Guard 5: Taker Bootstrap Pause ──────────────────────────────────────

class TestTakerBootstrapGuard:
    def test_blocks_taker_buy_in_tier1(self):
        guard = _make_guard(dd_tier=DrawdownTier.TIER1_PAUSE_TAKER)
        result = guard.evaluate(
            _buy_request(),
            condition_id="cond_abc",
            strategy="taker_bootstrap",
        )
        assert result.allowed is False
        assert result.reason == "drawdown_pause_taker"

    def test_allows_maker_buy_in_tier1(self):
        """Tier1 only blocks taker strategy, not maker."""
        guard = _make_guard(dd_tier=DrawdownTier.TIER1_PAUSE_TAKER)
        result = guard.evaluate(
            _buy_request(),
            condition_id="cond_abc",
            strategy="maker",
        )
        # Maker is NOT blocked by pause_taker — continues to other checks
        # (all other checks pass in default mock)
        assert result.allowed is True

    def test_allows_taker_in_normal_tier(self):
        guard = _make_guard(dd_tier=DrawdownTier.NORMAL)
        result = guard.evaluate(
            _buy_request(),
            condition_id="cond_abc",
            strategy="taker_bootstrap",
        )
        assert result.allowed is True


# ── Guard 6: Insufficient Inventory (SELL) ──────────────────────────────

class TestInventorySellGuard:
    def test_blocks_sell_insufficient_inventory(self):
        guard = _make_guard(can_sell=False)
        result = _eval(guard, _sell_request())
        assert result.allowed is False
        assert result.reason == "insufficient_inventory"

    def test_allows_sell_with_inventory(self):
        guard = _make_guard(can_sell=True)
        result = _eval(guard, _sell_request())
        assert result.allowed is True

    def test_sell_returns_early_on_success(self):
        """Allowed SELL returns immediately — no risk limit checks."""
        guard = _make_guard(
            can_sell=True,
            market_check_pass=False,  # Would block BUY
        )
        result = _eval(guard, _sell_request())
        assert result.allowed is True  # SELL bypasses risk limits


# ── Guard 7: Insufficient USDC (BUY) ───────────────────────────────────

class TestInsufficientUsdcGuard:
    def test_blocks_buy_insufficient_usdc(self):
        guard = _make_guard(can_buy=False)
        result = _eval(guard, _buy_request())
        assert result.allowed is False
        assert result.reason == "insufficient_usdc"

    def test_allows_buy_with_usdc(self):
        guard = _make_guard(can_buy=True)
        result = _eval(guard, _buy_request())
        assert result.allowed is True


# ── Guard 8: Per-Market Gross ───────────────────────────────────────────

class TestPerMarketGrossGuard:
    def test_blocks_buy_on_market_breach(self):
        guard = _make_guard(market_check_pass=False)
        result = _eval(guard, _buy_request())
        assert result.allowed is False
        assert result.reason == "per_market_gross"

    def test_allows_buy_within_market_limit(self):
        guard = _make_guard(market_check_pass=True)
        result = _eval(guard, _buy_request())
        assert result.allowed is True


# ── Guard 9: Event Cluster ──────────────────────────────────────────────

class TestEventClusterGuard:
    def test_blocks_buy_on_cluster_breach(self):
        guard = _make_guard(cluster_check_pass=False)
        result = _eval(guard, _buy_request())
        assert result.allowed is False
        assert result.reason == "event_cluster"

    def test_allows_buy_within_cluster_limit(self):
        guard = _make_guard(cluster_check_pass=True)
        result = _eval(guard, _buy_request())
        assert result.allowed is True

    def test_skips_cluster_check_when_no_event_id(self):
        """No event_id → skip cluster check."""
        guard = _make_guard(
            cluster_check_pass=False,
            market_event_id="",
        )
        result = _eval(guard, _buy_request())
        # Cluster check skipped, continues to directional check
        assert result.allowed is True


# ── Guard 10: Total Directional ─────────────────────────────────────────

class TestTotalDirectionalGuard:
    def test_blocks_buy_on_directional_breach(self):
        guard = _make_guard(directional_check_pass=False)
        result = _eval(guard, _buy_request())
        assert result.allowed is False
        assert result.reason == "total_directional"

    def test_allows_buy_within_directional_limit(self):
        guard = _make_guard(directional_check_pass=True)
        result = _eval(guard, _buy_request())
        assert result.allowed is True


# ── Guard Ordering / Cascade ────────────────────────────────────────────

class TestGuardPriority:
    def test_kill_switch_beats_all(self):
        """Kill switch should block even if everything else would pass."""
        guard = _make_guard(
            kill_triggered=True,
            kill_reasons=[KillSwitchReason.MANUAL],
            heartbeat_healthy=True,
            can_buy=True,
            market_check_pass=True,
        )
        result = _eval(guard, _buy_request())
        assert result.reason == "kill_switch_active"

    def test_heartbeat_beats_drawdown(self):
        """Heartbeat check comes before drawdown in guard order."""
        guard = _make_guard(
            heartbeat_healthy=False,
            heartbeat_failures=5,
            dd_tier=DrawdownTier.TIER3_FLATTEN_ONLY,
        )
        result = _eval(guard, _buy_request())
        assert result.reason == "heartbeat_unhealthy"

    def test_drawdown_beats_resume_token(self):
        """Drawdown flatten check comes before resume token."""
        guard = _make_guard(
            dd_tier=DrawdownTier.TIER3_FLATTEN_ONLY,
            resume_token_valid=False,
        )
        result = _eval(guard, _buy_request())
        assert result.reason == "drawdown_flatten_only"

    def test_all_pass_returns_allowed(self):
        """When every guard passes, order is allowed."""
        guard = _make_guard()
        result = _eval(guard, _buy_request())
        assert result.allowed is True
        assert result.reason == ""


# ── Decision Model ──────────────────────────────────────────────────────

class TestMutationGuardDecision:
    def test_default_is_allowed(self):
        d = MutationGuardDecision()
        assert d.allowed is True
        assert d.reason == ""
        assert d.details == {}

    def test_blocked_with_details(self):
        d = MutationGuardDecision(
            allowed=False,
            reason="test_block",
            details={"key": "value"},
        )
        assert d.allowed is False
        assert d.details["key"] == "value"

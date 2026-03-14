from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from pmm2.config import PMM2Config
from pmm2.planner.diff_engine import DiffEngine
from pmm2.planner.quote_planner import TargetQuotePlan
from pmm2.runtime.integration import maybe_init_pmm2
from pmm2.runtime.loops import PMM2Runtime
from pmm2.v1_views import adapt_live_order


def test_adapt_live_order_converts_tracked_order_strings_to_numeric_view() -> None:
    tracked = SimpleNamespace(
        order_id="order-1",
        token_id="token-1",
        condition_id="condition-1",
        side="BUY",
        price="0.48",
        remaining_size="12",
        strategy="mm",
        is_scoring=True,
    )

    view = adapt_live_order(tracked)

    assert view.price == 0.48
    assert view.size_open == 12.0
    assert view.strategy == "mm"
    assert view.is_scoring is True


def test_diff_engine_accepts_string_backed_tracked_orders() -> None:
    live_order = SimpleNamespace(
        order_id="live-1",
        token_id="token-1",
        condition_id="condition-1",
        side="BUY",
        price="0.48",
        remaining_size="10",
        remaining_size_float=10.0,
    )
    target = TargetQuotePlan(condition_id="condition-1")
    target.ladder = [
        SimpleNamespace(
            token_id="token-1",
            condition_id="condition-1",
            side="BUY",
            price=0.48,
            size=10.0,
        )
    ]

    mutations = DiffEngine().diff(target, [live_order], {})

    assert mutations == []


def test_control_lease_expires_fail_closed(monkeypatch) -> None:
    monkeypatch.setenv("PMM1_ACK_PMM2_LIVE", "YES")
    config = PMM2Config(
        enabled=True,
        shadow_mode=False,
        live_enabled=True,
        live_capital_pct=0.05,
        canary={"enabled": True},
    )
    runtime = PMM2Runtime(config, db=None, bridge=SimpleNamespace(order_manager=None))
    runtime.controlled_markets = {"condition-1"}
    runtime._control_lease_expires_at = time.time() - 1.0

    assert runtime.get_controlled_markets() == set()
    assert runtime.should_v1_skip_market("condition-1") is False


def test_maybe_init_pmm2_raises_when_enabled_config_is_invalid(monkeypatch) -> None:
    monkeypatch.delenv("PMM1_ACK_PMM2_LIVE", raising=False)
    settings = SimpleNamespace(
        raw_config={
            "pmm2": {
                "enabled": True,
                "shadow_mode": False,
                "live_enabled": True,
                "live_capital_pct": 0.05,
                "canary": {"enabled": True},
            }
        }
    )
    bot_state = SimpleNamespace(order_manager=object(), risk_limits=object(), spine=None)

    async def _run() -> None:
        await maybe_init_pmm2(settings, None, bot_state)

    try:
        asyncio.run(_run())
    except ValueError as exc:
        assert "PMM1_ACK_PMM2_LIVE=YES" in str(exc)
    else:
        raise AssertionError("maybe_init_pmm2 should fail closed when PMM2 is explicitly enabled")

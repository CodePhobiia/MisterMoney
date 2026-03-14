"""Phase 5 PMM-2 canary gating and controller telemetry tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pmm2.config import PMM2Config
from pmm2.runtime.loops import PMM2Runtime
from pmm2.runtime.v1_bridge import V1Bridge
from pmm2.universe.metadata import EnrichedMarket


def _market(
    condition_id: str,
    *,
    reward_eligible: bool = True,
    ambiguity_score: float = 0.05,
    is_neg_risk: bool = False,
    volume_24h: float = 75_000.0,
    liquidity: float = 5_000.0,
    spread_cents: float = 2.0,
    hours_to_resolution: float = 72.0,
    has_placeholder_outcomes: bool = False,
) -> EnrichedMarket:
    return EnrichedMarket(
        condition_id=condition_id,
        question=f"Question for {condition_id}",
        token_id_yes=f"{condition_id}-yes",
        token_id_no=f"{condition_id}-no",
        event_id=f"{condition_id}-event",
        best_bid=0.49,
        best_ask=0.51,
        mid=0.50,
        spread_cents=spread_cents,
        volume_24h=volume_24h,
        liquidity=liquidity,
        reward_eligible=reward_eligible,
        reward_daily_rate=25.0 if reward_eligible else 0.0,
        reward_min_size=5.0,
        reward_max_spread=10.0,
        hours_to_resolution=hours_to_resolution,
        is_neg_risk=is_neg_risk,
        has_placeholder_outcomes=has_placeholder_outcomes,
        ambiguity_score=ambiguity_score,
        accepting_orders=True,
        active=True,
    )


def test_runtime_canary_filters_universe_and_reports_status(monkeypatch):
    monkeypatch.setenv("PMM1_ACK_PMM2_LIVE", "YES")
    config = PMM2Config(
        enabled=True,
        shadow_mode=False,
        live_enabled=True,
        live_capital_pct=0.05,
        canary={"enabled": True, "max_markets": 1},
    )
    runtime = PMM2Runtime(config, db=None, bridge=SimpleNamespace(order_manager=None))

    markets = [
        _market("eligible"),
        _market("no_reward", reward_eligible=False),
        _market("ambiguous", ambiguity_score=0.40),
    ]

    summary = runtime._summarize_canary_universe(markets)
    runtime.last_canary_summary = summary
    selected = runtime._select_live_universe(markets)
    runtime.controlled_markets = {"eligible"}
    runtime._refresh_control_lease()

    assert summary["eligible_markets"] == 1
    assert summary["selected_condition_ids"] == ["eligible"]
    assert summary["rejection_counts"]["not_reward_eligible"] == 1
    assert summary["rejection_counts"]["ambiguity_above_limit"] == 1
    assert [market.condition_id for market in selected] == ["eligible"]
    assert runtime.should_v1_skip_market("eligible") is True

    status = runtime.get_status()
    assert status["stage"] == "canary_5pct"
    assert status["controller"] == "pmm2_canary"
    assert status["controlled_market_count"] == 1
    assert status["canary"]["selected_condition_ids"] == ["eligible"]


class _FakeTracker:
    def __init__(self) -> None:
        self.orders = {
            "legacy-order": SimpleNamespace(strategy="mm"),
        }
        self.updated: list[tuple[str, str, str]] = []

    def get(self, order_id: str):
        return self.orders.get(order_id)

    def update_state(self, order_id: str, state, source: str = "") -> bool:
        self.updated.append((order_id, getattr(state, "value", str(state)), source))
        return True


class _FakeClient:
    def __init__(self) -> None:
        self.cancelled: list[list[str]] = []

    async def cancel_orders(self, order_ids: list[str]) -> None:
        self.cancelled.append(list(order_ids))


class _FakeOrderManager:
    def __init__(self) -> None:
        self._client = _FakeClient()
        self._tracker = _FakeTracker()
        self.submit_calls: list[dict[str, str]] = []

    async def submit_order(self, request, *, condition_id: str = "", strategy: str = "manual"):
        self.submit_calls.append(
            {
                "token_id": request.token_id,
                "condition_id": condition_id,
                "strategy": strategy,
            }
        )
        return {"success": True, "order_id": "pmm2-order-1"}


def test_v1_bridge_live_mode_tags_pmm2_orders_and_cancels():
    from pmm2.planner import OrderMutation

    order_manager = _FakeOrderManager()
    bridge = V1Bridge(
        order_manager=order_manager,
        shadow_mode=False,
        controller_label="pmm2_canary",
        stage_name="canary_5pct",
        live_capital_pct=0.05,
        strategy_label="pmm2_canary",
    )

    mutations = [
        OrderMutation(
            action="add",
            token_id="token-yes",
            condition_id="cid1",
            side="BUY",
            price=0.48,
            size=10.0,
            reason="target_rung_unmatched",
        ),
        OrderMutation(
            action="cancel",
            order_id="legacy-order",
            token_id="token-yes",
            condition_id="cid1",
            reason="handoff_to_pmm2_control",
        ),
    ]

    result = asyncio.run(bridge.execute_mutations(mutations))

    assert result["shadow"] is False
    assert result["executed"] == 2
    assert result["failed"] == 0
    assert order_manager.submit_calls[0]["strategy"] == "pmm2_canary"
    assert order_manager.submit_calls[0]["condition_id"] == "cid1"
    assert order_manager._client.cancelled == [["legacy-order"]]
    assert order_manager._tracker.updated[0][2] == "pmm2_cancel"
    assert bridge.get_mutation_log()[0]["controller"] == "pmm2_canary"

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from pmm2.allocator.scoring import AdjustedScorer
from pmm2.planner.quote_planner import QuotePlanner
from pmm2.scorer.bundles import QuoteBundle
from pmm2.shadow import CounterfactualEngine, ShadowLogger, V1StateSnapshot


def _make_market():
    return SimpleNamespace(
        condition_id="cid1",
        event_id="event1",
        token_id_yes="yes1",
        token_id_no="no1",
        question="Will test coverage improve?",
        best_bid=0.49,
        best_ask=0.51,
        mid=0.50,
        depth_at_best_bid=20.0,
        depth_at_best_ask=18.0,
        volume_24h=2400.0,
        liquidity=400.0,
        reward_eligible=True,
        reward_daily_rate=24.0,
        reward_min_size=5.0,
        reward_max_spread=10.0,
        fees_enabled=True,
        fee_rate=0.02,
    )


def _make_order(order_id: str, side: str, price: float, scoring: bool = False):
    return SimpleNamespace(
        order_id=order_id,
        token_id="yes1",
        condition_id="cid1",
        side=side,
        price_float=price,
        remaining_size_float=10.0,
        state=SimpleNamespace(value="LIVE"),
        is_scoring=scoring,
        age_seconds=120.0,
    )


def _v1_state(
    ev: float,
    reward_markets: int,
    reward_ev: float,
    cancel_rate_seed: float,
    *,
    fill_count_recent: int = 0,
    unique_fill_markets_recent: list[str] | None = None,
) -> dict:
    order_minutes = 10.0
    return {
        "markets": ["market1", "market2"],
        "market_evaluations": [
            {"condition_id": "market1", "best_bid": 0.49, "best_ask": 0.51},
            {"condition_id": "market2", "best_bid": 0.44, "best_ask": 0.56},
        ],
        "total_expected_ev_usdc": ev,
        "total_reward_ev_usdc": reward_ev,
        "reward_market_count": reward_markets,
        "ev_sample_valid": True,
        "cancel_count_recent": cancel_rate_seed,
        "live_order_minutes": order_minutes,
        "cycle_minutes": 1.0,
        "fill_count_recent": fill_count_recent,
        "unique_fill_markets_recent": unique_fill_markets_recent or [],
    }


def _pmm2_plan(ev: float, reward_markets: int, reward_ev: float, cancel_rate_seed: float) -> dict:
    order_minutes = 10.0
    return {
        "markets": ["market1", "market3"],
        "market_evaluations": [
            {"condition_id": "market1", "best_bid": 0.50, "best_ask": 0.52},
            {"condition_id": "market3", "best_bid": 0.48, "best_ask": 0.53},
        ],
        "total_expected_ev_usdc": ev,
        "total_reward_ev_usdc": reward_ev,
        "reward_market_count": reward_markets,
        "ev_sample_valid": True,
        "projected_cancel_count": cancel_rate_seed,
        "live_order_minutes": order_minutes,
        "cycle_minutes": 1.0,
    }


def test_v1_state_snapshot_captures_realistic_ev_state():
    market = _make_market()
    bid_order = _make_order("order-bid", "BUY", 0.49, scoring=True)
    ask_order = _make_order("order-ask", "SELL", 0.51, scoring=False)

    bot_state = SimpleNamespace(
        nav=100.0,
        reward_eligible={"cid1"},
        active_markets={"cid1": market},
        order_tracker=SimpleNamespace(
            get_active_orders=lambda token_id=None: [bid_order, ask_order]
        ),
        position_tracker=SimpleNamespace(get_active_positions=lambda: []),
    )
    queue_estimator = SimpleNamespace(
        states={
            "order-bid": SimpleNamespace(fill_prob_30s=0.35, est_ahead_mid=4.0, eta_sec=9.0),
            "order-ask": SimpleNamespace(fill_prob_30s=0.25, est_ahead_mid=6.0, eta_sec=14.0),
        }
    )

    snapshot = V1StateSnapshot.capture(
        bot_state,
        queue_estimator=queue_estimator,
        allocator_interval_sec=60.0,
    )

    assert snapshot["nav"] == 100.0
    assert snapshot["ev_sample_valid"] is True
    assert snapshot["reward_market_count"] == 1
    assert snapshot["scoring_count"] == 1
    assert snapshot["total_expected_ev_usdc"] > 0.0
    assert snapshot["orders"][0]["fill_prob_30s"] > 0.0


def test_counterfactual_engine_uses_recent_window_not_lifetime_average(tmp_path: Path):
    logger = ShadowLogger(db=None, log_dir=str(tmp_path))
    engine = CounterfactualEngine(logger)

    for _ in range(100):
        engine.compare_cycle(
            _v1_state(ev=0.02, reward_markets=1, reward_ev=0.003, cancel_rate_seed=4.0),
            _pmm2_plan(ev=0.05, reward_markets=2, reward_ev=0.007, cancel_rate_seed=1.0),
        )

    assert engine.get_gate_diagnostics()["diagnostic_ready"] is True
    assert engine.is_ready_for_live() is False

    for _ in range(100):
        engine.compare_cycle(
            _v1_state(ev=0.05, reward_markets=2, reward_ev=0.007, cancel_rate_seed=1.0),
            _pmm2_plan(ev=0.01, reward_markets=1, reward_ev=0.002, cancel_rate_seed=4.0),
        )

    summary = engine.get_summary()
    diagnostics = engine.get_gate_diagnostics()

    assert summary["ready_for_live"] is False
    assert "gate_ev_positive" in diagnostics["blocking_gates"]
    assert diagnostics["gates"]["gate_sample_size"]["pass"] is True


def test_counterfactual_promotion_gate_requires_10_day_window(tmp_path: Path):
    logger = ShadowLogger(db=None, log_dir=str(tmp_path))
    engine = CounterfactualEngine(logger)

    for _ in range(100):
        engine.compare_cycle(
            _v1_state(
                ev=0.02,
                reward_markets=1,
                reward_ev=0.003,
                cancel_rate_seed=4.0,
                fill_count_recent=60,
                unique_fill_markets_recent=["market1", "market2", "market3", "market4"],
            ),
            _pmm2_plan(ev=0.05, reward_markets=2, reward_ev=0.007, cancel_rate_seed=1.0),
        )

    assert engine.get_gate_diagnostics()["diagnostic_ready"] is True
    assert engine.get_promotion_diagnostics()["promotion_ready"] is False
    assert "gate_shadow_duration" in engine.get_promotion_diagnostics()["blocking_gates"]

    engine.first_cycle_ts = engine.last_cycle_ts - engine.PROMOTION_MIN_SHADOW_SEC

    assert engine.get_promotion_diagnostics()["promotion_ready"] is True
    assert engine.is_ready_for_live() is True


def test_counterfactual_promotion_gate_blocks_without_fill_samples_or_market_variety(
    tmp_path: Path,
):
    logger = ShadowLogger(db=None, log_dir=str(tmp_path))
    engine = CounterfactualEngine(logger)

    for _ in range(100):
        engine.compare_cycle(
            _v1_state(
                ev=0.02,
                reward_markets=1,
                reward_ev=0.003,
                cancel_rate_seed=4.0,
                fill_count_recent=0,
                unique_fill_markets_recent=["market1"],
            ),
            _pmm2_plan(ev=0.05, reward_markets=2, reward_ev=0.007, cancel_rate_seed=1.0),
        )

    engine.first_cycle_ts = engine.last_cycle_ts - engine.PROMOTION_MIN_SHADOW_SEC
    diagnostics = engine.get_promotion_diagnostics()

    assert diagnostics["promotion_ready"] is False
    assert "gate_fill_samples" in diagnostics["blocking_gates"]
    assert "gate_market_variety" in diagnostics["blocking_gates"]


def test_shadow_logger_persists_cycle_diagnostics_to_sqlite(tmp_path: Path):
    class MemoryDB:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:")
            self.conn.execute(
                """
                CREATE TABLE shadow_cycle (
                    ts TEXT,
                    cycle_num INTEGER,
                    ready_for_live INTEGER,
                    window_cycles INTEGER,
                    ev_sample_count INTEGER,
                    reward_sample_count INTEGER,
                    churn_sample_count INTEGER,
                    gate_ev_positive INTEGER,
                    gate_reward_capture INTEGER,
                    gate_churn INTEGER,
                    gate_sample_size INTEGER,
                    v1_market_count INTEGER,
                    pmm2_market_count INTEGER,
                    market_overlap_pct REAL,
                    overlap_quote_distance_bps REAL,
                    v1_total_ev_usdc REAL,
                    pmm2_total_ev_usdc REAL,
                    ev_delta_usdc REAL,
                    v1_reward_market_count INTEGER,
                    pmm2_reward_market_count INTEGER,
                    reward_market_delta REAL,
                    v1_reward_ev_usdc REAL,
                    pmm2_reward_ev_usdc REAL,
                    reward_ev_delta_usdc REAL,
                    v1_cancel_rate_per_order_min REAL,
                    pmm2_cancel_rate_per_order_min REAL,
                    churn_delta_per_order_min REAL,
                    gate_blockers_json TEXT,
                    gate_diagnostics_json TEXT,
                    comparison_json TEXT,
                    summary_json TEXT,
                    v1_state_json TEXT,
                    pmm2_plan_json TEXT
                )
                """
            )

        async def execute(self, sql, parameters=None):
            self.conn.execute(sql, parameters or ())
            self.conn.commit()

        async def fetch_one(self, sql, parameters=None):
            cursor = self.conn.execute(sql, parameters or ())
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))

    async def _run():
        db = MemoryDB()
        logger = ShadowLogger(db=db, log_dir=str(tmp_path / "logs"))

        cycle_data = {
            "timestamp": "2026-03-12T00:00:00+00:00",
            "v1_markets": ["market1"],
            "pmm2_markets": ["market1", "market2"],
            "v1_state": {"markets": ["market1"]},
            "pmm2_plan": {"markets": ["market1", "market2"]},
            "comparison": {
                "cycle_num": 7,
                "market_overlap_pct": 0.5,
                "overlap_quote_distance_bps": 12.0,
                "v1_total_ev_usdc": 0.02,
                "pmm2_total_ev_usdc": 0.03,
                "ev_delta_usdc": 0.01,
                "v1_reward_market_count": 1,
                "pmm2_reward_market_count": 2,
                "reward_market_delta": 1.0,
                "v1_reward_ev_usdc": 0.001,
                "pmm2_reward_ev_usdc": 0.003,
                "reward_ev_delta_usdc": 0.002,
                "v1_cancel_rate_per_order_min": 0.30,
                "pmm2_cancel_rate_per_order_min": 0.10,
                "churn_delta_per_order_min": 0.20,
                "summary": {"ready_for_live": True},
                "gate_diagnostics": {
                    "window_cycles": 100,
                    "ev_sample_count": 80,
                    "reward_sample_count": 80,
                    "churn_sample_count": 80,
                    "blocking_gates": [],
                    "gates": {
                        "gate_ev_positive": {"pass": True},
                        "gate_reward_capture": {"pass": True},
                        "gate_churn": {"pass": True},
                        "gate_sample_size": {"pass": True},
                    },
                },
            },
        }

        logger.log_allocation_cycle(cycle_data)
        await logger.persist_allocation_cycle(cycle_data)

        row = await db.fetch_one("SELECT * FROM shadow_cycle WHERE cycle_num = ?", (7,))
        return row

    row = asyncio.run(_run())

    assert row is not None
    assert row["ready_for_live"] == 1
    assert row["gate_sample_size"] == 1
    assert row["ev_delta_usdc"] == pytest.approx(0.01)


def test_adjusted_scorer_offsets_entry_churn_for_reward_driven_bundle():
    bundle = QuoteBundle(
        market_condition_id="cid1",
        bundle_type="B1",
        capital_usdc=10.0,
        marginal_return=0.01,
        liq_ev=0.05,
        rebate_ev=0.0,
        is_reward_eligible=True,
    )

    scorer = AdjustedScorer(churn_phi=0.0015)
    result = scorer.score(
        bundle=bundle,
        current_markets=set(),
        event_clusters={},
        active_events={},
    )

    assert result.reward_credit > 0.0
    assert result.churn_penalty == 0.0


def test_quote_planner_preserves_fill_probabilities_and_roles():
    bundle = QuoteBundle(
        market_condition_id="cid1",
        bundle_type="B1",
        capital_usdc=10.0,
        bid_price=0.49,
        bid_size=10.0,
        ask_price=0.51,
        ask_size=10.0,
        fill_prob_bid=0.2,
        fill_prob_ask=0.4,
    )

    planner = QuotePlanner()
    plan = planner.plan_market(
        [bundle],
        token_id_yes="yes1",
        token_id_no="no1",
        condition_id="cid1",
    )

    assert len(plan.ladder) == 2
    bid_rung = next(rung for rung in plan.ladder if rung.quote_role == "bid")
    ask_rung = next(rung for rung in plan.ladder if rung.quote_role == "ask")
    assert bid_rung.fill_prob_30s == pytest.approx(0.2)
    assert ask_rung.fill_prob_30s == pytest.approx(0.4)


def test_schema_includes_shadow_cycle_table():
    schema = Path("pmm1/storage/schema.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS shadow_cycle" in schema

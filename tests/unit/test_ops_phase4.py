"""Phase 4 ops alerting and health surface tests."""

from __future__ import annotations

import asyncio

from pmm1.notifications import AlertManager, AlertSeverity
from pmm1.ops import OpsMonitor, evaluate_runtime_health, load_runtime_status
from pmm1.settings import OpsConfig


class _FakeOrderTracker:
    total_active = 0
    total_tracked = 0


class _FakeFillEscalator:
    def get_status(self) -> dict:
        return {"enabled": True, "no_fill_secs": 500.0}


class _FakeKillSwitch:
    def get_status(self) -> dict:
        return {"is_triggered": False, "active_reasons": []}


class _FakeState:
    def __init__(self) -> None:
        self.mode = "QUOTING"
        self.active_markets = {"cid": object()}
        self.order_tracker = _FakeOrderTracker()
        self.fill_escalator = _FakeFillEscalator()
        self.kill_switch = _FakeKillSwitch()

    def eligible_markets(self) -> list[object]:
        return [object()]


class _FakeWS:
    def __init__(self, reconnect_count: int = 0) -> None:
        self._reconnect_count = reconnect_count

    def get_stats(self) -> dict:
        return {
            "is_connected": True,
            "reconnect_count": self._reconnect_count,
            "seconds_since_last_message": 1.0,
        }


class _FakeHeartbeat:
    def get_stats(self) -> dict:
        return {"is_healthy": True, "consecutive_failures": 0}


class _FakeReconciler:
    def get_stats(self) -> dict:
        return {
            "order_mismatch_streak": 0,
            "position_mismatch_streak": 0,
            "last_mismatch_details": "",
        }


class _FakeDrawdownState:
    class _Tier:
        value = "normal"

    tier = _Tier()
    drawdown_pct = 0.0
    daily_pnl = 0.0
    daily_high_watermark = 100.0
    absolute_drawdown_pct = 0.0
    session_peak_nav = 100.0
    absolute_kill_triggered = False


def test_alert_manager_cooldown_suppresses_duplicates():
    sent_messages: list[str] = []
    now = [1000.0]

    async def sender(message: str) -> None:
        sent_messages.append(message)

    manager = AlertManager(
        default_cooldown_s=60.0,
        sender=sender,
        clock=lambda: now[0],
    )

    assert asyncio.run(
        manager.notify(AlertSeverity.WARNING, "TEST", "first", dedupe_key="same")
    ) is True
    assert asyncio.run(
        manager.notify(AlertSeverity.WARNING, "TEST", "second", dedupe_key="same")
    ) is False
    now[0] += 61.0
    assert asyncio.run(
        manager.notify(AlertSeverity.WARNING, "TEST", "third", dedupe_key="same")
    ) is True
    assert len(sent_messages) == 2


def test_ops_monitor_marks_no_quotes_and_excessive_churn(tmp_path):
    sent_messages: list[str] = []
    now = [1000.0]
    status_path = tmp_path / "runtime_status.json"

    async def sender(message: str) -> None:
        sent_messages.append(message)

    config = OpsConfig(
        runtime_status_path=str(status_path),
        alert_cooldown_s=1,
        no_active_quotes_after_s=2,
        excessive_churn_window_s=10,
        excessive_churn_min_cancels=2,
        excessive_churn_ratio=2.0,
        reconnect_storm_threshold=10,
    )
    alert_manager = AlertManager(sender=sender, clock=lambda: now[0], default_cooldown_s=1)
    monitor = OpsMonitor(config, alert_manager=alert_manager, clock=lambda: now[0])

    kwargs = {
        "state": _FakeState(),
        "paper_mode": False,
        "nav": 100.0,
        "dd_state": _FakeDrawdownState(),
        "market_ws": _FakeWS(),
        "user_ws": _FakeWS(),
        "heartbeat": _FakeHeartbeat(),
        "reconciler": _FakeReconciler(),
        "markets_quoted": 0,
        "cycle_lifecycle_counts": {"submitted": 1, "canceled": 3},
        "cycle_duration_ms": 10.0,
        "pmm2_status": {
            "enabled": True,
            "controller": "pmm2_canary",
            "stage": "canary_5pct",
            "controlled_market_count": 1,
        },
    }

    asyncio.run(monitor.observe_cycle(**kwargs))
    now[0] += 3.0
    asyncio.run(monitor.observe_cycle(**kwargs))

    snapshot = load_runtime_status(status_path)
    assert snapshot is not None
    assert snapshot["ops"]["conditions"]["no_active_quotes"]["active"] is True
    assert snapshot["ops"]["conditions"]["excessive_churn"]["active"] is True
    assert snapshot["pmm2"]["stage"] == "canary_5pct"
    assert any("NO ACTIVE QUOTES" in message for message in sent_messages)
    assert any("EXCESSIVE CHURN" in message for message in sent_messages)


def test_evaluate_runtime_health_reports_stale_status_and_db_failure():
    config = OpsConfig(status_stale_after_s=30, recent_failure_window_s=300)
    report = evaluate_runtime_health(
        {
            "updated_at": 10.0,
            "kill_switch": {"is_triggered": False, "active_reasons": []},
            "drawdown": {"tier": "normal", "drawdown_pct": 0.0},
            "reconciler": {"order_mismatch_streak": 0, "position_mismatch_streak": 0},
            "ops": {
                "conditions": {
                    "no_active_quotes": {"active": False, "duration_s": 0.0},
                    "reconnect_storm": {"active": False, "count": 0},
                    "excessive_churn": {"active": False, "ratio": 0.0},
                },
                "last_db_write_failure": {
                    "timestamp": 80.0,
                    "message": "execute failed",
                },
                "last_fill_recorder_failure": None,
            },
        },
        config=config,
        now=100.0,
        service_active=False,
    )

    assert report["severity"] == AlertSeverity.CRITICAL.value
    codes = {issue["code"] for issue in report["issues"]}
    assert "service_down" in codes
    assert "db_write_failure" in codes


def test_evaluate_runtime_health_warns_when_resume_gate_invalid_in_quoting():
    config = OpsConfig(status_stale_after_s=30, recent_failure_window_s=300)
    report = evaluate_runtime_health(
        {
            "updated_at": 95.0,
            "mode": "QUOTING",
            "kill_switch": {"is_triggered": False, "active_reasons": []},
            "drawdown": {"tier": "normal", "drawdown_pct": 0.0},
            "reconciler": {"order_mismatch_streak": 0, "position_mismatch_streak": 0},
            "runtime_safety": {"resume_token_valid": False},
            "ops": {
                "conditions": {
                    "no_active_quotes": {"active": False, "duration_s": 0.0},
                    "reconnect_storm": {"active": False, "count": 0},
                    "excessive_churn": {"active": False, "ratio": 0.0},
                },
                "last_db_write_failure": None,
                "last_fill_recorder_failure": None,
            },
        },
        config=config,
        now=100.0,
        service_active=True,
    )

    codes = {issue["code"] for issue in report["issues"]}
    assert "resume_gate" in codes

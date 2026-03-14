"""Operational monitoring, health evaluation, and runtime status snapshots."""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import structlog

from pmm1.notifications import AlertManager, AlertSeverity
from pmm1.settings import OpsConfig

logger = structlog.get_logger(__name__)


_SEVERITY_ORDER = {
    AlertSeverity.INFO.value: 0,
    AlertSeverity.WARNING.value: 1,
    AlertSeverity.CRITICAL.value: 2,
}


class RuntimeStatusWriter:
    """Atomically writes the current runtime status to disk."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.path)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return cast(dict[str, Any], json.loads(self.path.read_text(encoding="utf-8")))


def load_runtime_status(path: str | Path) -> dict[str, Any] | None:
    """Load a runtime status snapshot from disk."""
    return RuntimeStatusWriter(path).load()


def _max_severity(left: str, right: str) -> str:
    if _SEVERITY_ORDER[right] > _SEVERITY_ORDER[left]:
        return right
    return left


def evaluate_runtime_health(
    snapshot: dict[str, Any] | None,
    *,
    config: OpsConfig,
    now: float | None = None,
    service_active: bool | None = None,
) -> dict[str, Any]:
    """Evaluate a runtime snapshot into warning/critical health signals."""
    current_time = time.time() if now is None else now
    report: dict[str, Any] = {
        "severity": AlertSeverity.INFO.value,
        "issues": [],
        "checked_at": current_time,
    }

    def add_issue(severity: AlertSeverity, code: str, message: str) -> None:
        report["severity"] = _max_severity(report["severity"], severity.value)
        report["issues"].append(
            {
                "severity": severity.value,
                "code": code,
                "message": message,
            }
        )

    if snapshot is None:
        add_issue(
            AlertSeverity.CRITICAL,
            "service_down",
            "runtime status file is missing",
        )
        return report

    updated_at = float(snapshot.get("updated_at") or 0.0)
    age_seconds = current_time - updated_at if updated_at else float("inf")
    report["age_seconds"] = age_seconds

    if service_active is False:
        add_issue(AlertSeverity.CRITICAL, "service_down", "systemd service is not active")

    if age_seconds > config.status_stale_after_s:
        add_issue(
            AlertSeverity.CRITICAL,
            "service_down",
            f"runtime status is stale ({age_seconds:.0f}s old)",
        )

    kill_switch = snapshot.get("kill_switch", {})
    if kill_switch.get("is_triggered"):
        reasons = ", ".join(kill_switch.get("active_reasons", [])) or "unknown"
        add_issue(AlertSeverity.CRITICAL, "kill_switch", f"kill switch active: {reasons}")

    drawdown = snapshot.get("drawdown", {})
    tier = drawdown.get("tier", "")
    if tier == "tier3_flatten_only":
        add_issue(AlertSeverity.CRITICAL, "drawdown", "drawdown tier is FLATTEN_ONLY")
    elif tier in {"tier1_pause_taker", "tier2_wider_smaller"}:
        dd_pct = float(drawdown.get("drawdown_pct", 0.0)) * 100.0
        add_issue(AlertSeverity.WARNING, "drawdown", f"drawdown tier {tier} ({dd_pct:.2f}%)")

    market_ws = snapshot.get("market_ws", {})
    if market_ws and not market_ws.get("is_connected", True):
        add_issue(AlertSeverity.WARNING, "market_ws", "market websocket is disconnected")

    user_ws = snapshot.get("user_ws", {})
    if user_ws and not user_ws.get("is_connected", True):
        add_issue(AlertSeverity.WARNING, "user_ws", "user websocket is disconnected")

    reconciler = snapshot.get("reconciler", {})
    order_streak = int(reconciler.get("order_mismatch_streak", 0))
    position_streak = int(reconciler.get("position_mismatch_streak", 0))
    if order_streak > 0 or position_streak > 0:
        details = reconciler.get("last_mismatch_details") or "recent mismatch detected"
        add_issue(
            AlertSeverity.WARNING,
            "reconciliation",
            f"reconciliation mismatch streaks "
            f"order={order_streak} position={position_streak}: {details}",
        )

    runtime_safety = snapshot.get("runtime_safety", {})
    if runtime_safety and not runtime_safety.get("resume_token_valid", True):
        if str(snapshot.get("mode", "") or "") == "QUOTING":
            add_issue(
                AlertSeverity.WARNING,
                "resume_gate",
                "resume token invalid while bot reports QUOTING",
            )

    ops = snapshot.get("ops", {})
    conditions = ops.get("conditions", {})
    if conditions.get("reconnect_storm", {}).get("active"):
        count = conditions["reconnect_storm"].get("count", 0)
        add_issue(
            AlertSeverity.WARNING,
            "reconnect_storm",
            f"websocket reconnect storm ({count} reconnects)",
        )
    if conditions.get("no_active_quotes", {}).get("active"):
        duration = float(conditions["no_active_quotes"].get("duration_s", 0.0))
        add_issue(
            AlertSeverity.WARNING,
            "no_active_quotes",
            f"no active quotes for {duration:.0f}s",
        )
    if conditions.get("excessive_churn", {}).get("active"):
        ratio = float(conditions["excessive_churn"].get("ratio", 0.0))
        add_issue(
            AlertSeverity.WARNING,
            "excessive_churn",
            f"quote churn ratio {ratio:.2f}",
        )

    last_db_failure = ops.get("last_db_write_failure") or {}
    if last_db_failure:
        db_failure_age = current_time - float(last_db_failure.get("timestamp", 0.0) or 0.0)
        if db_failure_age <= config.recent_failure_window_s:
            add_issue(
                AlertSeverity.CRITICAL,
                "db_write_failure",
                last_db_failure.get("message", "recent database write failure"),
            )

    last_fill_failure = ops.get("last_fill_recorder_failure") or {}
    if last_fill_failure:
        fill_failure_age = current_time - float(last_fill_failure.get("timestamp", 0.0) or 0.0)
        if fill_failure_age <= config.recent_failure_window_s:
            add_issue(
                AlertSeverity.WARNING,
                "fill_recorder_failure",
                last_fill_failure.get("message", "recent fill recorder failure"),
            )

    runtime_safety = snapshot.get("runtime_safety", {})
    if runtime_safety and not bool(runtime_safety.get("resume_token_valid", True)):
        add_issue(
            AlertSeverity.WARNING,
            "resume_gate",
            "quote resume blocked: "
            + (runtime_safety.get('resume_blocked_reason')
               or 'awaiting clean reconciliation'),
        )

    # LLM reasoner health (H10)
    llm_status = snapshot.get("llm_reasoner", {})
    if llm_status.get("enabled"):
        if llm_status.get("circuit_open"):
            add_issue(
                AlertSeverity.CRITICAL,
                "llm_circuit_open",
                "LLM reasoner circuit breaker is open",
            )
        if (
            llm_status.get("fresh_estimates", 0) == 0
            and age_seconds > 600
        ):
            add_issue(
                AlertSeverity.WARNING,
                "llm_no_estimates",
                "LLM reasoner has no fresh estimates",
            )
        if llm_status.get("cost_cap_hit"):
            add_issue(
                AlertSeverity.WARNING,
                "llm_cost_cap",
                f"LLM daily cost cap reached "
                f"(${llm_status.get('daily_cost_usd', 0):.2f})",
            )

    # Paper 1/2: absolute drawdown kill switch
    drawdown_abs = drawdown.get("absolute_drawdown_pct", 0.0)
    if drawdown.get("absolute_kill_triggered"):
        add_issue(
            AlertSeverity.CRITICAL,
            "absolute_drawdown",
            f"absolute drawdown kill triggered"
            f" ({float(drawdown_abs)*100:.1f}%)",
        )
    elif float(drawdown_abs) > 0.10:
        add_issue(
            AlertSeverity.WARNING,
            "absolute_drawdown",
            f"approaching absolute drawdown limit"
            f" ({float(drawdown_abs)*100:.1f}%)",
        )

    # Paper 2: edge validation
    edge_val = snapshot.get("edge_validation", {})
    if edge_val:
        sprt = edge_val.get("sprt_decision", "")
        if sprt == "no_edge" and edge_val.get("total_trades", 0) > 200:
            add_issue(
                AlertSeverity.WARNING,
                "edge_lost",
                f"SPRT shows no statistical edge after"
                f" {edge_val['total_trades']} trades",
            )
        sharpe = float(edge_val.get("rolling_sharpe", 0))
        if (
            edge_val.get("total_trades", 0) > 50
            and sharpe < 0.5
        ):
            add_issue(
                AlertSeverity.WARNING,
                "low_sharpe",
                f"rolling Sharpe {sharpe:.2f} below 0.5",
            )

    return report


class OpsMonitor:
    """Tracks operational state, emits alerts, and persists runtime health."""

    def __init__(
        self,
        config: OpsConfig,
        *,
        alert_manager: AlertManager | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._alert_manager = alert_manager
        self._clock = clock
        self._status_writer = RuntimeStatusWriter(config.runtime_status_path)
        self._started_at = clock()
        self._recent_reconnects: deque[float] = deque()
        self._recent_churn: deque[tuple[float, int, int]] = deque()
        self._last_reconnect_total = 0
        self._no_quotes_since: float | None = None
        self._db_write_failures = 0
        self._fill_recorder_failures = 0
        self._last_db_write_failure: dict[str, Any] | None = None
        self._last_fill_recorder_failure: dict[str, Any] | None = None

    async def _send_alert(
        self,
        severity: AlertSeverity,
        event_type: str,
        details: str,
        *,
        dedupe_key: str | None = None,
    ) -> None:
        if self._alert_manager is None:
            return
        await self._alert_manager.notify(
            severity,
            event_type,
            details,
            dedupe_key=dedupe_key,
            cooldown_s=self.config.alert_cooldown_s,
        )

    async def record_db_write_failure(
        self, operation: str, error: str, sql: str = "",
    ) -> None:
        """Record and alert on database write failures."""
        now = self._clock()
        message = f"{operation} failed: {error}"
        if sql:
            message = f"{message} [{sql[:120]}]"
        self._db_write_failures += 1
        self._last_db_write_failure = {
            "timestamp": now,
            "operation": operation,
            "message": message,
        }
        await self._send_alert(
            AlertSeverity.CRITICAL,
            "DB WRITE FAILURE",
            message,
            dedupe_key=f"db:{operation}",
        )

    async def record_fill_recorder_failure(
        self,
        *,
        stage: str,
        token_id: str,
        order_id: str = "",
        error: str,
        delay_sec: int | None = None,
    ) -> None:
        """Record and alert on fill recorder failures."""
        now = self._clock()
        suffix = f", delay={delay_sec}s" if delay_sec is not None else ""
        message = (
            f"{stage} failed for token={token_id[:16] or '?'} "
            f"order={order_id[:16] or '?'}: {error}{suffix}"
        )
        self._fill_recorder_failures += 1
        self._last_fill_recorder_failure = {
            "timestamp": now,
            "stage": stage,
            "message": message,
        }
        await self._send_alert(
            AlertSeverity.WARNING,
            "FILL RECORDER FAILURE",
            message,
            dedupe_key=f"fill_recorder:{stage}",
        )

    async def record_reconciliation_mismatch(
        self,
        *,
        kind: str,
        count: int,
        details: str,
        streak: int,
    ) -> None:
        """Alert on reconciliation mismatches before they trip the kill switch."""
        await self._send_alert(
            AlertSeverity.WARNING,
            "RECONCILIATION MISMATCH",
            f"{kind}: {details} (count={count}, streak={streak})",
            dedupe_key=f"reconciliation:{kind}",
        )

    def write_lifecycle_status(
        self,
        *,
        mode: str,
        paper_mode: bool,
        note: str = "",
        kill_switch: dict[str, Any] | None = None,
        config_context: dict[str, Any] | None = None,
        runtime_safety: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a minimal status snapshot during startup or shutdown."""
        snapshot = {
            "schema_version": 1,
            "updated_at": self._clock(),
            "started_at": self._started_at,
            "mode": mode,
            "paper_mode": paper_mode,
            "note": note,
            "config": dict(config_context or {}),
            "runtime_safety": dict(runtime_safety or {}),
            "kill_switch": kill_switch or {"is_triggered": False, "active_reasons": []},
            "ops": {
                "db_write_failures": self._db_write_failures,
                "fill_recorder_failures": self._fill_recorder_failures,
                "last_db_write_failure": self._last_db_write_failure,
                "last_fill_recorder_failure": self._last_fill_recorder_failure,
                "conditions": {
                    "no_active_quotes": {"active": False, "duration_s": 0.0},
                    "reconnect_storm": {"active": False, "count": 0},
                    "excessive_churn": {"active": False, "ratio": 0.0},
                },
            },
        }
        self._status_writer.write(snapshot)
        return snapshot

    async def observe_cycle(
        self,
        *,
        state: Any,
        paper_mode: bool,
        nav: float,
        dd_state: Any,
        market_ws: Any,
        user_ws: Any,
        heartbeat: Any,
        reconciler: Any,
        markets_quoted: int,
        cycle_lifecycle_counts: dict[str, int],
        cycle_duration_ms: float,
        pmm2_status: dict[str, Any] | None = None,
        config_context: dict[str, Any] | None = None,
        runtime_safety: dict[str, Any] | None = None,
        edge_tracker_summary: dict[str, Any] | None = None,
        kelly_state: dict[str, Any] | None = None,
        v3_state: dict[str, Any] | None = None,
        calibration_state: dict[str, Any] | None = None,
        pnl_attribution: dict[str, Any] | None = None,
        llm_reasoner_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update rolling operational state after a quote cycle."""
        now = self._clock()
        eligible_markets = len(state.eligible_markets())
        active_orders = state.order_tracker.total_active

        market_ws_stats = market_ws.get_stats() if market_ws else {}
        user_ws_stats = user_ws.get_stats() if user_ws else {}
        heartbeat_stats = heartbeat.get_stats() if heartbeat else {}
        reconciler_stats = reconciler.get_stats() if reconciler else {}

        reconnect_total = int(market_ws_stats.get("reconnect_count", 0)) + int(
            user_ws_stats.get("reconnect_count", 0)
        )
        new_reconnects = max(0, reconnect_total - self._last_reconnect_total)
        self._last_reconnect_total = reconnect_total
        for _ in range(new_reconnects):
            self._recent_reconnects.append(now)
        while (
            self._recent_reconnects
            and (now - self._recent_reconnects[0]) > self.config.reconnect_storm_window_s
        ):
            self._recent_reconnects.popleft()
        reconnect_storm_active = (
            len(self._recent_reconnects)
            >= self.config.reconnect_storm_threshold
        )
        if reconnect_storm_active:
            await self._send_alert(
                AlertSeverity.WARNING,
                "WS RECONNECT STORM",
                (
                    f"{len(self._recent_reconnects)} reconnects in the last "
                    f"{self.config.reconnect_storm_window_s}s"
                ),
                dedupe_key="reconnect_storm",
            )

        if (
            not paper_mode
            and state.mode == "QUOTING"
            and eligible_markets >= self.config.no_active_quotes_min_markets
            and active_orders == 0
            and markets_quoted == 0
        ):
            if self._no_quotes_since is None:
                self._no_quotes_since = now
        else:
            self._no_quotes_since = None

        no_active_quotes_duration = (
            now - self._no_quotes_since if self._no_quotes_since is not None else 0.0
        )
        no_active_quotes_active = (
            self._no_quotes_since is not None
            and no_active_quotes_duration >= self.config.no_active_quotes_after_s
        )
        if no_active_quotes_active:
            await self._send_alert(
                AlertSeverity.WARNING,
                "NO ACTIVE QUOTES",
                (
                    f"mode={state.mode}, eligible_markets={eligible_markets}, "
                    f"active_orders={active_orders}, duration={no_active_quotes_duration:.0f}s"
                ),
                dedupe_key="no_active_quotes",
            )

        canceled = int(cycle_lifecycle_counts.get("canceled", 0))
        submitted = int(cycle_lifecycle_counts.get("submitted", 0))
        self._recent_churn.append((now, canceled, submitted))
        while (
            self._recent_churn
            and (now - self._recent_churn[0][0]) > self.config.excessive_churn_window_s
        ):
            self._recent_churn.popleft()
        churn_cancels = sum(sample[1] for sample in self._recent_churn)
        churn_submits = sum(sample[2] for sample in self._recent_churn)
        churn_ratio = churn_cancels / max(1, churn_submits)
        excessive_churn_active = (
            churn_submits > 0
            and churn_cancels >= self.config.excessive_churn_min_cancels
            and churn_ratio >= self.config.excessive_churn_ratio
        )
        if excessive_churn_active:
            await self._send_alert(
                AlertSeverity.WARNING,
                "EXCESSIVE CHURN",
                (
                    f"cancelled={churn_cancels}, submitted={churn_submits}, "
                    f"ratio={churn_ratio:.2f} over {self.config.excessive_churn_window_s}s"
                ),
                dedupe_key="excessive_churn",
            )

        snapshot = {
            "schema_version": 1,
            "updated_at": now,
            "started_at": self._started_at,
            "mode": state.mode,
            "paper_mode": paper_mode,
            "config": dict(config_context or {}),
            "nav": nav,
            "cycle_duration_ms": cycle_duration_ms,
            "eligible_markets": eligible_markets,
            "active_markets": len(state.active_markets),
            "markets_quoted_last_cycle": markets_quoted,
            "order_tracker": {
                "active": active_orders,
                "tracked": state.order_tracker.total_tracked,
            },
            "drawdown": {
                "tier": dd_state.tier.value,
                "drawdown_pct": dd_state.drawdown_pct,
                "daily_pnl": dd_state.daily_pnl,
                "daily_high_watermark": dd_state.daily_high_watermark,
                "absolute_drawdown_pct": dd_state.absolute_drawdown_pct,
                "session_peak_nav": dd_state.session_peak_nav,
                "absolute_kill_triggered": (
                    dd_state.absolute_kill_triggered
                ),
            },
            "kill_switch": state.kill_switch.get_status(),
            "market_ws": market_ws_stats,
            "user_ws": user_ws_stats,
            "heartbeat": heartbeat_stats,
            "reconciler": reconciler_stats,
            "runtime_safety": dict(runtime_safety or {}),
            "fill_escalator": state.fill_escalator.get_status(),
            "ops": {
                "db_write_failures": self._db_write_failures,
                "fill_recorder_failures": self._fill_recorder_failures,
                "last_db_write_failure": self._last_db_write_failure,
                "last_fill_recorder_failure": self._last_fill_recorder_failure,
                "conditions": {
                    "no_active_quotes": {
                        "active": no_active_quotes_active,
                        "since": self._no_quotes_since,
                        "duration_s": no_active_quotes_duration,
                    },
                    "reconnect_storm": {
                        "active": reconnect_storm_active,
                        "count": len(self._recent_reconnects),
                        "window_s": self.config.reconnect_storm_window_s,
                    },
                    "excessive_churn": {
                        "active": excessive_churn_active,
                        "cancel_count": churn_cancels,
                        "submit_count": churn_submits,
                        "ratio": churn_ratio,
                        "window_s": self.config.excessive_churn_window_s,
                    },
                },
            },
        }
        # Paper 2 observability: edge validation + Kelly + V3 + calibration
        if edge_tracker_summary is not None:
            snapshot["edge_validation"] = edge_tracker_summary
        if kelly_state is not None:
            snapshot["kelly"] = kelly_state
        if v3_state is not None:
            snapshot["v3"] = v3_state
        if calibration_state is not None:
            snapshot["calibration"] = calibration_state
        if llm_reasoner_status is not None:
            snapshot["llm_reasoner"] = llm_reasoner_status
        if pnl_attribution is not None:
            snapshot["pnl_attribution"] = pnl_attribution
        if pmm2_status is not None:
            snapshot["pmm2"] = pmm2_status
        self._status_writer.write(snapshot)
        return snapshot

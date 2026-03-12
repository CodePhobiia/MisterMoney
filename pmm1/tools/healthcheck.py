"""One-command runtime health and status check for operators."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from pmm1.notifications import AlertSeverity, send_alert
from pmm1.ops import evaluate_runtime_health, load_runtime_status
from pmm1.settings import OpsConfig, load_settings


def _service_active(service_name: str) -> bool | None:
    """Return systemd service state when available."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", service_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0 and result.stdout.strip() == "active"


def _build_fallback_report(status_path: str) -> dict[str, Any]:
    return {
        "severity": AlertSeverity.CRITICAL.value,
        "issues": [
            {
                "severity": AlertSeverity.CRITICAL.value,
                "code": "service_down",
                "message": f"runtime status file missing: {status_path}",
            }
        ],
        "status_path": status_path,
    }


def _format_text(report: dict[str, Any], status_path: str) -> str:
    lines = [
        f"severity={report['severity']}",
        f"status_path={status_path}",
    ]
    age_seconds = report.get("age_seconds")
    if age_seconds is not None:
        lines.append(f"age_seconds={age_seconds:.1f}")

    for issue in report.get("issues", []):
        lines.append(
            f"{issue['severity'].upper()} {issue['code']}: {issue['message']}"
        )

    if not report.get("issues"):
        lines.append("INFO ok: runtime status healthy")

    return "\n".join(lines)


async def _maybe_notify(report: dict[str, Any]) -> None:
    severity = AlertSeverity(report["severity"])
    if severity == AlertSeverity.INFO:
        return
    details = "\n".join(
        issue["message"] for issue in report.get("issues", [])
    ) or "runtime health degraded"
    await send_alert("HEALTHCHECK", details, severity=severity)


async def amain() -> None:
    parser = argparse.ArgumentParser(description="PMM-1 runtime health check")
    parser.add_argument(
        "--config",
        default="config/default.yaml",
        help="Path to the bot config file (default: config/default.yaml)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full health report as JSON",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Send the report through Telegram when severity is warning or critical",
    )
    parser.add_argument(
        "--service",
        default="pmm1",
        help="systemd --user service name to check (default: pmm1)",
    )
    args = parser.parse_args()

    settings = load_settings(args.config, enforce_runtime_guards=False)
    ops_config: OpsConfig = settings.ops
    status_path = str(Path(ops_config.runtime_status_path))
    snapshot = load_runtime_status(status_path)

    if snapshot is None:
        report = _build_fallback_report(status_path)
    else:
        report = evaluate_runtime_health(
            snapshot,
            config=ops_config,
            now=time.time(),
            service_active=_service_active(args.service),
        )
        report["status_path"] = status_path

    if args.notify:
        await _maybe_notify(report)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_text(report, status_path))

    exit_code = {
        AlertSeverity.INFO.value: 0,
        AlertSeverity.WARNING.value: 1,
        AlertSeverity.CRITICAL.value: 2,
    }[report["severity"]]
    raise SystemExit(exit_code)


if __name__ == "__main__":
    asyncio.run(amain())


def main() -> None:
    asyncio.run(amain())

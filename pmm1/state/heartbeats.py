"""Heartbeat state tracking — REST heartbeat loop management from §12.

Rules:
- Send every 5s, track heartbeat_id
- Server expects within 10s (+5s buffer) or cancels all orders
- 2 consecutive failures → PAUSED state
"""

from __future__ import annotations

import asyncio
import time

import structlog

from pmm1.api.clob_private import ClobPrivateClient

logger = structlog.get_logger(__name__)


class HeartbeatState:
    """Tracks heartbeat health and manages the heartbeat loop."""

    def __init__(
        self,
        client: ClobPrivateClient,
        interval_s: float = 5.0,
        max_consecutive_failures: int = 2,
    ) -> None:
        self._client = client
        self._interval_s = interval_s
        self._max_failures = max_consecutive_failures

        self._last_heartbeat_id: str = ""
        self._last_success_ts: float = 0.0
        self._last_attempt_ts: float = 0.0
        self._consecutive_failures: int = 0
        self._total_sent: int = 0
        self._total_failed: int = 0
        self._is_running: bool = False
        self._task: asyncio.Task | None = None

    @property
    def last_heartbeat_id(self) -> str:
        return self._last_heartbeat_id

    @property
    def seconds_since_last_success(self) -> float:
        if self._last_success_ts == 0:
            return float("inf")
        return time.time() - self._last_success_ts

    @property
    def is_healthy(self) -> bool:
        """True if heartbeat is active and recent."""
        return (
            self._consecutive_failures < self._max_failures
            and self.seconds_since_last_success < 15.0  # 10s server + 5s grace
        )

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def should_pause(self) -> bool:
        """True if too many consecutive failures → should enter PAUSED state."""
        return self._consecutive_failures >= self._max_failures

    async def send_heartbeat(self) -> bool:
        """Send a single heartbeat. Returns True on success."""
        self._last_attempt_ts = time.time()
        try:
            resp = await self._client.send_heartbeat()
            self._last_heartbeat_id = resp.heartbeat_id
            self._last_success_ts = time.time()
            self._consecutive_failures = 0
            self._total_sent += 1
            logger.debug(
                "heartbeat_ok",
                heartbeat_id=resp.heartbeat_id,
                total=self._total_sent,
            )
            return True
        except Exception as e:
            self._consecutive_failures += 1
            self._total_failed += 1
            logger.error(
                "heartbeat_failed",
                error=str(e),
                consecutive_failures=self._consecutive_failures,
                total_failed=self._total_failed,
            )
            return False

    async def _heartbeat_loop(self) -> None:
        """Internal loop that sends heartbeats at the configured interval."""
        logger.info("heartbeat_loop_started", interval_s=self._interval_s)
        while self._is_running:
            success = await self.send_heartbeat()
            if not success and self.should_pause:
                logger.critical(
                    "heartbeat_critical_failure",
                    consecutive_failures=self._consecutive_failures,
                    msg="Should enter PAUSED state — server will cancel all orders",
                )
            await asyncio.sleep(self._interval_s)

    def start(self) -> asyncio.Task:
        """Start the heartbeat loop as an asyncio task."""
        if self._is_running:
            logger.warning("heartbeat_loop_already_running")
            if self._task:
                return self._task

        self._is_running = True
        self._task = asyncio.create_task(self._heartbeat_loop())
        self._task.add_done_callback(self._on_task_done)
        return self._task

    def _on_task_done(self, task: asyncio.Task) -> None:
        """Handle heartbeat task completion/failure."""
        self._is_running = False
        if task.cancelled():
            logger.info("heartbeat_loop_cancelled")
        elif task.exception():
            logger.error(
                "heartbeat_loop_crashed",
                error=str(task.exception()),
            )
        else:
            logger.info("heartbeat_loop_stopped")

    async def stop(self) -> None:
        """Stop the heartbeat loop."""
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("heartbeat_loop_stopped")

    def get_stats(self) -> dict:
        """Get heartbeat statistics."""
        return {
            "is_healthy": self.is_healthy,
            "is_running": self._is_running,
            "last_heartbeat_id": self._last_heartbeat_id,
            "seconds_since_last_success": self.seconds_since_last_success,
            "consecutive_failures": self._consecutive_failures,
            "total_sent": self._total_sent,
            "total_failed": self._total_failed,
            "should_pause": self.should_pause,
        }

"""Replay engine — replay from recorded data for backtesting."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

try:
    import polars as pl
except ImportError:
    pl = None


class ReplayEvent:
    """A single event from recorded data."""

    def __init__(
        self,
        event_type: str,
        timestamp: float,
        data: dict[str, Any],
    ) -> None:
        self.event_type = event_type
        self.timestamp = timestamp
        self.data = data

    def __repr__(self) -> str:
        return f"ReplayEvent({self.event_type}, ts={self.timestamp:.3f})"


class ReplaySource:
    """Reads recorded events from JSONL files in chronological order."""

    def __init__(self, recording_dir: str) -> None:
        self._dir = Path(recording_dir)
        self._events: list[ReplayEvent] = []
        self._loaded = False

    def load(
        self,
        event_types: list[str] | None = None,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> int:
        """Load events from JSONL files.

        Args:
            event_types: Filter by event types. None = all.
            start_ts: Start timestamp filter.
            end_ts: End timestamp filter.

        Returns:
            Number of events loaded.
        """
        self._events.clear()
        jsonl_dir = self._dir / "jsonl"

        if not jsonl_dir.exists():
            logger.warning("replay_no_jsonl_dir", dir=str(jsonl_dir))
            return 0

        # Collect all JSONL files
        all_files: list[Path] = []
        for event_dir in sorted(jsonl_dir.iterdir()):
            if not event_dir.is_dir():
                continue
            event_type = event_dir.name
            if event_types and event_type not in event_types:
                continue
            for date_dir in sorted(event_dir.iterdir()):
                if not date_dir.is_dir():
                    continue
                for jsonl_file in sorted(date_dir.glob("*.jsonl")):
                    all_files.append(jsonl_file)

        # Parse all events
        for f in all_files:
            event_type = f.parent.parent.name
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        ts = data.get("_ts", 0.0)

                        if start_ts and ts < start_ts:
                            continue
                        if end_ts and ts > end_ts:
                            continue

                        self._events.append(ReplayEvent(
                            event_type=data.get("_type", event_type),
                            timestamp=ts,
                            data=data,
                        ))
                    except json.JSONDecodeError:
                        continue

        # Sort by timestamp
        self._events.sort(key=lambda e: e.timestamp)
        self._loaded = True

        logger.info(
            "replay_loaded",
            events=len(self._events),
            files=len(all_files),
        )
        return len(self._events)

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def duration_seconds(self) -> float:
        if len(self._events) < 2:
            return 0.0
        return self._events[-1].timestamp - self._events[0].timestamp

    def get_events(self) -> list[ReplayEvent]:
        """Get all loaded events."""
        return self._events

    def iter_events(
        self,
        speed: float = 1.0,
    ) -> EventIterator:
        """Iterate events with optional speed multiplier.

        Args:
            speed: Playback speed. 1.0 = real-time, 0 = as-fast-as-possible.
        """
        return EventIterator(self._events, speed)


class EventIterator:
    """Iterator that replays events with optional timing."""

    def __init__(self, events: list[ReplayEvent], speed: float = 1.0) -> None:
        self._events = events
        self._speed = speed
        self._index = 0
        self._start_time: float | None = None
        self._event_start_ts: float | None = None

    def __iter__(self) -> EventIterator:
        return self

    def __next__(self) -> ReplayEvent:
        if self._index >= len(self._events):
            raise StopIteration

        event = self._events[self._index]

        if self._speed > 0 and self._index > 0:
            # Simulate timing
            if self._start_time is None:
                self._start_time = time.time()
                self._event_start_ts = event.timestamp
            else:
                elapsed_event = event.timestamp - (self._event_start_ts or event.timestamp)
                elapsed_real = time.time() - self._start_time
                wait = (elapsed_event / self._speed) - elapsed_real
                if wait > 0:
                    time.sleep(wait)

        self._index += 1
        return event

    @property
    def progress(self) -> float:
        """Progress as fraction [0, 1]."""
        if not self._events:
            return 1.0
        return self._index / len(self._events)


class ReplayEngine:
    """Drives a backtest by replaying recorded events through handlers."""

    def __init__(self, source: ReplaySource) -> None:
        self._source = source
        self._handlers: dict[str, list[Callable[[ReplayEvent], None]]] = {}

    def on(self, event_type: str, handler: Callable[[ReplayEvent], None]) -> None:
        """Register a handler for an event type."""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def run(self, speed: float = 0.0) -> dict[str, int]:
        """Run the replay.

        Args:
            speed: 0 = fast-forward, 1.0 = real-time.

        Returns:
            Dict of event counts by type.
        """
        counts: dict[str, int] = {}

        for event in self._source.iter_events(speed):
            event_type = event.event_type
            counts[event_type] = counts.get(event_type, 0) + 1

            handlers = self._handlers.get(event_type, [])
            for handler in handlers:
                handler(event)

            # Also fire wildcard handlers
            for handler in self._handlers.get("*", []):
                handler(event)

        logger.info("replay_complete", counts=counts)
        return counts

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pmm1.storage.spine import (
    ConfigSnapshotRecord,
    SpineEvent,
    build_config_snapshot,
    compute_config_hash,
    make_session_id,
    resolve_git_sha,
)


def test_compute_config_hash_is_stable_across_key_order_and_secret_changes():
    config_a = {
        "bot": {"paper_mode": True, "quote_cycle_ms": 250},
        "api": {"api_key": "key-a", "api_secret": "secret-a"},
    }
    config_b = {
        "api": {"api_secret": "secret-b", "api_key": "key-b"},
        "bot": {"quote_cycle_ms": 250, "paper_mode": True},
    }

    assert compute_config_hash(config_a) == compute_config_hash(config_b)


def test_build_config_snapshot_redacts_sensitive_values():
    snapshot = build_config_snapshot(
        {
            "wallet": {"private_key": "super-secret", "address": "0xabc"},
            "api": {"api_key": "key", "api_passphrase": "passphrase"},
            "bot": {"paper_mode": True},
        },
        git_sha="abc123",
        created_at=datetime(2026, 3, 13, 9, 0, tzinfo=UTC),
    )

    assert isinstance(snapshot, ConfigSnapshotRecord)
    assert snapshot.git_sha == "abc123"
    assert snapshot.config_json["wallet"]["private_key"] == "[REDACTED]"
    assert snapshot.config_json["api"]["api_key"] == "[REDACTED]"
    assert snapshot.config_json["api"]["api_passphrase"] == "[REDACTED]"
    assert snapshot.config_json["wallet"]["address"] == "0xabc"


def test_make_session_id_matches_recorder_timestamp_format():
    now = datetime(2026, 3, 13, 12, 34, 56, tzinfo=UTC)
    assert make_session_id(now) == "20260313_123456"


def test_resolve_git_sha_returns_unknown_when_git_is_unavailable(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("git not installed")

    monkeypatch.setattr("pmm1.storage.spine.subprocess.run", _raise)

    assert resolve_git_sha(Path("C:/Users/talme/MisterMoney")) == "unknown"


def test_spine_event_rejects_blank_required_strings():
    with pytest.raises(ValueError, match="non-empty"):
        SpineEvent(
            event_type="",
            ts_event=datetime(2026, 3, 13, 12, 0, tzinfo=UTC),
            controller="v1",
            strategy="mm",
            session_id="20260313_120000",
            git_sha="unknown",
            config_hash="abc",
            run_stage="shadow",
        )


def test_spine_event_generates_event_id_and_ingest_timestamp():
    event = SpineEvent(
        event_type="order_submit_requested",
        ts_event=datetime(2026, 3, 13, 12, 0, tzinfo=UTC),
        controller="v1",
        strategy="mm",
        session_id="20260313_120000",
        git_sha="unknown",
        config_hash="abc",
        run_stage="shadow",
    )

    assert event.event_id
    assert event.ts_ingest.tzinfo == UTC

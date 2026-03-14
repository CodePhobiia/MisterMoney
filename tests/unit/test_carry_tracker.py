"""Tests for InventoryCarryTracker — inventory carry PnL tracking."""

from __future__ import annotations

from dataclasses import dataclass

from pmm1.analytics.carry_tracker import InventoryCarryTracker


@dataclass
class FakePosition:
    """Minimal position mock with mark_to_market."""

    condition_id: str
    yes_size: float = 10.0
    no_size: float = 0.0
    cost_basis: float = 5.0

    def mark_to_market(self, yes_price: float, no_price: float) -> float:
        return self.yes_size * yes_price + self.no_size * no_price - self.cost_basis


def test_carry_tracker_basic():
    """Two snapshots with price move produce correct delta."""
    tracker = InventoryCarryTracker()
    pos = FakePosition(condition_id="mkt_1", yes_size=10.0, cost_basis=5.0)

    # First snapshot at mid=0.50
    def get_mid_50(cid: str) -> float | None:
        return 0.50

    delta1 = tracker.snapshot([pos], get_mid_50)
    # First snapshot: no previous, so delta=0
    assert delta1 == 0.0

    # Second snapshot at mid=0.55 (price moved up, good for YES holders)
    def get_mid_55(cid: str) -> float | None:
        return 0.55

    delta2 = tracker.snapshot([pos], get_mid_55)
    # MTM at 0.50: 10*0.50 + 0*0.50 - 5.0 = 0.0
    # MTM at 0.55: 10*0.55 + 0*0.45 - 5.0 = 0.5
    # Delta = 0.5 - 0.0 = 0.5
    assert abs(delta2 - 0.5) < 1e-9
    assert abs(tracker.total_carry - 0.5) < 1e-9


def test_carry_tracker_position_removed():
    """Position disappears between snapshots — cleaned up from previous MTM."""
    tracker = InventoryCarryTracker()
    pos = FakePosition(condition_id="mkt_1")

    def get_mid(cid: str) -> float | None:
        return 0.50

    tracker.snapshot([pos], get_mid)
    assert "mkt_1" in tracker._previous_mtm

    # Second snapshot with no positions
    tracker.snapshot([], get_mid)
    assert "mkt_1" not in tracker._previous_mtm


def test_carry_tracker_first_snapshot_zero():
    """First snapshot has 0 carry since there is no previous data."""
    tracker = InventoryCarryTracker()
    pos = FakePosition(condition_id="mkt_1")

    def get_mid(cid: str) -> float | None:
        return 0.60

    delta = tracker.snapshot([pos], get_mid)
    assert delta == 0.0
    assert tracker.total_carry == 0.0


def test_carry_tracker_reset_daily():
    """Accumulate carry, reset, verify total is 0."""
    tracker = InventoryCarryTracker()
    pos = FakePosition(condition_id="mkt_1", yes_size=10.0, cost_basis=5.0)

    def get_mid_50(cid: str) -> float | None:
        return 0.50

    def get_mid_60(cid: str) -> float | None:
        return 0.60

    tracker.snapshot([pos], get_mid_50)
    tracker.snapshot([pos], get_mid_60)

    # Should have accumulated some carry
    assert tracker.total_carry != 0.0
    accumulated = tracker.total_carry

    # Reset and verify
    returned = tracker.reset_daily()
    assert returned == accumulated
    assert tracker.total_carry == 0.0
    assert tracker.get_all_market_carry() == {}
    assert tracker._snapshot_count == 0

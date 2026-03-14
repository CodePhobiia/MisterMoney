"""Tests for CrossEventArbDetector — PM-08 cross-event arbitrage detection."""

from __future__ import annotations

import pytest

from pmm1.strategy.cross_event_arb import (
    CrossEventArbDetector,
)

# ── Temporal violation detection ──────────────────────────────────────────


class TestTemporalViolation:
    """PM-08: Detect temporal containment violations."""

    def test_temporal_violation_detected(self):
        """Earlier > Later + costs -> violation detected with correct profit."""
        detector = CrossEventArbDetector(min_profit_threshold=0.02)
        detector.register_temporal_pair("march_cid", "june_cid")

        # P("X by March") = 0.60, P("X by June") = 0.50
        # Violation: 0.60 > 0.50 + 0.02(cost) -> violation = 0.10, profit = 0.08
        violations = detector.detect_violations(
            market_prices={"march_cid": 0.60, "june_cid": 0.50},
            transaction_cost=0.02,
        )
        assert len(violations) == 1
        v = violations[0]
        assert v.market_a_id == "march_cid"
        assert v.market_b_id == "june_cid"
        assert v.constraint_type == "temporal_containment"
        assert v.violation_size == pytest.approx(0.10)
        assert v.estimated_profit == pytest.approx(0.08)

    def test_no_violation_within_costs(self):
        """Small diff within transaction costs -> no violation."""
        detector = CrossEventArbDetector(min_profit_threshold=0.02)
        detector.register_temporal_pair("march_cid", "june_cid")

        # P("X by March") = 0.52, P("X by June") = 0.50
        # Diff = 0.02, equal to transaction_cost -> profit = 0.00 < min_profit
        violations = detector.detect_violations(
            market_prices={"march_cid": 0.52, "june_cid": 0.50},
            transaction_cost=0.02,
        )
        assert len(violations) == 0

    def test_no_violation_correct_ordering(self):
        """Earlier < Later -> no violation (constraint satisfied)."""
        detector = CrossEventArbDetector(min_profit_threshold=0.02)
        detector.register_temporal_pair("march_cid", "june_cid")

        violations = detector.detect_violations(
            market_prices={"march_cid": 0.40, "june_cid": 0.60},
            transaction_cost=0.02,
        )
        assert len(violations) == 0

    def test_missing_price_skipped(self):
        """Missing price for one market -> pair is skipped."""
        detector = CrossEventArbDetector(min_profit_threshold=0.02)
        detector.register_temporal_pair("march_cid", "june_cid")

        violations = detector.detect_violations(
            market_prices={"march_cid": 0.60},
            transaction_cost=0.02,
        )
        assert len(violations) == 0


# ── Auto-detect temporal pairs ────────────────────────────────────────────


class TestFindTemporalPairs:
    """PM-08: Auto-detect temporal containment pairs from market questions."""

    def test_find_temporal_pairs(self):
        """Markets with date patterns -> pairs found."""
        markets = [
            {
                "condition_id": "cid_march",
                "question": "Will BTC reach $100k by March 15, 2025?",
            },
            {
                "condition_id": "cid_june",
                "question": "Will BTC reach $100k by June 30, 2025?",
            },
            {
                "condition_id": "cid_unrelated",
                "question": "Will it rain tomorrow?",
            },
        ]
        pairs = CrossEventArbDetector.find_temporal_pairs(markets)
        assert len(pairs) == 1
        assert ("cid_march", "cid_june") in pairs

    def test_no_date_patterns_no_pairs(self):
        """Markets without date patterns -> no pairs."""
        markets = [
            {"condition_id": "cid_a", "question": "Will team A win?"},
            {"condition_id": "cid_b", "question": "Will team B win?"},
        ]
        pairs = CrossEventArbDetector.find_temporal_pairs(markets)
        assert len(pairs) == 0

    def test_three_markets_same_base(self):
        """Three markets with same base question -> 3 pairs (combinatorial)."""
        markets = [
            {
                "condition_id": "cid_march",
                "question": "Will X happen by March 1?",
            },
            {
                "condition_id": "cid_june",
                "question": "Will X happen by June 1?",
            },
            {
                "condition_id": "cid_sept",
                "question": "Will X happen by September 1?",
            },
        ]
        pairs = CrossEventArbDetector.find_temporal_pairs(markets)
        # C(3,2) = 3 pairs
        assert len(pairs) == 3


# ── Status ────────────────────────────────────────────────────────────────


class TestGetStatus:
    def test_status_reflects_registered_pairs(self):
        detector = CrossEventArbDetector()
        assert detector.get_status()["registered_pairs"] == 0
        detector.register_temporal_pair("a", "b")
        assert detector.get_status()["registered_pairs"] == 1

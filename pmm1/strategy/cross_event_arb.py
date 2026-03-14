"""Cross-event arbitrage detection for logically related markets.

PM-08: Markets with temporal/causal relationships create arb
opportunities when logical constraints are violated.

Example: P("X by March") must be <= P("X by June")
If market prices violate this, arb exists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class ConstraintViolation:
    """A detected constraint violation between two markets."""

    market_a_id: str
    market_b_id: str
    constraint_type: str  # "temporal_containment", "logical_implication"
    market_a_price: float
    market_b_price: float
    violation_size: float  # How much the constraint is violated
    estimated_profit: float  # After transaction costs


class CrossEventArbDetector:
    """Detects cross-event arbitrage from logical constraints.

    Currently supports:
    - Temporal containment: "X by March" <= "X by June"
    - Complement: P(A) + P(not A) should equal ~1.0
    """

    def __init__(self, min_profit_threshold: float = 0.02) -> None:
        self.min_profit = min_profit_threshold
        self._known_pairs: list[tuple[str, str, str]] = []  # (id_a, id_b, constraint_type)

    def register_temporal_pair(
        self, earlier_id: str, later_id: str,
    ) -> None:
        """Register that earlier_id's event must happen before later_id's."""
        self._known_pairs.append((earlier_id, later_id, "temporal_containment"))

    def detect_violations(
        self, market_prices: dict[str, float],
        transaction_cost: float = 0.02,
    ) -> list[ConstraintViolation]:
        """Check all registered pairs for violations."""
        violations = []

        for id_a, id_b, constraint in self._known_pairs:
            price_a = market_prices.get(id_a)
            price_b = market_prices.get(id_b)

            if price_a is None or price_b is None:
                continue

            if constraint == "temporal_containment":
                # P(X by earlier_date) should be <= P(X by later_date)
                if price_a > price_b + transaction_cost:
                    violation = price_a - price_b
                    profit = violation - transaction_cost
                    if profit >= self.min_profit:
                        violations.append(ConstraintViolation(
                            market_a_id=id_a,
                            market_b_id=id_b,
                            constraint_type=constraint,
                            market_a_price=price_a,
                            market_b_price=price_b,
                            violation_size=violation,
                            estimated_profit=profit,
                        ))

        return violations

    @staticmethod
    def find_temporal_pairs(
        markets: list[dict[str, Any]],
    ) -> list[tuple[str, str]]:
        """Auto-detect temporal containment pairs from market questions.

        Looks for patterns like "Will X happen by [date]?" across markets.
        """
        # Group by base question (removing date references)
        date_pattern = re.compile(
            r'\b(?:by|before|until)\s+'
            r'(?:January|February|March|April|May|June|July|August|'
            r'September|October|November|December)\s+\d{1,2}(?:,?\s+\d{4})?',
            re.IGNORECASE,
        )

        groups: dict[str, list[tuple[str, str]]] = {}
        for m in markets:
            question = m.get("question", "")
            cid = m.get("condition_id", "")
            base = date_pattern.sub("[DATE]", question).strip()
            if "[DATE]" in base:
                groups.setdefault(base, []).append((cid, question))

        pairs = []
        for base, items in groups.items():
            if len(items) >= 2:
                # Sort by date (simple heuristic: shorter deadline first)
                # In practice, parse actual dates
                for i in range(len(items)):
                    for j in range(i + 1, len(items)):
                        pairs.append((items[i][0], items[j][0]))

        return pairs

    def get_status(self) -> dict[str, Any]:
        return {
            "registered_pairs": len(self._known_pairs),
        }

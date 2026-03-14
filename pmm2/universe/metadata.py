"""Market metadata enrichment — unified model with all scoring inputs.

EnrichedMarket consolidates book state, volume, rewards, fees, and risk metadata
into a single model for universe scoring.
"""

from __future__ import annotations

import re

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)


class EnrichedMarket(BaseModel):
    """Enriched market metadata with all fields needed for universe scoring.

    Combines:
    - Market identifiers (condition_id, token_ids, event_id)
    - Book state (bid/ask/mid/spread)
    - Volume and liquidity
    - Reward eligibility and parameters
    - Fee info
    - Risk metadata (time to resolution, ambiguity, placeholder outcomes)
    - Trading flags (accepting_orders, active)
    """

    # Identifiers
    condition_id: str
    question: str = ""
    token_id_yes: str = ""
    token_id_no: str = ""
    event_id: str = ""  # For event cluster grouping

    # Book state
    best_bid: float = 0.0
    best_ask: float = 0.0
    mid: float = 0.0
    spread_cents: float = 0.0

    # Volume & liquidity
    volume_24h: float = 0.0
    liquidity: float = 0.0

    # Book depth at best price levels (for queue estimation)
    depth_at_best_bid: float = 0.0
    depth_at_best_ask: float = 0.0

    # Reward info
    reward_eligible: bool = False
    reward_daily_rate: float = 0.0
    reward_min_size: float = 0.0
    reward_max_spread: float = 0.0

    # Fee info
    fees_enabled: bool = False
    fee_rate: float = 0.0

    # Risk metadata
    hours_to_resolution: float = 0.0
    is_neg_risk: bool = False
    has_placeholder_outcomes: bool = False
    ambiguity_score: float = 0.0  # Computed heuristic

    # Market config
    tick_size: str = "0.01"

    # Flags
    accepting_orders: bool = True
    active: bool = True

    model_config = {"frozen": False}


def compute_ambiguity_score(question: str, description: str = "") -> float:
    """Compute heuristic ambiguity score for a market.

    Higher score = more ambiguous = higher resolution risk.

    Heuristics:
    - Vague keywords ("approximately", "around", "roughly", "about") → +0.2 each
    - Long title (>100 chars) → +0.1
    - Contains "or" (multiple interpretations) → +0.1
    - Contains numbers/dates → -0.1 (more specific)
    - Clamped to [0, 1]

    Args:
        question: Market question/title.
        description: Optional market description.

    Returns:
        Ambiguity score in [0, 1].
    """
    combined = f"{question} {description}".lower()
    score = 0.0

    # Vague keywords
    vague_keywords = ["approximately", "around", "roughly", "about", "nearly", "almost"]
    for keyword in vague_keywords:
        if keyword in combined:
            score += 0.2

    # Long title suggests complexity
    if len(question) > 100:
        score += 0.1

    # "Or" suggests multiple interpretations
    if " or " in combined:
        score += 0.1

    # Numbers/dates suggest specificity (reduce ambiguity)
    if re.search(r"\d{4}", combined):  # Year pattern
        score -= 0.1
    if re.search(r"\$\d+", combined):  # Dollar amount
        score -= 0.1
    if re.search(r"\d+%", combined):  # Percentage
        score -= 0.1

    # Clamp to [0, 1]
    return max(0.0, min(1.0, score))


def detect_placeholder_outcomes(question: str) -> bool:
    """Detect if market has placeholder outcomes like "Yes" / "No".

    Markets with generic outcomes are harder to interpret and may have
    higher resolution risk.

    Args:
        question: Market question/title.

    Returns:
        True if placeholder outcomes detected.
    """
    question_lower = question.lower()

    # Check for generic yes/no pattern
    # Real markets usually have specific outcomes, not just "Yes" or "No"
    generic_patterns = [
        r"\byes\b.*\bno\b",
        r"\byes\s*/\s*no\b",
    ]

    for pattern in generic_patterns:
        if re.search(pattern, question_lower):
            return True

    return False

"""Market selection and quote sanity helpers for V1 live behavior."""

from __future__ import annotations

import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel, Field

from pmm1.settings import MarketFiltersConfig
from pmm1.state.books import OrderBook
from pmm1.strategy.features import FeatureVector
from pmm1.strategy.quote_engine import QuoteIntent
from pmm1.strategy.universe import MarketMetadata, ScoredMarket


def _spread_cents(spread: float) -> float:
    if spread <= 0:
        return 0.0
    return spread * 100.0 if spread < 1.0 else spread


def _hours_to_end(end_date: datetime | None, now: datetime | None = None) -> float:
    if end_date is None:
        return float("inf")

    current_time = now or datetime.now(timezone.utc)
    normalized_end = end_date if end_date.tzinfo else end_date.replace(tzinfo=timezone.utc)
    normalized_now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
    return max(0.0, (normalized_end - normalized_now).total_seconds() / 3600.0)


class MarketAuditEntry(BaseModel):
    """Structured audit result for one candidate market."""

    condition_id: str
    question: str = ""
    score: float = 0.0
    selected: bool = False
    reasons: list[str] = Field(default_factory=list)
    event_id: str = ""
    theme: str = "uncorrelated"
    spread_cents: float = 0.0
    liquidity: float = 0.0
    reward_eligible: bool = False
    reward_capture_ok: bool = True
    hours_to_end: float = float("inf")


class MarketAuditSummary(BaseModel):
    """Selected market set plus structured audit metadata."""

    selected: list[ScoredMarket] = Field(default_factory=list)
    entries: list[MarketAuditEntry] = Field(default_factory=list)
    reason_counts: dict[str, int] = Field(default_factory=dict)
    event_counts: dict[str, int] = Field(default_factory=dict)
    theme_counts: dict[str, int] = Field(default_factory=dict)
    reward_capture_selected: int = 0
    avg_selected_spread_cents: float = 0.0
    avg_selected_liquidity: float = 0.0


class LiveMarketAssessment(BaseModel):
    """Quote-time sanity check for a market with a live book."""

    tradable: bool = True
    reasons: list[str] = Field(default_factory=list)
    spread_cents: float = 0.0
    min_side_depth: float = 0.0
    reward_capture_ok: bool = True


class MarketTelemetryEvent(BaseModel):
    """Structured market rejection or suppression event."""

    kind: str
    stage: str
    reason: str
    condition_id: str
    token_id: str = ""
    side: str = "BOTH"
    question: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class MarketTelemetry:
    """Rate-limited structured market telemetry for operators."""

    def __init__(self, log_ttl_s: float = 60.0, max_keys: int = 4096) -> None:
        self._log_ttl_s = log_ttl_s
        self._max_keys = max_keys
        self._last_emitted: OrderedDict[str, float] = OrderedDict()
        self._reason_counts: dict[str, int] = defaultdict(int)

    def record(self, event: MarketTelemetryEvent, now: float | None = None) -> bool:
        """Record an event and return True when it should be emitted."""
        current_time = now or time.time()
        self._reason_counts[event.reason] += 1

        key = "|".join((
            event.kind,
            event.stage,
            event.condition_id,
            event.token_id,
            event.side,
            event.reason,
        ))
        last_emitted = self._last_emitted.get(key)
        self._last_emitted[key] = current_time
        self._last_emitted.move_to_end(key)
        while len(self._last_emitted) > self._max_keys:
            self._last_emitted.popitem(last=False)

        if last_emitted is None:
            return True
        return current_time - last_emitted >= self._log_ttl_s

    def consume_reason_counts(self) -> dict[str, int]:
        """Return and reset aggregated reason counts."""
        counts = dict(self._reason_counts)
        self._reason_counts = defaultdict(int)
        return counts


def audit_active_markets(
    ranked_markets: list[ScoredMarket],
    filters: MarketFiltersConfig,
    max_markets: int,
    classify_theme: Callable[[str, str], str] | None = None,
    now: datetime | None = None,
) -> MarketAuditSummary:
    """Select a diversified active market set from ranked candidates."""
    selected: list[ScoredMarket] = []
    entries: list[MarketAuditEntry] = []
    reason_counts: dict[str, int] = defaultdict(int)
    event_counts: dict[str, int] = defaultdict(int)
    theme_counts: dict[str, int] = defaultdict(int)

    for scored in ranked_markets:
        if len(selected) >= max_markets:
            break
        if not scored.eligible:
            continue

        md = scored.metadata
        question = md.question or md.slug or md.condition_id
        theme = classify_theme(md.condition_id, question) if classify_theme else "uncorrelated"
        spread_cents = _spread_cents(md.spread)
        hours_to_end = _hours_to_end(md.end_date, now)
        reward_capture_ok = True
        reasons: list[str] = []

        if md.reward_eligible and md.reward_max_spread > 0 and spread_cents > md.reward_max_spread:
            reward_capture_ok = False
            reasons.append("reward_spread_outside_scoring")

        if (
            md.event_id
            and filters.max_markets_per_event > 0
            and event_counts[md.event_id] >= filters.max_markets_per_event
        ):
            reasons.append("event_diversification_cap")

        if (
            theme != "uncorrelated"
            and filters.max_markets_per_theme > 0
            and theme_counts[theme] >= filters.max_markets_per_theme
        ):
            reasons.append("theme_diversification_cap")

        selected_flag = not reasons
        entries.append(MarketAuditEntry(
            condition_id=md.condition_id,
            question=question,
            score=scored.score,
            selected=selected_flag,
            reasons=list(reasons),
            event_id=md.event_id,
            theme=theme,
            spread_cents=spread_cents,
            liquidity=md.liquidity,
            reward_eligible=md.reward_eligible,
            reward_capture_ok=reward_capture_ok,
            hours_to_end=hours_to_end,
        ))

        if not selected_flag:
            for reason in reasons:
                reason_counts[reason] += 1
            continue

        md.universe_score = scored.score
        md.universe_rank = len(selected) + 1
        md.theme = theme
        selected.append(scored)

        if md.event_id:
            event_counts[md.event_id] += 1
        if theme != "uncorrelated":
            theme_counts[theme] += 1

    selected_spreads = [_spread_cents(item.metadata.spread) for item in selected]
    selected_liquidity = [item.metadata.liquidity for item in selected]

    return MarketAuditSummary(
        selected=selected,
        entries=entries,
        reason_counts=dict(reason_counts),
        event_counts=dict(event_counts),
        theme_counts=dict(theme_counts),
        reward_capture_selected=sum(
            1
            for item in selected
            if item.metadata.reward_eligible
            and (
                item.metadata.reward_max_spread <= 0
                or _spread_cents(item.metadata.spread) <= item.metadata.reward_max_spread
            )
        ),
        avg_selected_spread_cents=(
            sum(selected_spreads) / len(selected_spreads) if selected_spreads else 0.0
        ),
        avg_selected_liquidity=(
            sum(selected_liquidity) / len(selected_liquidity) if selected_liquidity else 0.0
        ),
    )


def compute_concentration_suppressions(
    markets: list[MarketMetadata],
    filters: MarketFiltersConfig,
    classify_theme: Callable[[str, str], str] | None = None,
) -> dict[str, list[str]]:
    """Return market-level concentration reasons for lower-ranked overflow markets."""
    suppressions: dict[str, list[str]] = defaultdict(list)
    event_counts: dict[str, int] = defaultdict(int)
    theme_counts: dict[str, int] = defaultdict(int)

    ranked_markets = sorted(
        markets,
        key=lambda market: (-market.universe_score, market.universe_rank, market.condition_id),
    )

    for md in ranked_markets:
        question = md.question or md.slug or md.condition_id
        theme = md.theme or (classify_theme(md.condition_id, question) if classify_theme else "uncorrelated")

        if (
            md.event_id
            and filters.max_markets_per_event > 0
            and event_counts[md.event_id] >= filters.max_markets_per_event
        ):
            suppressions[md.condition_id].append("event_diversification_cap")
        else:
            if md.event_id:
                event_counts[md.event_id] += 1

        if (
            theme != "uncorrelated"
            and filters.max_markets_per_theme > 0
            and theme_counts[theme] >= filters.max_markets_per_theme
        ):
            suppressions[md.condition_id].append("theme_diversification_cap")
        else:
            if theme != "uncorrelated":
                theme_counts[theme] += 1

    return {condition_id: reasons for condition_id, reasons in suppressions.items() if reasons}


def assess_live_market(
    market: MarketMetadata,
    book: OrderBook | None,
    features: FeatureVector,
    filters: MarketFiltersConfig,
) -> LiveMarketAssessment:
    """Check whether a live market is sane to quote right now."""
    reasons: list[str] = []

    if book is None:
        reasons.append("book_missing")
    elif book.get_best_bid() is None or book.get_best_ask() is None:
        reasons.append("book_one_sided")

    if features.is_stale:
        reasons.append("book_stale")

    spread_cents = max(0.0, features.spread_cents)
    reward_capture_ok = True
    if market.reward_eligible and market.reward_max_spread > 0 and spread_cents > market.reward_max_spread:
        reward_capture_ok = False
        reasons.append("reward_spread_outside_scoring")

    if spread_cents <= 0:
        reasons.append("book_one_sided")
    elif spread_cents > filters.max_top_spread_cents:
        reasons.append("live_spread_too_wide")

    min_side_depth = min(features.bid_depth_2c, features.ask_depth_2c)
    min_required_depth = filters.min_live_depth_within_2c_shares
    if market.reward_eligible and market.reward_min_size > 0:
        min_required_depth = max(min_required_depth, market.reward_min_size)
    if min_required_depth > 0 and min_side_depth < min_required_depth:
        reasons.append("live_depth_too_thin")

    return LiveMarketAssessment(
        tradable=len(reasons) == 0,
        reasons=list(dict.fromkeys(reasons)),
        spread_cents=spread_cents,
        min_side_depth=min_side_depth,
        reward_capture_ok=reward_capture_ok,
    )


def inventory_context_for_token(
    *,
    is_no_token: bool,
    market_inventory: float,
    cluster_inventory: float,
) -> tuple[float, float]:
    """Return inventory inputs in the quoted token's directional frame."""
    if is_no_token:
        return -market_inventory, -cluster_inventory
    return market_inventory, cluster_inventory


def apply_quote_book_guards(
    intent: QuoteIntent,
    book: OrderBook | None,
    tick_size: Decimal,
    escalation_ticks: int = 0,
) -> None:
    """Apply fill-speed adjustments plus top-of-book and crossing guards."""
    if book is None:
        return

    tick_float = float(tick_size)
    if escalation_ticks > 0:
        if intent.bid_price is not None:
            intent.bid_price = max(
                tick_float,
                min(1.0 - tick_float, intent.bid_price + escalation_ticks * tick_float),
            )
        if intent.ask_price is not None:
            intent.ask_price = max(
                tick_float,
                min(1.0 - tick_float, intent.ask_price - escalation_ticks * tick_float),
            )

    best_bid = book.get_best_bid()
    best_ask = book.get_best_ask()

    if best_bid and intent.bid_price is not None and intent.bid_price > best_bid.price_float:
        intent.bid_price = best_bid.price_float
    if best_ask and intent.ask_price is not None and intent.ask_price < best_ask.price_float:
        intent.ask_price = best_ask.price_float

    if best_bid and intent.ask_price is not None and intent.ask_price <= best_bid.price_float:
        intent.ask_price = best_bid.price_float + tick_float
    if best_ask and intent.bid_price is not None and intent.bid_price >= best_ask.price_float:
        intent.bid_price = best_ask.price_float - tick_float

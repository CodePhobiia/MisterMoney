"""Shared valuation helpers for PMM-2 shadow comparisons."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MarketShadowContext(BaseModel):
    """Market metadata needed to value a quoted market honestly."""

    condition_id: str
    event_id: str = ""
    token_id_yes: str = ""
    token_id_no: str = ""
    mid: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    depth_at_best_bid: float = 0.0
    depth_at_best_ask: float = 0.0
    volume_24h: float = 0.0
    liquidity: float = 0.0
    reward_eligible: bool = False
    reward_daily_rate: float = 0.0
    reward_min_size: float = 0.0
    reward_max_spread: float = 0.0
    fees_enabled: bool = False
    fee_rate: float = 0.0


class ShadowQuote(BaseModel):
    """Normalized quote in YES-price space for shadow valuation."""

    source_id: str = ""
    condition_id: str = ""
    token_id: str = ""
    quote_role: str = ""
    price: float = 0.0
    size: float = 0.0
    capital_usdc: float = 0.0
    fill_prob_30s: float = 0.0
    is_scoring: bool = False
    raw_side: str = ""
    bundle_type: str = ""


class MarketQuoteEvaluation(BaseModel):
    """Economic summary for one market's active or planned quotes."""

    condition_id: str
    event_id: str = ""
    quote_count: int = 0
    bid_quotes: int = 0
    ask_quotes: int = 0
    total_capital_usdc: float = 0.0
    market_mid: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread_ev_usdc: float = 0.0
    reward_ev_usdc: float = 0.0
    rebate_ev_usdc: float = 0.0
    arb_ev_usdc: float = 0.0
    total_ev_usdc: float = 0.0
    reward_bid_mass: float = 0.0
    reward_ask_mass: float = 0.0
    reward_pair_mass: float = 0.0
    reward_share: float = 0.0
    scoring_quote_count: int = 0
    scoring_quote_fraction: float = 0.0
    structurally_two_sided: bool = False
    reward_eligible: bool = False
    valid_for_ev: bool = False
    invalid_reasons: list[str] = Field(default_factory=list)


def _truthy_number(value: float) -> bool:
    return abs(float(value or 0.0)) > 1e-12


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def market_context_from_object(
    market: Any,
    *,
    reward_eligible_override: bool | None = None,
) -> MarketShadowContext:
    """Build a shadow context from either PMM-1 or PMM-2 market metadata."""

    if isinstance(market, MarketShadowContext):
        context = market
    else:
        if hasattr(market, "model_dump"):
            data = market.model_dump()
        elif isinstance(market, dict):
            data = dict(market)
        else:
            fields = (
                "condition_id",
                "event_id",
                "token_id_yes",
                "token_id_no",
                "mid",
                "best_bid",
                "best_ask",
                "depth_at_best_bid",
                "depth_at_best_ask",
                "volume_24h",
                "volume_24hr",
                "liquidity",
                "reward_eligible",
                "reward_daily_rate",
                "daily_reward_rate",
                "reward_min_size",
                "min_size",
                "reward_max_spread",
                "max_spread",
                "fees_enabled",
                "fee_rate",
            )
            data = {field: getattr(market, field) for field in fields if hasattr(market, field)}

        best_bid = _safe_float(data.get("best_bid"))
        best_ask = _safe_float(data.get("best_ask"))
        mid = _safe_float(data.get("mid"))
        if mid <= 0.0 and best_bid > 0.0 and best_ask > 0.0:
            mid = (best_bid + best_ask) / 2.0

        context = MarketShadowContext(
            condition_id=str(data.get("condition_id", "")),
            event_id=str(data.get("event_id", "")),
            token_id_yes=str(data.get("token_id_yes", "")),
            token_id_no=str(data.get("token_id_no", "")),
            mid=mid,
            best_bid=best_bid,
            best_ask=best_ask,
            depth_at_best_bid=_safe_float(data.get("depth_at_best_bid")),
            depth_at_best_ask=_safe_float(data.get("depth_at_best_ask")),
            volume_24h=_safe_float(data.get("volume_24h") or data.get("volume_24hr")),
            liquidity=_safe_float(data.get("liquidity")),
            reward_eligible=bool(data.get("reward_eligible", False)),
            reward_daily_rate=_safe_float(data.get("reward_daily_rate") or data.get("daily_reward_rate")),
            reward_min_size=_safe_float(data.get("reward_min_size") or data.get("min_size")),
            reward_max_spread=_safe_float(data.get("reward_max_spread") or data.get("max_spread")),
            fees_enabled=bool(data.get("fees_enabled", False)),
            fee_rate=_safe_float(data.get("fee_rate")),
        )

    if reward_eligible_override is not None:
        context.reward_eligible = bool(reward_eligible_override)

    return context


def merge_market_contexts(
    existing: MarketShadowContext | None,
    incoming: MarketShadowContext | None,
) -> MarketShadowContext | None:
    """Merge two market contexts, preferring already-populated truthy values."""

    if existing is None:
        return incoming
    if incoming is None:
        return existing

    merged = existing.model_copy(deep=True)
    incoming_data = incoming.model_dump()

    for field_name, incoming_value in incoming_data.items():
        existing_value = getattr(merged, field_name)
        if isinstance(existing_value, bool):
            setattr(merged, field_name, bool(existing_value or incoming_value))
            continue
        if isinstance(existing_value, str):
            if not existing_value and incoming_value:
                setattr(merged, field_name, incoming_value)
            continue
        if not _truthy_number(existing_value) and _truthy_number(incoming_value):
            setattr(merged, field_name, incoming_value)

    return merged


def shadow_quote_from_order(
    order: Any,
    context: MarketShadowContext | None,
    *,
    fill_prob_30s: float = 0.0,
) -> ShadowQuote | None:
    """Normalize a PMM-1 live order into YES-price-space."""

    side = str(getattr(order, "side", "") or "").upper()
    token_id = str(getattr(order, "token_id", "") or "")
    raw_price = _safe_float(
        getattr(order, "price_float", None),
        _safe_float(getattr(order, "price", None)),
    )
    size = _safe_float(
        getattr(order, "remaining_size_float", None),
        _safe_float(getattr(order, "remaining_size", None)),
    )
    if size <= 0.0:
        size = _safe_float(
            getattr(order, "size", None),
            _safe_float(getattr(order, "original_size_float", None)),
        )

    if side not in {"BUY", "SELL"} or raw_price <= 0.0 or raw_price >= 1.0 or size <= 0.0:
        return None

    quote_role = "bid" if side == "BUY" else "ask"
    normalized_price = raw_price

    if context and token_id and token_id == context.token_id_no:
        normalized_price = 1.0 - raw_price
        quote_role = "ask" if side == "BUY" else "bid"

    capital_usdc = (
        size * normalized_price
        if quote_role == "bid"
        else size * max(0.0, 1.0 - normalized_price)
    )

    return ShadowQuote(
        source_id=str(getattr(order, "order_id", "") or ""),
        condition_id=str(getattr(order, "condition_id", "") or ""),
        token_id=token_id,
        quote_role=quote_role,
        price=normalized_price,
        size=size,
        capital_usdc=capital_usdc,
        fill_prob_30s=max(0.0, min(1.0, _safe_float(fill_prob_30s))),
        is_scoring=bool(getattr(order, "is_scoring", False)),
        raw_side=side,
    )


def shadow_quote_from_rung(rung: Any) -> ShadowQuote | None:
    """Normalize a PMM-2 target rung into the same quote representation."""

    price = _safe_float(getattr(rung, "price", None))
    size = _safe_float(getattr(rung, "size", None))
    quote_role = str(getattr(rung, "quote_role", "") or "")
    side = str(getattr(rung, "side", "") or "").upper()

    if not quote_role:
        quote_role = "bid" if side == "BUY" else "ask"

    if quote_role not in {"bid", "ask"} or price <= 0.0 or price >= 1.0 or size <= 0.0:
        return None

    capital_usdc = _safe_float(getattr(rung, "capital_usdc", None))
    if capital_usdc <= 0.0:
        capital_usdc = size * price if quote_role == "bid" else size * max(0.0, 1.0 - price)

    return ShadowQuote(
        source_id=str(getattr(rung, "token_id", "") or ""),
        condition_id=str(getattr(rung, "condition_id", "") or ""),
        token_id=str(getattr(rung, "token_id", "") or ""),
        quote_role=quote_role,
        price=price,
        size=size,
        capital_usdc=capital_usdc,
        fill_prob_30s=max(0.0, min(1.0, _safe_float(getattr(rung, "fill_prob_30s", None)))),
        is_scoring=bool(getattr(rung, "is_scoring", False)),
        raw_side=side,
        bundle_type=str(getattr(rung, "bundle_type", "") or ""),
    )


def _reward_distance_score(price: float, mid: float, max_spread: float) -> float:
    if max_spread <= 0.0:
        return 0.0
    distance = abs(price - mid)
    if distance > max_spread:
        return 0.0
    return max(0.0, (1.0 - distance / max_spread) ** 2)


def _combine_reward_mass(q_bid: float, q_ask: float, mid: float) -> float:
    c = 3.0
    if 0.10 <= mid <= 0.90:
        return max(min(q_bid, q_ask), max(q_bid / c, q_ask / c))
    return min(q_bid, q_ask)


def evaluate_quote_set(
    context: MarketShadowContext | None,
    quotes: list[ShadowQuote],
) -> MarketQuoteEvaluation:
    """Value a market's quotes using real quote state instead of coarse proxies."""

    condition_id = context.condition_id if context else (
        quotes[0].condition_id if quotes else ""
    )
    evaluation = MarketQuoteEvaluation(
        condition_id=condition_id,
        event_id=context.event_id if context else "",
    )

    if not quotes:
        evaluation.valid_for_ev = True
        return evaluation

    valid_quotes = [q for q in quotes if q.quote_role in {"bid", "ask"} and 0.0 < q.price < 1.0 and q.size > 0.0]
    if len(valid_quotes) != len(quotes):
        evaluation.invalid_reasons.append("dropped_invalid_quotes")

    evaluation.quote_count = len(valid_quotes)
    evaluation.bid_quotes = sum(1 for q in valid_quotes if q.quote_role == "bid")
    evaluation.ask_quotes = sum(1 for q in valid_quotes if q.quote_role == "ask")
    evaluation.scoring_quote_count = sum(1 for q in valid_quotes if q.is_scoring)
    evaluation.scoring_quote_fraction = (
        evaluation.scoring_quote_count / evaluation.quote_count
        if evaluation.quote_count
        else 0.0
    )
    evaluation.total_capital_usdc = sum(
        q.capital_usdc if q.capital_usdc > 0.0 else (
            q.size * q.price if q.quote_role == "bid" else q.size * max(0.0, 1.0 - q.price)
        )
        for q in valid_quotes
    )

    if not context:
        evaluation.invalid_reasons.append("missing_market_context")
        return evaluation

    mid = context.mid
    if mid <= 0.0 and context.best_bid > 0.0 and context.best_ask > 0.0:
        mid = (context.best_bid + context.best_ask) / 2.0
    evaluation.market_mid = mid
    evaluation.reward_eligible = bool(context.reward_eligible)
    evaluation.structurally_two_sided = evaluation.bid_quotes > 0 and evaluation.ask_quotes > 0

    if mid <= 0.0 or mid >= 1.0:
        evaluation.invalid_reasons.append("missing_valid_mid")
        return evaluation

    bid_prices = [q.price for q in valid_quotes if q.quote_role == "bid"]
    ask_prices = [q.price for q in valid_quotes if q.quote_role == "ask"]
    evaluation.best_bid = max(bid_prices) if bid_prices else 0.0
    evaluation.best_ask = min(ask_prices) if ask_prices else 0.0

    reward_max_spread = (
        context.reward_max_spread / 100.0
        if context.reward_max_spread > 0.0
        else 0.05
    )
    reward_bid_mass = 0.0
    reward_ask_mass = 0.0
    spread_ev = 0.0
    fee_eq_total = 0.0
    rebate_per_fill = 0.0

    for quote in valid_quotes:
        edge = mid - quote.price if quote.quote_role == "bid" else quote.price - mid
        spread_ev += max(0.0, edge) * quote.size * max(0.0, min(1.0, quote.fill_prob_30s))

        reward_mass = _reward_distance_score(quote.price, mid, reward_max_spread) * quote.size
        if quote.quote_role == "bid":
            reward_bid_mass += reward_mass
        else:
            reward_ask_mass += reward_mass

        if context.fees_enabled and context.fee_rate > 0.0:
            variance = quote.price * (1.0 - quote.price)
            fee_eq = quote.size * quote.price * context.fee_rate * variance
            fee_eq_total += fee_eq
            rebate_per_fill += quote.size * quote.price * context.fee_rate

    reward_pair_mass = _combine_reward_mass(reward_bid_mass, reward_ask_mass, mid)
    q_others = max(
        context.depth_at_best_bid + context.depth_at_best_ask,
        context.reward_min_size * 2.0,
        10.0,
    )
    reward_share = (
        reward_pair_mass / (reward_pair_mass + q_others)
        if reward_pair_mass > 0.0 and q_others >= 0.0
        else 0.0
    )
    reward_ev = 0.0
    if context.reward_eligible and context.reward_daily_rate > 0.0:
        reward_ev = context.reward_daily_rate * reward_share * (1.0 / 24.0)

    rebate_ev = 0.0
    if context.fees_enabled and context.fee_rate > 0.0 and fee_eq_total > 0.0:
        visible_depth = max(
            context.depth_at_best_bid + context.depth_at_best_ask,
            sum(q.size for q in valid_quotes),
            10.0,
        )
        market_fee_eq = visible_depth * max(mid, 0.01) * context.fee_rate
        rebate_share = fee_eq_total / (fee_eq_total + market_fee_eq)
        fills_per_hour = max(context.volume_24h / 24.0 / max(visible_depth, 25.0), 0.1)
        rebate_ev = fills_per_hour * (rebate_per_fill / max(len(valid_quotes), 1)) * rebate_share

    evaluation.spread_ev_usdc = spread_ev
    evaluation.reward_bid_mass = reward_bid_mass
    evaluation.reward_ask_mass = reward_ask_mass
    evaluation.reward_pair_mass = reward_pair_mass
    evaluation.reward_share = reward_share
    evaluation.reward_ev_usdc = reward_ev
    evaluation.rebate_ev_usdc = rebate_ev
    evaluation.total_ev_usdc = spread_ev + reward_ev + rebate_ev + evaluation.arb_ev_usdc
    evaluation.valid_for_ev = True
    return evaluation


def aggregate_market_evaluations(
    evaluations: list[MarketQuoteEvaluation],
) -> dict[str, Any]:
    """Aggregate market-level valuations into cycle-level shadow metrics."""

    invalid_markets = [
        {
            "condition_id": evaluation.condition_id,
            "reasons": list(evaluation.invalid_reasons),
        }
        for evaluation in evaluations
        if evaluation.invalid_reasons and not evaluation.valid_for_ev
    ]
    valid_evals = [evaluation for evaluation in evaluations if evaluation.valid_for_ev]

    return {
        "quote_market_count": len(evaluations),
        "valid_market_count": len(valid_evals),
        "ev_sample_valid": len(invalid_markets) == 0,
        "invalid_markets": invalid_markets,
        "total_capital_usdc": sum(evaluation.total_capital_usdc for evaluation in evaluations),
        "total_ev_usdc": sum(evaluation.total_ev_usdc for evaluation in valid_evals),
        "total_spread_ev_usdc": sum(evaluation.spread_ev_usdc for evaluation in valid_evals),
        "total_reward_ev_usdc": sum(evaluation.reward_ev_usdc for evaluation in valid_evals),
        "total_rebate_ev_usdc": sum(evaluation.rebate_ev_usdc for evaluation in valid_evals),
        "reward_market_count": sum(1 for evaluation in evaluations if evaluation.reward_ev_usdc > 0.0),
        "scoring_market_count": sum(1 for evaluation in evaluations if evaluation.scoring_quote_count > 0),
        "reward_pair_mass_total": sum(evaluation.reward_pair_mass for evaluation in evaluations),
    }

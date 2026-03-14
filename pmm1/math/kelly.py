"""Kelly criterion for binary prediction markets.

From Research Paper 2, §1:
    f*_YES = (p - p_m) / (1 - p_m)
    f*_NO  = (p_m - p) / p_m
    g*     = D_KL(p || p_m)  (optimal growth rate)

Fractional Kelly at λ:
    f = λ * f*
    g(λ) ≈ g* * λ(2 - λ)
    Equivalent to full Kelly with blended belief:
        p_adj = λ * p + (1 - λ) * p_m
"""

from __future__ import annotations

import math


def kelly_fraction_yes(p_true: float, p_market: float) -> float:
    """Kelly fraction for a YES bet.

    Args:
        p_true: Believed true probability of YES outcome.
        p_market: Current market price for YES contract.

    Returns:
        Optimal fraction of bankroll to bet (0 if no edge).
    """
    if p_market >= 1.0 or p_market <= 0.0:
        return 0.0
    f = (p_true - p_market) / (1.0 - p_market)
    return max(0.0, f)


def kelly_fraction_no(p_true: float, p_market: float) -> float:
    """Kelly fraction for a NO bet.

    Args:
        p_true: Believed true probability of YES outcome.
        p_market: Current market price for YES contract.

    Returns:
        Optimal fraction of bankroll to bet on NO (0 if no edge).
    """
    if p_market <= 0.0 or p_market >= 1.0:
        return 0.0
    f = (p_market - p_true) / p_market
    return max(0.0, f)


def kelly_fraction_auto(
    p_true: float, p_market: float,
) -> tuple[str, float]:
    """Determine side and Kelly fraction automatically.

    Returns:
        (side, fraction) where side is "YES", "NO", or "NO_TRADE".
    """
    if abs(p_true - p_market) < 1e-9:
        return ("NO_TRADE", 0.0)

    if p_true > p_market:
        return ("YES", kelly_fraction_yes(p_true, p_market))
    else:
        return ("NO", kelly_fraction_no(p_true, p_market))


def fractional_kelly(
    p_true: float,
    p_market: float,
    lambda_frac: float = 0.25,
) -> tuple[str, float]:
    """Fractional Kelly sizing.

    Half-Kelly (λ=0.5) captures 75% of growth with 25% variance.
    Quarter-Kelly (λ=0.25) captures 43.75% of growth with 6.25% variance.

    Args:
        p_true: Believed true probability.
        p_market: Market price.
        lambda_frac: Kelly fraction (0.25 = quarter-Kelly).

    Returns:
        (side, sized_fraction) with λ applied.
    """
    side, f_star = kelly_fraction_auto(p_true, p_market)
    return (side, lambda_frac * f_star)


def kelly_growth_rate(p_true: float, p_market: float) -> float:
    """Optimal Kelly growth rate = KL divergence D_KL(p || p_m).

    This is the expected log-wealth growth per trade at full Kelly.
    For small edges δ near even odds: g* ≈ 2δ².

    Args:
        p_true: True probability.
        p_market: Market probability.

    Returns:
        Growth rate in nats per trade.
    """
    p = max(1e-10, min(1.0 - 1e-10, p_true))
    q = max(1e-10, min(1.0 - 1e-10, p_market))

    return p * math.log(p / q) + (1.0 - p) * math.log((1.0 - p) / (1.0 - q))


def fractional_kelly_growth_rate(
    p_true: float, p_market: float, lambda_frac: float,
) -> float:
    """Growth rate at fractional Kelly: g(λ) ≈ g* × λ(2 - λ).

    This is the approximate growth rate, exact for small edges.
    """
    g_star = kelly_growth_rate(p_true, p_market)
    return g_star * lambda_frac * (2.0 - lambda_frac)


def kelly_bet_dollars(
    p_true: float,
    p_market: float,
    nav: float,
    lambda_frac: float = 0.25,
    adverse_selection_lambda: float = 1.0,
    adverse_selection_cost: float = 0.0,
    min_edge: float = 0.03,
    max_position_nav: float = 0.05,
) -> tuple[str, float]:
    """Full Kelly sizing pipeline: probability → dollar amount.

    Applies adverse selection discount, minimum edge filter,
    fractional Kelly, and position cap.

    Adverse selection can be applied two ways:
    - adverse_selection_cost: absolute cost in probability units
      (subtracted from edge — preferred, per quant audit)
    - adverse_selection_lambda: multiplicative discount
      (multiplied with edge — legacy, default 1.0 = no effect)

    When using the LLM blend (67/33 market/AI), the blend itself
    is the adverse selection discount. Set lambda=1.0 and cost=0.0.

    Args:
        p_true: Believed true probability.
        p_market: Market price.
        nav: Current net asset value (bankroll).
        lambda_frac: Kelly fraction (default 0.25 = quarter-Kelly).
        adverse_selection_lambda: Multiplicative edge discount
            (1.0 = no discount, since blend handles it).
        adverse_selection_cost: Absolute AS cost to subtract from
            edge (e.g., trailing 5s markout in probability units).
        min_edge: Minimum adjusted edge to trade (default 3%).
        max_position_nav: Maximum position as fraction of NAV.

    Returns:
        (side, dollar_amount) — dollar_amount is 0 if below min_edge.
    """
    raw_edge = abs(p_true - p_market)
    adj_edge = raw_edge * adverse_selection_lambda - adverse_selection_cost
    adj_edge = max(0.0, adj_edge)

    if adj_edge < min_edge:
        return ("NO_TRADE", 0.0)

    # Compute Kelly with adjusted belief
    if p_true > p_market:
        p_adj = p_market + adj_edge
        side = "YES"
        f_star = kelly_fraction_yes(p_adj, p_market)
    else:
        p_adj = p_market - adj_edge
        side = "NO"
        f_star = kelly_fraction_no(p_adj, p_market)

    f_sized = lambda_frac * f_star
    dollar_amount = f_sized * nav
    max_dollars = max_position_nav * nav
    dollar_amount = min(dollar_amount, max_dollars)

    return (side, round(dollar_amount, 2))

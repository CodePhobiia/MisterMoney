"""
Signal Decay Functions
Time-based decay of fair value signals toward market consensus
"""

import math

# Route-specific half-lives (seconds)
HALF_LIVES = {
    'numeric': 60,       # 1 minute (fast-moving data)
    'simple': 900,       # 15 minutes
    'rule': 1800,        # 30 minutes
    'dossier': 7200,     # 2 hours
}


def decay_signal(
    p_raw: float,
    market_mid: float,
    age_seconds: float,
    source_staleness_seconds: float,
    route: str
) -> float:
    """
    Decay signal toward market consensus as it ages

    p_live = lambda(age) * p_raw + (1 - lambda(age)) * market_mid

    As signal ages, it decays toward market consensus.

    Route-specific half-lives:
    - numeric: 60s (fast-moving data)
    - simple: 900s (15 min)
    - rule: 1800s (30 min)
    - dossier: 7200s (2 hours)

    lambda = exp(-age / half_life)

    Source staleness additionally penalizes:
    lambda *= exp(-staleness / (2 * half_life))

    Args:
        p_raw: Original raw probability estimate
        market_mid: Current market midpoint
        age_seconds: Age of signal in seconds
        source_staleness_seconds: Age of underlying source data
        route: Route name (numeric, simple, rule, dossier)

    Returns:
        Decayed probability estimate
    """
    if route not in HALF_LIVES:
        raise ValueError(f"Unknown route: {route}. Must be one of {list(HALF_LIVES.keys())}")

    half_life = HALF_LIVES[route]

    # Base decay from signal age
    lambda_age = math.exp(-age_seconds / half_life)

    # Additional penalty from source staleness
    # (uses 2x half_life to be less aggressive)
    lambda_staleness = math.exp(-source_staleness_seconds / (2 * half_life))

    # Combined decay factor
    lambda_combined = lambda_age * lambda_staleness

    # Interpolate between raw signal and market consensus
    p_live = lambda_combined * p_raw + (1 - lambda_combined) * market_mid

    return p_live


def is_signal_expired(age_seconds: float, route: str) -> bool:
    """
    Check if signal is fully expired (decay factor < 0.05)

    Args:
        age_seconds: Age of signal in seconds
        route: Route name

    Returns:
        True if signal is expired
    """
    if route not in HALF_LIVES:
        raise ValueError(f"Unknown route: {route}. Must be one of {list(HALF_LIVES.keys())}")

    half_life = HALF_LIVES[route]
    lambda_age = math.exp(-age_seconds / half_life)

    return lambda_age < 0.05

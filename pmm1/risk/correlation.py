"""Thematic correlation grouping for cross-event risk limits.

Groups markets by theme keywords to enforce per-theme NAV limits,
preventing over-concentration on correlated events.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Theme keywords — markets matching any keyword in a theme are grouped
DEFAULT_THEMES = {
    "US_ELECTION": [
        "president", "election", "electoral", "biden", "trump", "harris",
        "republican", "democrat", "senate", "house", "governor",
    ],
    "CRYPTO_BTC": ["bitcoin", "btc"],
    "CRYPTO_ETH": ["ethereum", "eth"],
    "CRYPTO_SOL": ["solana", "sol"],
    "CRYPTO_GENERAL": ["crypto", "cryptocurrency", "defi"],
    "GEOPOLITICS": [
        "war", "invasion", "nato", "sanctions", "russia", "ukraine",
        "china", "taiwan", "iran", "israel",
    ],
    "FED_RATES": [
        "fed", "federal reserve", "interest rate", "fomc",
        "rate cut", "rate hike",
    ],
    "AI_TECH": [
        "openai", "chatgpt", "gemini", "claude",
        "artificial intelligence", "agi",
    ],
}


class ThematicCorrelation:
    """Groups markets by theme for cross-event correlation limits."""

    def __init__(
        self,
        themes: dict[str, list[str]] | None = None,
        per_theme_nav: float = 0.15,
    ) -> None:
        self.themes = themes or DEFAULT_THEMES
        self.per_theme_nav = per_theme_nav
        self._market_themes: dict[str, str] = {}  # condition_id → theme
        # KP-03: Dynamic correlation estimation by theme
        self._theme_outcomes: dict[str, list[float]] = {}  # theme → list of outcomes
        self._theme_rho: dict[str, float] = {
            "US_ELECTION": 0.30,
            "CRYPTO_BTC": 0.25,
            "CRYPTO_ETH": 0.25,
            "CRYPTO_SOL": 0.25,
            "CRYPTO_GENERAL": 0.20,
            "GEOPOLITICS": 0.20,
            "FED_RATES": 0.15,
            "AI_TECH": 0.10,
        }
        self._default_rho: float = 0.03

    def classify(self, condition_id: str, market_title: str) -> str:
        """Classify a market into a theme based on title keywords."""
        title_lower = market_title.lower()
        for theme, keywords in self.themes.items():
            if any(kw in title_lower for kw in keywords):
                self._market_themes[condition_id] = theme
                return theme
        self._market_themes[condition_id] = "uncorrelated"
        return "uncorrelated"

    def get_theme(self, condition_id: str) -> str:
        return self._market_themes.get(condition_id, "uncorrelated")

    def record_outcome(self, condition_id: str, outcome: float) -> None:
        """Record a market resolution for correlation estimation."""
        theme = self.get_theme(condition_id)
        if theme == "uncorrelated":
            return
        self._theme_outcomes.setdefault(theme, []).append(outcome)
        # Keep last 200 outcomes per theme
        if len(self._theme_outcomes[theme]) > 200:
            self._theme_outcomes[theme] = self._theme_outcomes[theme][-200:]
        # Update empirical rho if enough data
        if len(self._theme_outcomes[theme]) >= 20:
            self._update_theme_rho(theme)

    def _update_theme_rho(self, theme: str) -> None:
        """Estimate pairwise correlation from outcome data."""
        outcomes = self._theme_outcomes[theme]
        n = len(outcomes)
        if n < 20:
            return
        mean = sum(outcomes) / n
        var = sum((o - mean) ** 2 for o in outcomes) / n
        if var < 0.01:
            return
        # Use lag-1 autocorrelation as proxy for pairwise correlation
        # (markets in same theme resolve at similar times)
        cov = sum(
            (outcomes[i] - mean) * (outcomes[i - 1] - mean)
            for i in range(1, n)
        ) / (n - 1)
        rho = max(0.0, min(0.8, cov / var))
        # Blend with prior
        prior = self._theme_rho.get(theme, self._default_rho)
        self._theme_rho[theme] = 0.7 * prior + 0.3 * rho

    def get_theme_rho(self, condition_id: str) -> float:
        """Get estimated pairwise correlation for this market's theme."""
        theme = self.get_theme(condition_id)
        return self._theme_rho.get(theme, self._default_rho)

    def get_theme_exposure(self, theme: str, position_tracker: Any) -> float:
        """Get total gross exposure for a theme across all markets."""
        total = 0.0
        for cid, t in self._market_themes.items():
            if t == theme:
                pos = position_tracker.get(cid)
                if pos:
                    total += pos.total_cost_basis
        return total

    def get_theme_exposure_mark_to_market(
        self,
        theme: str,
        position_tracker: Any,
        price_oracle: dict[str, float] | None = None,
    ) -> float:
        """Get total marked gross exposure for a theme across all markets."""
        total = 0.0
        for cid, t in self._market_themes.items():
            if t != theme:
                continue
            pos = position_tracker.get(cid)
            if pos is None:
                continue
            yes_price = (
                float(price_oracle.get(pos.token_id_yes, pos.yes_avg_price) or 0.0)
                if price_oracle
                else pos.yes_avg_price
            )
            no_price = (
                float(price_oracle.get(pos.token_id_no, pos.no_avg_price) or 0.0)
                if price_oracle
                else pos.no_avg_price
            )
            total += pos.yes_size * yes_price
            total += pos.no_size * no_price
        return total

    def check_theme_limit(
        self,
        condition_id: str,
        proposed_additional: float,
        nav: float,
        position_tracker: Any,
        price_oracle: dict[str, float] | None = None,
    ) -> tuple[bool, float]:
        """Check if adding exposure would breach theme limit.

        Returns (passed, max_additional).
        """
        theme = self.get_theme(condition_id)
        if theme == "uncorrelated":
            return True, float("inf")

        current = self.get_theme_exposure_mark_to_market(
            theme,
            position_tracker,
            price_oracle=price_oracle,
        )
        limit = self.per_theme_nav * nav

        if current + proposed_additional > limit:
            return False, max(0.0, limit - current)
        return True, limit - current

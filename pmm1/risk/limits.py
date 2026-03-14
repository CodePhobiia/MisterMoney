"""Risk limits — hard caps from §14.

| Limit                    | Default |
|--------------------------|---------|
| Per-market gross         | 2% NAV  |
| Per-event cluster        | 5% NAV  |
| Total directional net    | 10% NAV |
| Total arb gross          | 25% NAV |
| Max orders per market    | 3       |
| Max quoted markets       | 20      |

Dynamic caps shrink on: rising volatility, rising model error,
falling time-to-catalyst, deepening drawdown, falling reward EV.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from pydantic import BaseModel, Field

from pmm1.risk.correlation import ThematicCorrelation
from pmm1.settings import RiskConfig
from pmm1.state.inventory import InventoryManager
from pmm1.state.positions import PositionTracker
from pmm1.strategy.quote_engine import QuoteIntent

logger = structlog.get_logger(__name__)


class LimitCheckResult(BaseModel):
    """Result of a risk limit check."""

    passed: bool = True
    breaches: list[str] = Field(default_factory=list)
    adjustments: dict[str, float] = Field(default_factory=dict)  # field → adjusted value


class QuoteRiskDiagnostics(BaseModel):
    """Structured diagnostics for risk-based quote adjustments."""

    bid_reasons: list[str] = Field(default_factory=list)
    ask_reasons: list[str] = Field(default_factory=list)


class RiskLimits:
    """Enforces hard risk caps on positions and orders."""

    def __init__(
        self,
        config: RiskConfig,
        position_tracker: PositionTracker,
        inventory_manager: InventoryManager,
        correlation: ThematicCorrelation | None = None,
    ) -> None:
        self.config = config
        self.positions = position_tracker
        self.inventory = inventory_manager
        self.correlation = correlation
        self._nav: float = 0.0
        # Dynamic multiplier (1.0 = normal, <1.0 = tighter)
        self._dynamic_multiplier: float = 1.0
        self._price_oracle_provider: Callable[[], dict[str, float]] | None = None

    def update_nav(self, nav: float) -> None:
        """Update current NAV for percentage-based limits."""
        self._nav = nav

    def set_dynamic_multiplier(self, multiplier: float) -> None:
        """Adjust limits dynamically based on market conditions.

        multiplier < 1.0 tightens limits (higher vol, drawdown, etc.)
        """
        self._dynamic_multiplier = max(0.1, min(1.0, multiplier))

    def set_price_oracle_provider(
        self,
        provider: Callable[[], dict[str, float]] | None,
    ) -> None:
        """Provide current mark prices for exposure-based risk checks."""
        self._price_oracle_provider = provider

    def _current_price_oracle(self) -> dict[str, float] | None:
        if self._price_oracle_provider is None:
            return None
        return self._price_oracle_provider()

    def _effective_limit(self, base_limit: float) -> float:
        """Apply dynamic multiplier to a base limit."""
        return base_limit * self._dynamic_multiplier

    def check_per_market_gross(
        self,
        condition_id: str,
        proposed_additional_dollars: float = 0.0,
    ) -> LimitCheckResult:
        """Check per-market gross exposure limit (default: 2% NAV).

        Both current_gross and proposed_additional should be in dollar terms.
        """
        if self._nav <= 0:
            return LimitCheckResult(passed=True)

        pos = self.positions.get(condition_id)
        current_gross = pos.gross_exposure_usdc(self._current_price_oracle()) if pos else 0.0
        new_gross = current_gross + proposed_additional_dollars
        limit = self._effective_limit(self.config.per_market_gross_nav) * self._nav

        if new_gross > limit:
            return LimitCheckResult(
                passed=False,
                breaches=[
                    f"per_market_gross: {new_gross:.2f} > {limit:.2f} "
                    f"({self.config.per_market_gross_nav*100:.1f}% NAV)"
                ],
                adjustments={"max_additional": max(0, limit - current_gross)},
            )
        return LimitCheckResult(passed=True)

    def check_per_event_cluster(
        self,
        event_id: str,
        proposed_additional: float = 0.0,
    ) -> LimitCheckResult:
        """Check per-event cluster exposure limit (default: 5% NAV)."""
        if self._nav <= 0:
            return LimitCheckResult(passed=True)

        current_gross = self.positions.get_event_gross_exposure_mark_to_market(
            event_id,
            price_oracle=self._current_price_oracle(),
        )
        new_gross = current_gross + proposed_additional
        limit = self._effective_limit(self.config.per_event_cluster_nav) * self._nav

        if new_gross > limit:
            return LimitCheckResult(
                passed=False,
                breaches=[
                    f"per_event_cluster: {new_gross:.2f} > {limit:.2f} "
                    f"({self.config.per_event_cluster_nav*100:.1f}% NAV)"
                ],
                adjustments={"max_additional": max(0, limit - current_gross)},
            )
        return LimitCheckResult(passed=True)

    def check_total_directional(
        self,
        proposed_additional_net: float = 0.0,
    ) -> LimitCheckResult:
        """Check total directional net exposure limit (default: 10% NAV)."""
        if self._nav <= 0:
            return LimitCheckResult(passed=True)

        current_net = self.positions.get_total_directional_exposure_mark_to_market(
            price_oracle=self._current_price_oracle()
        )
        new_net = current_net + abs(proposed_additional_net)
        limit = self._effective_limit(self.config.total_directional_nav) * self._nav

        if new_net > limit:
            return LimitCheckResult(
                passed=False,
                breaches=[
                    f"total_directional: {new_net:.2f} > {limit:.2f} "
                    f"({self.config.total_directional_nav*100:.1f}% NAV)"
                ],
            )
        return LimitCheckResult(passed=True)

    def check_total_arb_gross(
        self,
        proposed_additional: float = 0.0,
    ) -> LimitCheckResult:
        """Check total arb gross exposure limit (default: 25% NAV)."""
        if self._nav <= 0:
            return LimitCheckResult(passed=True)

        # Count arb positions (simplified: all positions for now)
        current_arb = self.positions.get_total_gross_exposure_mark_to_market(
            price_oracle=self._current_price_oracle()
        )
        new_arb = current_arb + proposed_additional
        limit = self._effective_limit(self.config.total_arb_gross_nav) * self._nav

        if new_arb > limit:
            return LimitCheckResult(
                passed=False,
                breaches=[
                    f"total_arb_gross: {new_arb:.2f} > {limit:.2f} "
                    f"({self.config.total_arb_gross_nav*100:.1f}% NAV)"
                ],
            )
        return LimitCheckResult(passed=True)

    def check_order_count(
        self,
        token_id: str,
        side: str,
        current_count: int,
    ) -> LimitCheckResult:
        """Check max orders per market side (default: 3)."""
        limit = self.config.max_orders_per_market_side
        if current_count >= limit:
            return LimitCheckResult(
                passed=False,
                breaches=[f"max_orders_per_side: {current_count} >= {limit}"],
            )
        return LimitCheckResult(passed=True)

    def check_quoted_markets(self, current_count: int) -> LimitCheckResult:
        """Check max quoted markets (default: 20)."""
        limit = self.config.max_quoted_markets
        if current_count >= limit:
            return LimitCheckResult(
                passed=False,
                breaches=[f"max_quoted_markets: {current_count} >= {limit}"],
            )
        return LimitCheckResult(passed=True)

    def apply_to_quote_with_diagnostics(
        self,
        intent: QuoteIntent,
        event_id: str = "",
    ) -> tuple[QuoteIntent, QuoteRiskDiagnostics]:
        """Apply all risk limits to a quote intent, adjusting sizes as needed.

        Returns a modified QuoteIntent plus structured diagnostics.
        """
        diagnostics = QuoteRiskDiagnostics()

        # Risk limits only constrain BUY orders (asks reduce exposure)
        # Save ask side — risk checks only touch bids
        saved_ask_price = intent.ask_price
        saved_ask_size = intent.ask_size

        if intent.bid_size and intent.bid_size > 0:
            bid_dollar = (intent.bid_size or 0) * (intent.bid_price or 0)

            # Per-market check (in dollar terms, buys only)
            market_check = self.check_per_market_gross(
                intent.condition_id,
                bid_dollar,
            )
            if not market_check.passed:
                max_add = market_check.adjustments.get("max_additional", 0)
                if max_add <= 0:
                    intent.bid_size = 0
                    intent.bid_price = None
                    diagnostics.bid_reasons.append("per_market_gross")
                    logger.warning(
                        "risk_zeroed_bid",
                        condition_id=intent.condition_id[:16],
                        reason="per_market_gross",
                    )
                elif intent.bid_price and intent.bid_price > 0:
                    intent.bid_size = min(intent.bid_size, max_add / intent.bid_price)
                    diagnostics.bid_reasons.append("per_market_gross")

            # Theme correlation check (buys only)
            if self.correlation and intent.bid_size and intent.bid_size > 0:
                bid_dollar = (intent.bid_size or 0) * (intent.bid_price or 0)
                theme_passed, theme_max = self.correlation.check_theme_limit(
                    intent.condition_id,
                    bid_dollar,
                    self._nav,
                    self.positions,
                    price_oracle=self._current_price_oracle(),
                )
                if not theme_passed:
                    if theme_max <= 0:
                        intent.bid_size = 0
                        intent.bid_price = None
                        diagnostics.bid_reasons.append("theme_correlation")
                        logger.warning(
                            "risk_zeroed_bid",
                            condition_id=intent.condition_id[:16],
                            reason="theme_correlation",
                        )
                    elif intent.bid_price and intent.bid_price > 0:
                        intent.bid_size = min(intent.bid_size, theme_max / intent.bid_price)
                        diagnostics.bid_reasons.append("theme_correlation")

            # Event cluster check (buys only)
            if event_id and intent.bid_size and intent.bid_size > 0:
                bid_dollar = (intent.bid_size or 0) * (intent.bid_price or 0)
                cluster_check = self.check_per_event_cluster(
                    event_id,
                    bid_dollar,
                )
                if not cluster_check.passed:
                    max_add = cluster_check.adjustments.get("max_additional", 0)
                    if max_add <= 0:
                        intent.bid_size = 0
                        intent.bid_price = None
                        diagnostics.bid_reasons.append("event_cluster")
                        logger.warning(
                            "risk_zeroed_bid",
                            condition_id=intent.condition_id[:16],
                            reason="event_cluster",
                        )
                    elif intent.bid_price and intent.bid_price > 0:
                        intent.bid_size = min(intent.bid_size, max_add / intent.bid_price)
                        diagnostics.bid_reasons.append("event_cluster")

        # Restore ask side (never blocked by risk limits)
        intent.ask_price = saved_ask_price
        intent.ask_size = saved_ask_size

        # Total directional check
        net_add = (intent.bid_size or 0) * (intent.bid_price or 0.0)
        dir_check = self.check_total_directional(net_add)
        if not dir_check.passed:
            # Scale down proportionally
            intent.bid_size = (intent.bid_size or 0) * 0.5
            intent.ask_size = (intent.ask_size or 0) * 0.5
            diagnostics.bid_reasons.append("total_directional")
            if intent.ask_size:
                diagnostics.ask_reasons.append("total_directional")
            logger.warning(
                "risk_scaled_quote",
                condition_id=intent.condition_id[:16],
                reason="total_directional",
            )

        diagnostics.bid_reasons = list(dict.fromkeys(diagnostics.bid_reasons))
        diagnostics.ask_reasons = list(dict.fromkeys(diagnostics.ask_reasons))
        return intent, diagnostics

    def apply_to_quote(
        self,
        intent: QuoteIntent,
        event_id: str = "",
    ) -> QuoteIntent:
        """Apply all risk limits to a quote intent."""
        adjusted_intent, _ = self.apply_to_quote_with_diagnostics(intent, event_id)
        return adjusted_intent

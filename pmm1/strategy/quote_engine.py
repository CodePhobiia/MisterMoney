"""Quote engine — reservation price, quote width, size model, crossing rule from §7.

Reservation price:
    r_t = clip(p̂_t − γ·q_t − η·q_t^cluster, ε, 1−ε)

Quote widths:
    δ_t = max(tick/2, δ_0 + δ_t^tox + δ_t^lat + δ_t^vol − δ_t^reward)
    bid_t = ⌊r_t − δ_t⌋_tick
    ask_t = ⌈r_t + δ_t⌉_tick

Size model:
    size_t = min(s_max, (s_0 · conf_t · rewardBoost_t) / (1 + k|q_t|))

Quote objective:
    EV = P_b·(r_t − bid − AS_b) + P_a·(ask − r_t − AS_a) + EV^liq + EV^rebate − InvPenalty
"""

from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import structlog
from pydantic import BaseModel, Field

from pmm1.settings import PricingConfig
from pmm1.strategy.features import FeatureVector

logger = structlog.get_logger(__name__)


class QuoteIntent(BaseModel):
    """Desired quote for one side of one market."""

    token_id: str
    condition_id: str = ""
    bid_price: float | None = None
    bid_size: float | None = None
    ask_price: float | None = None
    ask_size: float | None = None
    reservation_price: float = 0.0
    half_spread: float = 0.0
    strategy: str = "mm"
    confidence: float = 0.0
    neg_risk: bool = False
    bid_suppression_reasons: list[str] = Field(default_factory=list)
    ask_suppression_reasons: list[str] = Field(default_factory=list)

    @property
    def has_bid(self) -> bool:
        return self.bid_price is not None and self.bid_size is not None and self.bid_size > 0

    @property
    def has_ask(self) -> bool:
        return self.ask_price is not None and self.ask_size is not None and self.ask_size > 0


class QuoteEngine:
    """Computes two-sided quotes around the reservation price.

    Uses inventory skew, quote width model, size model.
    """

    def __init__(
        self,
        config: PricingConfig,
        base_size: float = 5.0,
        max_size: float = 20.0,
        size_decay_k: float = 0.1,
        target_dollar_size: float = 8.0,
        max_dollar_size: float = 15.0,
    ) -> None:
        self.config = config
        self.base_size = base_size
        self.max_size = max_size
        self.size_decay_k = size_decay_k
        self.target_dollar_size = target_dollar_size
        self.max_dollar_size = max_dollar_size

    def compute_reservation_price(
        self,
        fair_value: float,
        market_inventory: float,
        cluster_inventory: float = 0.0,
        position_age_hours: float = 0.0,
        time_to_resolution_hours: float = float("inf"),  # MM-08
    ) -> float:
        """Compute reservation price with inventory skew from §7.

        r_t = clip(p̂_t − γ·q_t − η·q_t^cluster, ε, 1−ε)

        Dynamic γ: starts at gamma_base and ramps towards gamma_max as
        position ages, using exponential decay with configurable halflife.

        Args:
            fair_value: p̂_t from fair value model.
            market_inventory: q_t signed inventory (positive = long YES).
            cluster_inventory: q_t^cluster correlated exposure.
            position_age_hours: How long the current position has been held.
            time_to_resolution_hours: Hours until market resolves (MM-08).

        Returns:
            Reservation price clipped to (ε, 1-ε).
        """
        gamma_base = self.config.inventory_skew_gamma
        gamma_max = self.config.gamma_max
        halflife = self.config.age_halflife_hours
        eta = self.config.cluster_skew_eta
        epsilon = 0.005  # Price floor/ceiling

        # Dynamic gamma: ramp from base to max as inventory ages
        if position_age_hours > 0 and halflife > 0:
            age_factor = 1.0 - math.exp(-0.693 * position_age_hours / halflife)
        else:
            age_factor = 0.0

        # MM-11: Inventory-proportional urgency
        max_position = getattr(self.config, 'max_position_shares', 200.0)
        inv_fraction = abs(market_inventory) / max(1.0, max_position)
        inv_urgency = min(1.0, inv_fraction ** 0.5)  # sqrt for diminishing sensitivity

        # Use whichever is stronger: age or inventory urgency
        effective_gamma = gamma_base + (gamma_max - gamma_base) * max(age_factor, inv_urgency)

        # MM-08: Time-to-resolution urgency
        # As resolution approaches, gamma increases to flatten inventory faster
        time_to_res = max(0.1, time_to_resolution_hours)
        if time_to_res < float('inf'):
            time_urgency = min(1.0, 24.0 / time_to_res)
            urgency_multiplier = 0.5
            effective_gamma *= (1 + time_urgency * urgency_multiplier)

        r_t = fair_value - effective_gamma * market_inventory - eta * cluster_inventory
        return max(epsilon, min(1.0 - epsilon, r_t))

    def compute_half_spread(
        self,
        features: FeatureVector,
        tick_size: float = 0.01,
        reward_ev: float = 0.0,
        optimal_base_spread: float | None = None,  # CL-01
        optimal_gamma: float | None = None,
    ) -> float:
        """Avellaneda-Stoikov optimal half-spread.

        delta = (gamma * sigma_eff^2 * t_eff) / 2 + (1/gamma) * ln(1 + gamma/kappa)

        Plus additive adjustments for latency and reward discount.

        Args:
            features: Current market features.
            tick_size: Minimum price increment.
            reward_ev: Expected value of liquidity rewards.

        Returns:
            Half-spread in price units (not cents).
        """
        gamma = self.config.inventory_skew_gamma
        if optimal_gamma is not None and optimal_gamma > 0:
            gamma = 0.7 * gamma + 0.3 * optimal_gamma
        sigma_eff = max(0.01, features.sigma_eff)  # from MM-02 Phase 1

        # Time horizon: normalize to [0, 1] session
        t_eff = min(features.time_to_resolution_hours, 24.0) / 24.0
        t_eff = max(0.01, t_eff)  # Floor to prevent zero

        # Order arrival rate (kappa) from features
        kappa = max(0.01, features.kappa_estimate)

        # A-S optimal half-spread
        delta_as = (gamma * sigma_eff ** 2 * t_eff) / 2 + (1 / gamma) * math.log(1 + gamma / kappa)

        # Binary market: clamp half-spread to valid range
        # A-S formula overestimates for [0,1] markets due to 1/gamma
        delta_as = min(0.49, delta_as)

        # CL-01: Blend with learned optimal spread if available
        if optimal_base_spread is not None and optimal_base_spread > 0:
            delta_as = 0.7 * delta_as + 0.3 * optimal_base_spread

        # Latency component (keep from original)
        delta_lat = 0.003 if features.is_stale else 0.0

        # PM-01: Quadratic reward-aware spread discount
        # Polymarket scores quadratically: being 2x tighter ≈ 4x reward share
        # Optimize: tighten spread until marginal reward gain < marginal AS risk
        if reward_ev > 0:
            # Quadratic bonus: aggressive tightening for high-reward markets
            # reward_discount = base_discount * (1 + reward_ev * quadratic_boost)
            base_discount = reward_ev * self.config.reward_capture_weight
            quadratic_boost = min(2.0, reward_ev * 10.0)  # Cap at 3x tightening
            delta_reward = base_discount * (1 + quadratic_boost)
        else:
            delta_reward = 0.0

        half_spread = delta_as + delta_lat - delta_reward
        min_half_spread = tick_size / 2.0

        # MM-04+PM-04: VPIN toxicity widening
        if features.vpin > 0.3:
            vpin_mult = 1.0 + (features.vpin - 0.3) * 1.5
            half_spread *= vpin_mult

        # PM-03: Time-of-day adjustment
        if features.tod_spread_mult != 1.0:
            half_spread *= features.tod_spread_mult

        # PM-07: Maker rebate tightening
        if features.rebate_spread_discount > 0:
            half_spread = max(min_half_spread, half_spread - features.rebate_spread_discount)

        return max(min_half_spread, half_spread)

    def compute_size(
        self,
        confidence: float,
        market_inventory: float,
        reward_boost: float = 1.0,
        volatility_regime: str = "normal",
        time_to_catalyst_hours: float = float("inf"),
        fair_value: float = 0.5,
        market_price: float = 0.0,
        nav: float = 0.0,
        edge_confidence: float = 1.0,
        n_active_positions: int = 1,
        shrinkage: float | None = None,        # KP-02
        dd_size_cap: float | None = None,      # KP-04
        diversity_disc: float | None = None,   # KP-05
    ) -> float:
        """Compute quote size in SHARES.

        Two modes:
        - Kelly (kelly_enabled=True): edge-proportional sizing
          from Paper 2, §1. Bets more on larger edges.
        - Dollar-flat (default): target $8 per market per side,
          independent of edge magnitude.

        Both modes apply the same downstream adjustments:
        confidence, inventory, volatility, time-to-catalyst.
        """
        price = max(0.01, fair_value)
        k = self.size_decay_k

        cfg = self.config
        _kelly_active = False
        if (
            cfg.kelly_enabled
            and nav > 0
            and market_price > 0
        ):
            # Kelly sizing (Paper 2 §1 + §2)
            from pmm1.math.kelly import kelly_bet_dollars

            # KP-01: Adaptive lambda ramp — scale with edge_confidence
            effective_lambda = (
                cfg.kelly_base_lambda
                + (cfg.kelly_max_lambda - cfg.kelly_base_lambda) * edge_confidence
            )

            # KP-02: Baker-McHale shrinkage for estimation uncertainty
            if shrinkage is not None:
                effective_lambda *= shrinkage

            # KP-04: Drawdown-constrained cap
            if dd_size_cap is not None and dd_size_cap < effective_lambda:
                effective_lambda = dd_size_cap

            # KP-05: Ensemble diversity discount
            if diversity_disc is not None:
                effective_lambda *= diversity_disc

            _, dollar_size = kelly_bet_dollars(
                p_true=fair_value,
                p_market=market_price,
                nav=nav,
                lambda_frac=effective_lambda,
                adverse_selection_lambda=(
                    cfg.kelly_adverse_selection_lambda
                ),
                min_edge=cfg.kelly_min_edge,
                max_position_nav=cfg.kelly_max_position_nav,
            )
            # M5: Correlation adjustment for simultaneous positions.
            # We pass 1.0 (not the actual Kelly fraction) because the function
            # is linear in f_star, so multi_bet_kelly_adjustment(1.0, N, rho)
            # returns the pure scaling factor 1/(1+(N-1)*rho) which we then
            # multiply into dollar_size. Mathematically equivalent to passing
            # the real fraction and using the result directly.
            if n_active_positions > 1:
                from pmm1.math.kelly import multi_bet_kelly_adjustment
                corr_factor = multi_bet_kelly_adjustment(
                    1.0, n_active_positions, rho=0.05,
                )
                dollar_size *= corr_factor
            # Q-H1: When Kelly-sizing, don't override with 5-share floor.
            # If Kelly says the position is too small for Polymarket minimum ($1.50),
            # return 0 (don't trade) rather than inflating to 5 shares.
            _kelly_active = True
            if dollar_size <= 0:
                return 0.0
            target_shares = dollar_size / price
        else:
            # Dollar-flat sizing (original)
            target_shares = self.target_dollar_size / price

        max_shares = self.max_dollar_size / price

        # Apply shared adjustments
        size = (
            (target_shares * confidence * reward_boost)
            / (1.0 + k * abs(market_inventory))
        )

        # Volatility discount
        vol_multiplier = {
            "low": 1.2,
            "normal": 1.0,
            "high": 0.6,
            "extreme": 0.3,
        }.get(volatility_regime, 1.0)
        size *= vol_multiplier

        # Time-to-catalyst discount
        if time_to_catalyst_hours < 2:
            size *= 0.3
        elif time_to_catalyst_hours < 6:
            size *= 0.5
        elif time_to_catalyst_hours < 24:
            size *= 0.8

        if _kelly_active:
            return max(0.0, min(max_shares, size))  # Kelly: no artificial floor
        return max(5.0, min(max_shares, size))  # Dollar-flat: keep minimum

    def compute_quote(
        self,
        token_id: str,
        features: FeatureVector,
        fair_value: float,
        haircut: float,
        confidence: float,
        market_inventory: float,
        cluster_inventory: float = 0.0,
        tick_size: float = 0.01,
        reward_ev: float = 0.0,
        neg_risk: bool = False,
        condition_id: str = "",
        position_age_hours: float = 0.0,
        market_price: float = 0.0,
        nav: float = 0.0,
        edge_confidence: float = 1.0,
        n_active_positions: int = 1,
        optimal_base_spread: float | None = None,  # CL-01
        optimal_gamma: float | None = None,
        shrinkage: float | None = None,        # KP-02
        dd_size_cap: float | None = None,      # KP-04
        diversity_disc: float | None = None,   # KP-05
    ) -> QuoteIntent:
        """Compute full two-sided quote intent.

        Args:
            token_id: Asset token ID.
            features: Market feature vector.
            fair_value: p̂_t from fair value model.
            haircut: h_t from fair value model.
            confidence: 1 - haircut.
            market_inventory: q_t signed inventory.
            cluster_inventory: q_t^cluster.
            tick_size: Minimum price increment.
            reward_ev: Reward EV for spread tightening.
            neg_risk: Whether this is a neg-risk market.
            condition_id: Market condition ID.

        Returns:
            QuoteIntent with bid/ask prices and sizes.
        """
        # 1. Reservation price (with dynamic γ based on position age)
        r_t = self.compute_reservation_price(
            fair_value, market_inventory, cluster_inventory, position_age_hours,
            time_to_resolution_hours=features.time_to_resolution_hours,  # MM-08
        )

        # 2. Half spread
        delta_t = self.compute_half_spread(
            features, tick_size, reward_ev, optimal_base_spread, optimal_gamma,
        )

        # MM-07: Asymmetric spread skew
        # When long: wider bid (don't accumulate), tighter ask (eager to sell)
        # When short: tighter bid (eager to buy), wider ask (don't accumulate)
        max_pos = getattr(self.config, 'max_position_shares', 200.0)
        inv_fraction = market_inventory / max(1.0, max_pos)
        skew_factor = 0.3  # How aggressively to skew

        delta_bid = delta_t * (1 + skew_factor * max(0, inv_fraction))
        delta_ask = delta_t * (1 + skew_factor * max(0, -inv_fraction))

        # 3. Tick-round bid and ask (using asymmetric deltas)
        tick = Decimal(str(tick_size))
        raw_bid = r_t - delta_bid
        raw_ask = r_t + delta_ask

        # Floor bid to tick, ceil ask to tick
        bid_price = float(
            (Decimal(str(raw_bid)) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        )
        ask_price = float(
            (Decimal(str(raw_ask)) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        )

        # Clamp prices
        bid_price = max(tick_size, min(1.0 - tick_size, bid_price))
        ask_price = max(tick_size, min(1.0 - tick_size, ask_price))

        # Ensure bid < ask
        if bid_price >= ask_price:
            mid = (bid_price + ask_price) / 2.0
            bid_price = float(
                (Decimal(str(mid - tick_size)) / tick)
                .to_integral_value(rounding=ROUND_FLOOR) * tick
            )
            ask_price = float(
                (Decimal(str(mid + tick_size)) / tick)
                .to_integral_value(rounding=ROUND_CEILING) * tick
            )
            # Re-clamp after resolution to stay in valid range
            bid_price = max(
                tick_size, min(1.0 - tick_size, bid_price),
            )
            ask_price = max(
                tick_size, min(1.0 - tick_size, ask_price),
            )

        # 4. Size (dollar-based)
        reward_boost = 1.0 + reward_ev * 5.0 if reward_ev > 0 else 1.0
        size = self.compute_size(
            confidence=confidence,
            market_inventory=market_inventory,
            reward_boost=reward_boost,
            volatility_regime=features.vol_regime,
            time_to_catalyst_hours=features.time_to_resolution_hours,
            fair_value=fair_value,
            market_price=market_price,
            nav=nav,
            edge_confidence=edge_confidence,
            n_active_positions=n_active_positions,
            shrinkage=shrinkage,
            dd_size_cap=dd_size_cap,
            diversity_disc=diversity_disc,
        )

        # Asymmetric sizing to encourage position flattening:
        #
        # When long YES (inventory > 0):
        #   - Reduce bid_size: don't accumulate more long
        #   - Boost ask_size: encourage selling to flatten
        #
        # When short YES (inventory < 0):
        #   - Reduce ask_size: don't increase the short further
        #   - Boost bid_size: encourage buying to close the short
        #
        # Note: market_inventory is negative in the short case, so
        # (1.0 + 0.05 * negative_value) < 1.0 → reduces ask_size ✓
        # (1.0 - 0.03 * negative_value) > 1.0 → boosts bid_size ✓
        bid_size = size
        ask_size = size

        if market_inventory > 0:
            # Long YES → reduce bid (don't accumulate more), boost ask
            bid_size *= max(0.3, 1.0 - 0.05 * market_inventory)
            ask_size *= min(2.0, 1.0 + 0.03 * market_inventory)
        elif market_inventory < 0:
            # Short YES → reduce ask, boost bid
            ask_size *= max(0.3, 1.0 + 0.05 * market_inventory)
            bid_size *= min(2.0, 1.0 - 0.03 * market_inventory)

        bid_size = max(1.0, round(bid_size, 2))
        ask_size = max(1.0, round(ask_size, 2))

        # Enforce Polymarket minimums: min 5 shares AND min $1 dollar value
        min_shares = 5.0
        min_dollar = 1.5
        if bid_price > 0:
            min_bid_shares = max(min_shares, min_dollar / bid_price)
            bid_size = max(bid_size, min_bid_shares)
        if ask_price > 0:
            min_ask_shares = max(min_shares, min_dollar / ask_price)
            ask_size = max(ask_size, min_ask_shares)

        intent = QuoteIntent(
            token_id=token_id,
            condition_id=condition_id,
            bid_price=bid_price,
            bid_size=bid_size,
            ask_price=ask_price,
            ask_size=ask_size,
            reservation_price=r_t,
            half_spread=delta_t,
            strategy="mm",
            confidence=confidence,
            neg_risk=neg_risk,
        )

        logger.debug(
            "quote_computed",
            token_id=token_id[:16],
            r_t=f"{r_t:.4f}",
            bid=f"{bid_price:.4f}x{bid_size:.0f}",
            ask=f"{ask_price:.4f}x{ask_size:.0f}",
            delta=f"{delta_t:.4f}",
            inv=f"{market_inventory:.1f}",
        )

        return intent

    def compute_layered_quotes(
        self,
        token_id: str,
        features: FeatureVector,
        fair_value: float,
        haircut: float,
        confidence: float,
        market_inventory: float,
        cluster_inventory: float = 0.0,
        tick_size: float = 0.01,
        reward_ev: float = 0.0,
        neg_risk: bool = False,
        condition_id: str = "",
        position_age_hours: float = 0.0,
        market_price: float = 0.0,
        nav: float = 0.0,
        edge_confidence: float = 1.0,
        n_active_positions: int = 1,
        n_layers: int = 3,
        optimal_base_spread: float | None = None,  # CL-01
        optimal_gamma: float | None = None,
        shrinkage: float | None = None,        # KP-02
        dd_size_cap: float | None = None,      # KP-04
        diversity_disc: float | None = None,   # KP-05
    ) -> list[QuoteIntent]:
        """Generate layered quotes at multiple price levels.

        MM-12: Layer 1 (60% size) at bid/ask
               Layer 2 (30% size) at bid-1tick/ask+1tick
               Layer 3 (10% size) at bid-2ticks/ask+2ticks
        """
        base = self.compute_quote(
            token_id=token_id, features=features, fair_value=fair_value,
            haircut=haircut, confidence=confidence,
            market_inventory=market_inventory,
            cluster_inventory=cluster_inventory, tick_size=tick_size,
            reward_ev=reward_ev, neg_risk=neg_risk, condition_id=condition_id,
            position_age_hours=position_age_hours, market_price=market_price,
            nav=nav, edge_confidence=edge_confidence,
            n_active_positions=n_active_positions,
            optimal_base_spread=optimal_base_spread,
            optimal_gamma=optimal_gamma,
            shrinkage=shrinkage,
            dd_size_cap=dd_size_cap,
            diversity_disc=diversity_disc,
        )

        if n_layers <= 1:
            return [base]

        layers = []
        size_fractions = [0.60, 0.30, 0.10][:n_layers]

        for i, frac in enumerate(size_fractions):
            layer = QuoteIntent(
                token_id=token_id,
                condition_id=condition_id,
                bid_price=(
                    max(tick_size, (base.bid_price or 0) - i * tick_size)
                    if base.bid_price else None
                ),
                bid_size=(
                    max(5.0, round((base.bid_size or 0) * frac, 2))
                    if base.bid_size else None
                ),
                ask_price=(
                    min(1 - tick_size, (base.ask_price or 0) + i * tick_size)
                    if base.ask_price else None
                ),
                ask_size=(
                    max(5.0, round((base.ask_size or 0) * frac, 2))
                    if base.ask_size else None
                ),
                reservation_price=base.reservation_price,
                half_spread=base.half_spread + i * tick_size,
                strategy=f"mm_layer{i}",
                confidence=base.confidence,
                neg_risk=neg_risk,
            )
            layers.append(layer)

        return layers

    def compute_quote_ev(
        self,
        reservation_price: float,
        bid_price: float,
        ask_price: float,
        fill_prob_bid: float = 0.3,
        fill_prob_ask: float = 0.3,
        as_cost: float = 0.0,
    ) -> float:
        """Expected value of quoting (MM-10).

        EV = P_b*(r - bid - AS) + P_a*(ask - r - AS)

        If EV < 0, quoting this market loses money in expectation.
        """
        ev_bid = fill_prob_bid * (reservation_price - bid_price - abs(as_cost))
        ev_ask = fill_prob_ask * (ask_price - reservation_price - abs(as_cost))
        return ev_bid + ev_ask

    def should_suppress_quotes(
        self,
        quote_ev: float,
        min_ev: float = 0.001,
    ) -> bool:
        """Whether to suppress quotes for negative EV (MM-10)."""
        return quote_ev < min_ev

    def check_crossing_rule(
        self,
        fair_value: float,
        execution_price: float,
        side: str,
        haircut: float,
        fee: float = 0.0,
        slippage: float = 0.0,
        quantity: float = 1.0,
    ) -> tuple[bool, float]:
        """Check if crossing the spread is profitable (§7).

        takeEV = (p̂_t - p_exec)·Q - fee - slippage - h_t
        Only cross if takeEV > take_threshold AND no cluster/drawdown breach.

        Returns:
            (should_cross, take_ev)
        """
        # KP-09: Scale threshold with execution costs
        base_threshold = self.config.take_threshold_cents / 100.0
        # Add estimated slippage proportional to quantity
        slippage_estimate = 0.001 * quantity  # ~0.1c per share
        effective_threshold = base_threshold + fee + slippage + slippage_estimate

        if side == "BUY":
            raw_edge = (fair_value - execution_price) * quantity
        else:
            raw_edge = (execution_price - fair_value) * quantity

        take_ev = raw_edge - fee - slippage - haircut * quantity

        should_cross = raw_edge > effective_threshold * quantity

        if should_cross:
            logger.info(
                "crossing_rule_passed",
                side=side,
                fair_value=f"{fair_value:.4f}",
                price=f"{execution_price:.4f}",
                take_ev=f"{take_ev:.4f}",
                threshold=f"{effective_threshold:.4f}",
            )

        return should_cross, take_ev

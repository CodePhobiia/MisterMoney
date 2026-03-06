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
from pydantic import BaseModel

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
    ) -> None:
        self.config = config
        self.base_size = base_size
        self.max_size = max_size
        self.size_decay_k = size_decay_k

    def compute_reservation_price(
        self,
        fair_value: float,
        market_inventory: float,
        cluster_inventory: float = 0.0,
    ) -> float:
        """Compute reservation price with inventory skew from §7.

        r_t = clip(p̂_t − γ·q_t − η·q_t^cluster, ε, 1−ε)

        Args:
            fair_value: p̂_t from fair value model.
            market_inventory: q_t signed inventory (positive = long YES).
            cluster_inventory: q_t^cluster correlated exposure.

        Returns:
            Reservation price clipped to (ε, 1-ε).
        """
        gamma = self.config.inventory_skew_gamma
        eta = self.config.cluster_skew_eta
        epsilon = 0.005  # Price floor/ceiling

        r_t = fair_value - gamma * market_inventory - eta * cluster_inventory
        return max(epsilon, min(1.0 - epsilon, r_t))

    def compute_half_spread(
        self,
        features: FeatureVector,
        tick_size: float = 0.01,
        reward_ev: float = 0.0,
    ) -> float:
        """Compute half-spread δ_t from §7.

        δ_t = max(tick/2, δ_0 + δ_t^tox + δ_t^lat + δ_t^vol − δ_t^reward)

        Args:
            features: Current market features.
            tick_size: Minimum price increment.
            reward_ev: Expected value of liquidity rewards.

        Returns:
            Half-spread in price units (not cents).
        """
        delta_0 = self.config.base_half_spread_cents / 100.0  # Convert cents to price

        # Toxicity component: widen on aggressive flow
        delta_tox = 0.0
        if features.sweep_intensity > 0.5:
            delta_tox = 0.005  # +0.5¢ for high sweep activity
        elif features.sweep_intensity > 0.2:
            delta_tox = 0.002

        # Latency component (simplified): widen if data is stale
        delta_lat = 0.003 if features.is_stale else 0.0

        # Volatility component
        delta_vol = 0.0
        if features.vol_regime == "extreme":
            delta_vol = 0.01
        elif features.vol_regime == "high":
            delta_vol = 0.005
        elif features.vol_regime == "normal":
            delta_vol = 0.001

        # Reward discount: tighten spread to capture rewards
        delta_reward = reward_ev * self.config.reward_capture_weight

        # Combine
        half_spread = delta_0 + delta_tox + delta_lat + delta_vol - delta_reward
        min_half_spread = tick_size / 2.0

        return max(min_half_spread, half_spread)

    def compute_size(
        self,
        confidence: float,
        market_inventory: float,
        reward_boost: float = 1.0,
        volatility_regime: str = "normal",
        time_to_catalyst_hours: float = float("inf"),
    ) -> float:
        """Compute quote size from §7.

        size_t = min(s_max, (s_0 · conf_t · rewardBoost_t) / (1 + k|q_t|))

        Smaller when: high volatility, high toxicity, imbalanced inventory,
        short time to catalyst.
        """
        s_0 = self.base_size
        k = self.size_decay_k

        # Base size adjusted for confidence and rewards
        size = (s_0 * confidence * reward_boost) / (1.0 + k * abs(market_inventory))

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

        return max(1.0, min(self.max_size, size))

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
        # 1. Reservation price
        r_t = self.compute_reservation_price(
            fair_value, market_inventory, cluster_inventory
        )

        # 2. Half spread
        delta_t = self.compute_half_spread(features, tick_size, reward_ev)

        # 3. Tick-round bid and ask
        tick = Decimal(str(tick_size))
        raw_bid = r_t - delta_t
        raw_ask = r_t + delta_t

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
                (Decimal(str(mid - tick_size)) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
            )
            ask_price = float(
                (Decimal(str(mid + tick_size)) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
            )

        # 4. Size
        reward_boost = 1.0 + reward_ev * 5.0 if reward_ev > 0 else 1.0
        size = self.compute_size(
            confidence=confidence,
            market_inventory=market_inventory,
            reward_boost=reward_boost,
            volatility_regime=features.vol_regime,
            time_to_catalyst_hours=features.time_to_resolution_hours,
        )

        # Asymmetric sizing: smaller on the side with more inventory
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

        # Enforce minimum dollar value per order ($1 min on Polymarket)
        min_dollar = 1.5  # $1.50 to clear the $1 minimum with margin
        if bid_price > 0:
            min_bid_shares = min_dollar / bid_price
            bid_size = max(bid_size, round(min_bid_shares + 0.5, 2))
        if ask_price > 0:
            min_ask_shares = min_dollar / ask_price
            ask_size = max(ask_size, round(min_ask_shares + 0.5, 2))

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
        threshold = self.config.take_threshold_cents / 100.0

        if side == "BUY":
            raw_edge = (fair_value - execution_price) * quantity
        else:
            raw_edge = (execution_price - fair_value) * quantity

        take_ev = raw_edge - fee - slippage - haircut * quantity

        should_cross = take_ev > threshold * quantity

        if should_cross:
            logger.info(
                "crossing_rule_passed",
                side=side,
                fair_value=f"{fair_value:.4f}",
                price=f"{execution_price:.4f}",
                take_ev=f"{take_ev:.4f}",
                threshold=f"{threshold:.4f}",
            )

        return should_cross, take_ev

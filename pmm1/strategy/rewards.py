"""Liquidity reward and maker rebate estimation from §8.

Liquidity rewards:
    EV^liq_share = (rewardShare_m · rewardPool_m) / expectedFilledShares_m
    Score: quadratic in distance from mid, min-of-sides for two-sided boost,
    discount c=3.0 for single-sided in [0.10, 0.90].
    Quote both sides always on reward markets.

Maker rebates:
    EV^rebate = shareOfFeeEquivalent_m · rebatePool_m
    Updated daily via /rebates/current endpoint reconciliation.
"""

from __future__ import annotations

import math

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class RewardParams(BaseModel):
    """Parameters for a reward-eligible market."""

    condition_id: str
    daily_reward_rate: float = 0.0  # Daily reward pool USDC
    min_size: float = 0.0  # Minimum size for reward eligibility
    max_spread: float = 0.0  # Maximum spread for reward eligibility
    reward_epoch: int = 0
    # Estimated parameters
    total_market_liquidity: float = 0.0  # Total liquidity in the market
    expected_filled_shares: float = 0.0  # Expected daily fills


class RewardEstimate(BaseModel):
    """Estimated reward value for quoting a market."""

    condition_id: str = ""
    liquidity_reward_ev: float = 0.0  # Expected daily liquidity reward
    maker_rebate_ev: float = 0.0  # Expected daily maker rebate
    total_ev: float = 0.0  # Total reward EV per day
    reward_per_share: float = 0.0  # Reward per share quoted
    is_eligible: bool = False
    score_components: dict[str, float] = Field(default_factory=dict)


class RewardEstimator:
    """Estimates liquidity rewards and maker rebates.

    Polymarket reward scoring:
    - Score is quadratic in distance from mid (closer = better)
    - Two-sided quoting gets min(bid_score, ask_score) as boost
    - Single-sided in [0.10, 0.90] range gets c=3.0 discount
    - At extremes (<0.10 or >0.90), both sides required
    """

    # Single-sided discount factor
    SINGLE_SIDED_DISCOUNT = 3.0

    def __init__(
        self,
        daily_rebate_pool: float = 0.0,  # Total daily rebate pool USDC
        our_maker_share: float = 0.01,  # Our estimated share of maker volume
    ) -> None:
        self._reward_params: dict[str, RewardParams] = {}
        self._daily_rebate_pool = daily_rebate_pool
        self._our_maker_share = our_maker_share

    def register_reward_market(self, params: RewardParams) -> None:
        """Register or update reward parameters for a market."""
        self._reward_params[params.condition_id] = params

    def is_reward_eligible(self, condition_id: str) -> bool:
        """Check if a market is reward-eligible."""
        return condition_id in self._reward_params

    def compute_position_score(
        self,
        distance_from_mid: float,
        size: float,
        min_size: float = 0.0,
    ) -> float:
        """Compute reward score for a single position.

        Score is quadratic in distance: closer to mid = higher score.
        Score = size × (1 - (distance / max_distance)^2)

        Returns 0 if below minimum size.
        """
        if size < min_size:
            return 0.0

        max_distance = 0.10  # 10¢ max scoring distance
        if distance_from_mid >= max_distance:
            return 0.0

        # Quadratic decay: tighter quotes score much better
        distance_factor = 1.0 - (distance_from_mid / max_distance) ** 2
        return size * max(0.0, distance_factor)

    def compute_two_sided_score(
        self,
        bid_distance: float,
        ask_distance: float,
        bid_size: float,
        ask_size: float,
        midpoint: float,
        min_size: float = 0.0,
    ) -> dict[str, float]:
        """Compute two-sided reward score.

        Two-sided boost: take min(bid_score, ask_score)
        Single-sided discount: c=3.0 for prices in [0.10, 0.90]
        At extremes: both sides required for any score

        Returns dict with component scores.
        """
        bid_score = self.compute_position_score(bid_distance, bid_size, min_size)
        ask_score = self.compute_position_score(ask_distance, ask_size, min_size)

        is_extreme = midpoint < 0.10 or midpoint > 0.90
        is_two_sided = bid_score > 0 and ask_score > 0

        if is_extreme and not is_two_sided:
            # At extremes, both sides required
            return {
                "bid_score": 0.0,
                "ask_score": 0.0,
                "two_sided_boost": 0.0,
                "total_score": 0.0,
                "is_two_sided": False,
            }

        if is_two_sided:
            # Two-sided boost: use min of both sides
            two_sided_boost = min(bid_score, ask_score)
            total = bid_score + ask_score + two_sided_boost
        else:
            # Single-sided with discount
            total = (bid_score + ask_score) / self.SINGLE_SIDED_DISCOUNT

        return {
            "bid_score": bid_score,
            "ask_score": ask_score,
            "two_sided_boost": min(bid_score, ask_score) if is_two_sided else 0.0,
            "total_score": total,
            "is_two_sided": is_two_sided,
        }

    def estimate_reward(
        self,
        condition_id: str,
        bid_distance: float,
        ask_distance: float,
        bid_size: float,
        ask_size: float,
        midpoint: float = 0.5,
    ) -> RewardEstimate:
        """Estimate total reward EV for quoting a market.

        EV^liq_share = (rewardShare_m · rewardPool_m) / expectedFilledShares_m

        Args:
            condition_id: Market condition ID.
            bid_distance: Distance of our bid from midpoint.
            ask_distance: Distance of our ask from midpoint.
            bid_size: Our bid size.
            ask_size: Our ask size.
            midpoint: Current midpoint price.

        Returns:
            RewardEstimate with daily EV breakdown.
        """
        params = self._reward_params.get(condition_id)
        if params is None:
            return RewardEstimate(
                condition_id=condition_id,
                is_eligible=False,
            )

        # Check size eligibility
        if bid_size < params.min_size and ask_size < params.min_size:
            return RewardEstimate(
                condition_id=condition_id,
                is_eligible=False,
            )

        # Compute our score
        score_components = self.compute_two_sided_score(
            bid_distance, ask_distance,
            bid_size, ask_size,
            midpoint,
            params.min_size,
        )

        our_score = score_components["total_score"]

        # Estimate our share of the reward pool
        # Rough heuristic: our score / (our score + market average)
        market_avg_score = max(1.0, params.total_market_liquidity * 0.1)
        our_share = our_score / (our_score + market_avg_score) if our_score > 0 else 0.0

        # Liquidity reward EV
        liquidity_reward_ev = our_share * params.daily_reward_rate

        # Maker rebate EV
        maker_rebate_ev = self._our_maker_share * self._daily_rebate_pool / max(1, len(self._reward_params))

        total_ev = liquidity_reward_ev + maker_rebate_ev

        # Per-share reward
        total_size = bid_size + ask_size
        reward_per_share = total_ev / total_size if total_size > 0 else 0.0

        return RewardEstimate(
            condition_id=condition_id,
            liquidity_reward_ev=liquidity_reward_ev,
            maker_rebate_ev=maker_rebate_ev,
            total_ev=total_ev,
            reward_per_share=reward_per_share,
            is_eligible=True,
            score_components=score_components,
        )

    def compute_reward_ev_for_universe(self, condition_id: str) -> float:
        """Quick reward EV estimate for universe scoring.

        Returns a simplified EV number based on daily rate and market liquidity.
        """
        params = self._reward_params.get(condition_id)
        if params is None:
            return 0.0

        if params.daily_reward_rate <= 0:
            return 0.0

        # Simplified: assume we can capture a reasonable share
        assumed_share = 0.05  # Optimistic 5% share
        return params.daily_reward_rate * assumed_share

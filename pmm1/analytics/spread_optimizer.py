"""Adaptive spread selection via Thompson Sampling.

CL-01: The bot's spread was hardcoded. This learns the optimal
base spread per market from actual fill outcomes (spread capture
minus adverse selection cost).

Uses Gaussian Thompson Sampling over discretized spread buckets.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Spread buckets in price units (0.5c to 3c)
SPREAD_BUCKETS = [0.005, 0.01, 0.015, 0.02, 0.025, 0.03]


class BucketStats:
    """Gaussian posterior for a single spread bucket."""

    __slots__ = ("mu", "sigma", "n", "ewma_reward")

    def __init__(self, prior_mu: float = 0.0, prior_sigma: float = 0.01) -> None:
        self.mu = prior_mu
        self.sigma = prior_sigma
        self.n = 0
        self.ewma_reward = 0.0

    def update(self, reward: float, decay: float = 0.95) -> None:
        """Update posterior with new observation using EWMA."""
        self.n += 1
        # EWMA for non-stationarity
        self.ewma_reward = decay * self.ewma_reward + (1 - decay) * reward
        # Online mean/variance update
        self.mu = self.ewma_reward
        # Shrink sigma as observations grow, but floor at 0.001
        self.sigma = max(0.001, self.sigma * decay)

    def sample(self) -> float:
        """Thompson sample from posterior."""
        return random.gauss(self.mu, self.sigma)

    def to_dict(self) -> dict[str, float]:
        return {"mu": self.mu, "sigma": self.sigma, "n": self.n, "ewma_reward": self.ewma_reward}

    @classmethod
    def from_dict(cls, d: dict[str, float]) -> BucketStats:
        bs = cls(prior_mu=d.get("mu", 0.0), prior_sigma=d.get("sigma", 0.01))
        bs.n = int(d.get("n", 0))
        bs.ewma_reward = d.get("ewma_reward", 0.0)
        return bs


class SpreadOptimizer:
    """Per-market Thompson Sampling spread optimizer.

    Usage:
        optimizer = SpreadOptimizer()
        # Get optimal spread for quoting:
        spread = optimizer.get_optimal_base_spread("cid_123")
        # After fill with markout data:
        optimizer.record_fill("cid_123", spread_at_fill=0.015,
                             spread_capture=0.008, adverse_selection_5s=-0.003)
    """

    def __init__(self, default_spread: float = 0.01, decay: float = 0.95) -> None:
        self.default_spread = default_spread
        self.decay = decay
        self._market_buckets: dict[str, dict[int, BucketStats]] = {}
        self._global_buckets: dict[int, BucketStats] = {
            i: BucketStats() for i in range(len(SPREAD_BUCKETS))
        }

    def _get_buckets(self, condition_id: str) -> dict[int, BucketStats]:
        if condition_id not in self._market_buckets:
            self._market_buckets[condition_id] = {
                i: BucketStats() for i in range(len(SPREAD_BUCKETS))
            }
        return self._market_buckets[condition_id]

    def _classify_bucket(self, spread: float) -> int:
        """Find the closest bucket for a given spread."""
        min_dist = float("inf")
        best = 0
        for i, bucket_spread in enumerate(SPREAD_BUCKETS):
            dist = abs(spread - bucket_spread)
            if dist < min_dist:
                min_dist = dist
                best = i
        return best

    def get_optimal_base_spread(self, condition_id: str) -> float:
        """Thompson-sample the best spread bucket for this market."""
        buckets = self._get_buckets(condition_id)

        # Need at least 3 observations per market before optimizing
        total_obs = sum(b.n for b in buckets.values())
        if total_obs < 3:
            # Use global buckets if available
            global_obs = sum(b.n for b in self._global_buckets.values())
            if global_obs < 10:
                return self.default_spread
            buckets = self._global_buckets

        # Thompson sample from each bucket, pick the best
        best_idx = max(buckets, key=lambda i: buckets[i].sample())
        return SPREAD_BUCKETS[best_idx]

    def record_fill(
        self,
        condition_id: str,
        spread_at_fill: float,
        spread_capture: float,
        adverse_selection_5s: float = 0.0,
    ) -> None:
        """Record fill outcome for learning.

        Args:
            condition_id: Market identifier
            spread_at_fill: Half-spread that was quoted when fill occurred
            spread_capture: Actual spread captured (positive = good)
            adverse_selection_5s: 5-second adverse selection (negative = bad)
        """
        reward = spread_capture + adverse_selection_5s  # Net spread after AS
        bucket_idx = self._classify_bucket(spread_at_fill)

        # Update market-specific buckets
        buckets = self._get_buckets(condition_id)
        buckets[bucket_idx].update(reward, self.decay)

        # Update global buckets
        self._global_buckets[bucket_idx].update(reward, self.decay)

    def save(self, path: str) -> None:
        """Persist to JSON."""
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            data = {
                "global": {str(k): v.to_dict() for k, v in self._global_buckets.items()},
                "markets": {
                    cid: {str(k): v.to_dict() for k, v in buckets.items()}
                    for cid, buckets in self._market_buckets.items()
                },
            }
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            Path(tmp).replace(path)
        except Exception as e:
            logger.warning("spread_optimizer_save_failed", error=str(e))

    def load(self, path: str) -> None:
        """Load from JSON."""
        try:
            p = Path(path)
            if not p.exists():
                return
            with open(p) as f:
                data = json.load(f)
            for k, v in data.get("global", {}).items():
                self._global_buckets[int(k)] = BucketStats.from_dict(v)
            for cid, buckets in data.get("markets", {}).items():
                self._market_buckets[cid] = {
                    int(k): BucketStats.from_dict(v) for k, v in buckets.items()
                }
            logger.info("spread_optimizer_loaded", markets=len(self._market_buckets))
        except Exception as e:
            logger.warning("spread_optimizer_load_failed", error=str(e))

    def get_status(self) -> dict[str, Any]:
        return {
            "tracked_markets": len(self._market_buckets),
            "global_observations": sum(b.n for b in self._global_buckets.values()),
            "global_best_bucket": SPREAD_BUCKETS[
                max(self._global_buckets, key=lambda i: self._global_buckets[i].mu)
            ] if any(b.n > 0 for b in self._global_buckets.values()) else self.default_spread,
        }

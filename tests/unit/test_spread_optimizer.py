"""Tests for SpreadOptimizer — CL-01 Thompson Sampling spread selection."""

import os
import random

from pmm1.analytics.spread_optimizer import (
    SPREAD_BUCKETS,
    BucketStats,
    SpreadOptimizer,
)


def test_default_spread_cold_start():
    """No data -> returns default spread."""
    optimizer = SpreadOptimizer(default_spread=0.015)
    spread = optimizer.get_optimal_base_spread("cid_unknown")
    assert spread == 0.015


def test_bucket_classification():
    """Various spreads map to correct buckets."""
    optimizer = SpreadOptimizer()
    # Exact matches
    assert optimizer._classify_bucket(0.005) == 0
    assert optimizer._classify_bucket(0.01) == 1
    assert optimizer._classify_bucket(0.03) == 5
    # Closest match: 0.012 is closer to 0.01 (idx 1) than 0.015 (idx 2)
    assert optimizer._classify_bucket(0.012) == 1
    # 0.0175 is closer to 0.02 (idx 3) than 0.015 (idx 2)
    assert optimizer._classify_bucket(0.0175) == 3
    # Value below range -> bucket 0
    assert optimizer._classify_bucket(0.001) == 0
    # Value above range -> bucket 5
    assert optimizer._classify_bucket(0.05) == 5


def test_record_fill_updates_stats():
    """Recording fills increases observation count."""
    optimizer = SpreadOptimizer()
    assert optimizer.get_status()["global_observations"] == 0

    optimizer.record_fill("cid_1", spread_at_fill=0.01, spread_capture=0.005)
    assert optimizer.get_status()["global_observations"] == 1

    optimizer.record_fill("cid_1", spread_at_fill=0.01, spread_capture=0.004)
    assert optimizer.get_status()["global_observations"] == 2
    assert optimizer.get_status()["tracked_markets"] == 1

    optimizer.record_fill("cid_2", spread_at_fill=0.02, spread_capture=0.01)
    assert optimizer.get_status()["tracked_markets"] == 2


def test_thompson_sampling_converges():
    """After many fills, optimizer selects best bucket.

    Bucket idx=3 (spread=0.02) consistently gets the highest reward.
    After enough observations, Thompson Sampling should favor it.
    """
    random.seed(42)
    optimizer = SpreadOptimizer(default_spread=0.01)

    # Simulate fills: bucket 0.02 (idx 3) is most profitable
    for _ in range(200):
        for i, spread in enumerate(SPREAD_BUCKETS):
            # Bucket at 0.02 gets consistently positive reward
            if spread == 0.02:
                reward = 0.008 + random.gauss(0, 0.001)
            else:
                reward = 0.001 + random.gauss(0, 0.001)
            optimizer.record_fill(
                "cid_test",
                spread_at_fill=spread,
                spread_capture=reward,
            )

    # Run multiple samples — the best bucket should dominate
    selections = [optimizer.get_optimal_base_spread("cid_test") for _ in range(100)]
    # 0.02 should be selected most often
    count_02 = selections.count(0.02)
    assert count_02 > 50, f"Expected 0.02 to dominate, got {count_02}/100"


def test_save_load_roundtrip(tmp_path):
    """Save, load, verify state preserved."""
    optimizer = SpreadOptimizer()
    optimizer.record_fill("cid_a", spread_at_fill=0.01, spread_capture=0.005)
    optimizer.record_fill("cid_a", spread_at_fill=0.02, spread_capture=0.008)
    optimizer.record_fill("cid_b", spread_at_fill=0.015, spread_capture=0.006)

    path = str(tmp_path / "spread_opt.json")
    optimizer.save(path)
    assert os.path.exists(path)

    optimizer2 = SpreadOptimizer()
    optimizer2.load(path)

    assert optimizer2.get_status()["tracked_markets"] == 2
    assert optimizer2.get_status()["global_observations"] == 3
    # Verify bucket stats survived
    buckets_a = optimizer2._get_buckets("cid_a")
    assert buckets_a[1].n == 1  # 0.01 bucket
    assert buckets_a[3].n == 1  # 0.02 bucket


def test_global_fallback():
    """Market with 0 fills uses global stats when available."""
    random.seed(42)
    optimizer = SpreadOptimizer(default_spread=0.01)

    # Build up global data via other markets (need >= 10 global observations)
    for _ in range(15):
        optimizer.record_fill(
            "cid_other",
            spread_at_fill=0.015,
            spread_capture=0.007,
        )

    # New market with no fills should use global buckets (not default)
    spread = optimizer.get_optimal_base_spread("cid_new")
    # Should return some valid spread bucket, not necessarily the default
    assert spread in SPREAD_BUCKETS


def test_bucket_stats_update():
    """BucketStats properly tracks EWMA and shrinks sigma."""
    bs = BucketStats(prior_mu=0.0, prior_sigma=0.01)
    assert bs.n == 0
    assert bs.sigma == 0.01

    bs.update(0.1, decay=0.95)
    assert bs.n == 1
    assert bs.ewma_reward > 0
    assert bs.sigma < 0.01  # Sigma shrinks

    initial_sigma = bs.sigma
    bs.update(0.1, decay=0.95)
    assert bs.n == 2
    assert bs.sigma < initial_sigma  # Continues shrinking


def test_bucket_stats_serialization():
    """BucketStats to_dict / from_dict roundtrip."""
    bs = BucketStats(prior_mu=0.5, prior_sigma=0.02)
    bs.n = 10
    bs.ewma_reward = 0.03

    d = bs.to_dict()
    bs2 = BucketStats.from_dict(d)

    assert bs2.mu == bs.mu
    assert bs2.sigma == bs.sigma
    assert bs2.n == bs.n
    assert bs2.ewma_reward == bs.ewma_reward


def test_save_atomic(tmp_path):
    """Verify .tmp file is used during save and renamed to final path."""
    optimizer = SpreadOptimizer()
    optimizer.record_fill("cid_1", spread_at_fill=0.01, spread_capture=0.005)

    path = str(tmp_path / "spread_opt.json")
    tmp_file = path + ".tmp"

    optimizer.save(path)

    assert os.path.exists(path)
    assert not os.path.exists(tmp_file)


def test_get_optimal_gamma_default():
    """Before any fills, gamma should be the default."""
    from pmm1.analytics.spread_optimizer import SpreadOptimizer
    so = SpreadOptimizer()
    gamma = so.get_optimal_gamma("test_market")
    assert gamma == so.default_gamma


def test_get_optimal_gamma_after_fills():
    """After fills, gamma should come from Thompson sampling."""
    from pmm1.analytics.spread_optimizer import GAMMA_BUCKETS, SpreadOptimizer
    so = SpreadOptimizer()
    for _ in range(20):
        so.record_fill(
            "toxic_market",
            spread_at_fill=0.015,
            spread_capture=0.003,
            adverse_selection_5s=-0.008,
            gamma_at_fill=0.04,
        )
    gamma = so.get_optimal_gamma("toxic_market")
    assert gamma in GAMMA_BUCKETS


def test_gamma_save_load_roundtrip(tmp_path):
    """Gamma buckets survive save/load."""
    from pmm1.analytics.spread_optimizer import SpreadOptimizer
    so = SpreadOptimizer()
    for _ in range(10):
        so.record_fill(
            "mkt1", spread_at_fill=0.015,
            spread_capture=0.003, adverse_selection_5s=-0.005,
            gamma_at_fill=0.04,
        )
    path = str(tmp_path / "spread_opt.json")
    so.save(path)

    so2 = SpreadOptimizer()
    so2.load(path)
    assert so2.get_optimal_gamma("mkt1") is not None


def test_default_gamma_property():
    """SpreadOptimizer has default_gamma."""
    from pmm1.analytics.spread_optimizer import SpreadOptimizer
    so = SpreadOptimizer(default_gamma=0.015)
    assert so.default_gamma == 0.015

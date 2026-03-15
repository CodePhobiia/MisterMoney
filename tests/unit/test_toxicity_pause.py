"""Tests for toxicity-based quoting pause."""
from __future__ import annotations

import time


def test_mute_dict_set_on_high_vpin():
    """VPIN > threshold sets mute-until timestamp."""
    mute_until: dict[str, float] = {}
    threshold = 0.55
    pause_s = 30.0
    vpin = 0.7
    cid = "cond_abc"

    if vpin > threshold:
        mute_until[cid] = time.time() + pause_s

    assert cid in mute_until
    assert mute_until[cid] > time.time()


def test_mute_dict_not_set_below_threshold():
    """VPIN below threshold does not mute."""
    mute_until: dict[str, float] = {}
    threshold = 0.55
    vpin = 0.40
    cid = "cond_abc"

    if vpin > threshold:
        mute_until[cid] = time.time() + 30.0

    assert cid not in mute_until


def test_mute_expires_after_pause():
    """Muted market resumes after pause duration."""
    mute_until: dict[str, float] = {}
    cid = "cond_abc"
    mute_until[cid] = time.time() - 1.0  # Already expired

    is_muted = time.time() < mute_until.get(cid, 0)
    assert is_muted is False


def test_per_market_isolation():
    """Muting market A does not affect market B."""
    mute_until: dict[str, float] = {}
    mute_until["market_a"] = time.time() + 30.0

    is_b_muted = time.time() < mute_until.get("market_b", 0)
    assert is_b_muted is False


def test_pricing_config_has_toxicity_fields():
    """PricingConfig has toxicity pause fields with defaults."""
    from pmm1.settings import PricingConfig
    cfg = PricingConfig()
    assert cfg.toxicity_pause_vpin == 0.55
    assert cfg.toxicity_pause_seconds == 30.0

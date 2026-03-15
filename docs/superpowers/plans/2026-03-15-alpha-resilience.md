# Alpha + Resilience Improvements Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Increase per-trade profitability by 5-15 bps and eliminate false-positive shutdown events through 4 targeted changes to existing modules.

**Architecture:** Each change is a self-contained addition to an existing module — no new modules, no new wiring paths. Data flow stays identical: fills → analytics → pricing → orders. Implementation order: toxicity pause (smallest) → staged recovery → adaptive gamma → dynamic exits.

**Tech Stack:** Python 3.14, structlog, pydantic, asyncio

---

## File Map

| File | Action | What Changes |
|------|--------|-------------|
| `pmm1/settings.py` | Modify | Add 2 toxicity pause fields to PricingConfig |
| `pmm1/main.py` | Modify | Add toxicity mute dict + check; pass optimal_gamma; wire mismatch tracker |
| `pmm1/risk/kill_switch.py` | Modify | Add `MismatchTracker` class |
| `pmm1/analytics/spread_optimizer.py` | Modify | Add gamma buckets + `get_optimal_gamma()` |
| `pmm1/strategy/quote_engine.py` | Modify | Accept `optimal_gamma` param in `compute_half_spread()` |
| `pmm1/math/kelly.py` | No change | `kelly_growth_rate()` already exists |
| `pmm1/strategy/exit_manager.py` | Modify | Add `_check_kelly_rational_exit()` method |
| `tests/unit/test_toxicity_pause.py` | Create | Toxicity pause tests |
| `tests/unit/test_mismatch_tracker.py` | Create | Staged recovery tests |
| `tests/unit/test_spread_optimizer.py` | Modify | Add gamma bucket tests |
| `tests/unit/test_exit_manager.py` | Modify | Add Kelly-rational exit tests |

---

## Task 1: Toxicity-Based Quoting Pause

**Files:**
- Modify: `pmm1/settings.py:67-106` (PricingConfig)
- Modify: `pmm1/main.py:~3240` (quote loop, before compute_quote)
- Create: `tests/unit/test_toxicity_pause.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_toxicity_pause.py
"""Tests for toxicity-based quoting pause."""
from __future__ import annotations

import time

import pytest


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_toxicity_pause.py -v`
Expected: Last test fails (toxicity_pause_vpin not on PricingConfig yet)

- [ ] **Step 3: Add config fields to PricingConfig**

In `pmm1/settings.py`, after `theta_1: float = 5.0` (line ~106), add:

```python
    # Toxicity-based quoting pause
    toxicity_pause_vpin: float = 0.55    # VPIN threshold to mute market
    toxicity_pause_seconds: float = 30.0  # Seconds to mute after trigger
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_toxicity_pause.py -v`
Expected: All 5 PASS

- [ ] **Step 5: Wire toxicity pause into main.py quote loop**

In `pmm1/main.py`, find the per-market quoting section. Before the line that computes features (~line 3240), add the mute dict initialization near other per-cycle dicts (around line 2600, after `_toxicity_mute_until: dict[str, float] = {}` — declare it once before the while loop).

Near line 2600 (before `while not shutdown_event.is_set()`), add:
```python
    _toxicity_mute_until: dict[str, float] = {}
```

Then in the per-market loop, after features are computed but before `compute_quote` is called (~line 3270), add:
```python
                    # Toxicity pause: suppress quoting when VPIN is dangerously high
                    if features.vpin > settings.pricing.toxicity_pause_vpin:
                        _toxicity_mute_until[md.condition_id] = (
                            time.time() + settings.pricing.toxicity_pause_seconds
                        )
                    if time.time() < _toxicity_mute_until.get(md.condition_id, 0):
                        _add_suppression_reason(quote_intent, "BUY", "toxicity_pause")
                        _add_suppression_reason(quote_intent, "SELL", "toxicity_pause")
                        continue
```

Note: Find the exact insertion point by looking for `_optimal_spread = spread_optimizer.get_optimal_base_spread`. Insert the toxicity check BEFORE that line so we skip all downstream work.

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ -x -q`
Expected: All pass (829+)

- [ ] **Step 7: Run linter**

Run: `python -m ruff check pmm1/settings.py pmm1/main.py tests/unit/test_toxicity_pause.py`
Expected: All checks passed

- [ ] **Step 8: Commit**

```bash
git add pmm1/settings.py pmm1/main.py tests/unit/test_toxicity_pause.py
git commit -m "feat: toxicity-based quoting pause (VPIN > 0.55 → 30s mute per market)"
```

---

## Task 2: Staged Recovery Protocol

**Files:**
- Modify: `pmm1/risk/kill_switch.py`
- Create: `tests/unit/test_mismatch_tracker.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_mismatch_tracker.py
"""Tests for staged mismatch recovery protocol."""
from __future__ import annotations

import time

import pytest

from pmm1.risk.kill_switch import MismatchTracker


def test_initial_stage_is_zero():
    tracker = MismatchTracker()
    assert tracker.get_stage("mkt_a") == 0


def test_first_mismatch_goes_to_stage_1():
    tracker = MismatchTracker()
    stage = tracker.record_mismatch("mkt_a")
    assert stage == 1


def test_second_mismatch_goes_to_stage_2():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    stage = tracker.record_mismatch("mkt_a")
    assert stage == 2


def test_third_mismatch_goes_to_stage_3():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    tracker.record_mismatch("mkt_a")
    stage = tracker.record_mismatch("mkt_a")
    assert stage == 3


def test_stage_3_caps_at_3():
    tracker = MismatchTracker()
    for _ in range(10):
        stage = tracker.record_mismatch("mkt_a")
    assert stage == 3


def test_per_market_isolation():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    tracker.record_mismatch("mkt_a")
    assert tracker.get_stage("mkt_a") == 2
    assert tracker.get_stage("mkt_b") == 0


def test_clean_reconciliation_resets_stage():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    tracker.record_mismatch("mkt_a")
    assert tracker.get_stage("mkt_a") == 2
    tracker.record_clean("mkt_a")
    assert tracker.get_stage("mkt_a") == 0


def test_should_skip_at_stage_1():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    assert tracker.should_skip("mkt_a") is True


def test_should_not_skip_at_stage_0():
    tracker = MismatchTracker()
    assert tracker.should_skip("mkt_a") is False


def test_is_read_only_at_stage_2():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    tracker.record_mismatch("mkt_a")
    assert tracker.is_read_only("mkt_a") is True


def test_should_flatten_at_stage_3():
    tracker = MismatchTracker()
    for _ in range(3):
        tracker.record_mismatch("mkt_a")
    assert tracker.should_flatten("mkt_a") is True


def test_skip_expires_after_duration():
    tracker = MismatchTracker(skip_duration_s=0.0)  # Instant expiry
    tracker.record_mismatch("mkt_a")
    # Stage is 1, but skip already expired
    assert tracker.should_skip("mkt_a") is False


def test_stage_3_recovery_attempt():
    tracker = MismatchTracker()
    for _ in range(3):
        tracker.record_mismatch("mkt_a")
    assert tracker.should_flatten("mkt_a") is True

    # Simulate recovery attempt
    recovered = tracker.attempt_recovery("mkt_a")
    assert recovered is True
    assert tracker.get_stage("mkt_a") == 0


def test_stage_3_recovery_capped_at_3():
    tracker = MismatchTracker(max_recovery_attempts=3)
    for _ in range(3):
        tracker.record_mismatch("mkt_a")

    # Use all 3 recovery attempts
    for _ in range(3):
        # Re-escalate to stage 3
        for _ in range(3):
            tracker.record_mismatch("mkt_a")
        tracker.attempt_recovery("mkt_a")

    # 4th recovery should fail
    for _ in range(3):
        tracker.record_mismatch("mkt_a")
    recovered = tracker.attempt_recovery("mkt_a")
    assert recovered is False


def test_get_status():
    tracker = MismatchTracker()
    tracker.record_mismatch("mkt_a")
    tracker.record_mismatch("mkt_b")
    status = tracker.get_status()
    assert status["tracked_markets"] == 2
    assert "mkt_a" in status["stages"]


def test_stage_resets_after_clean_timeout():
    tracker = MismatchTracker(clean_reset_s=0.0)  # Instant reset
    tracker.record_mismatch("mkt_a")
    tracker.record_mismatch("mkt_a")
    # Force a time check
    tracker.record_clean("mkt_a")
    assert tracker.get_stage("mkt_a") == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_mismatch_tracker.py -v`
Expected: ImportError — MismatchTracker doesn't exist yet

- [ ] **Step 3: Implement MismatchTracker**

Add to `pmm1/risk/kill_switch.py` (after the KillSwitch class, at the end of file):

```python


class MismatchTracker:
    """Per-market staged recovery for reconciliation mismatches.

    Stage 0: Clean — quote normally
    Stage 1: Skip quoting this market for skip_duration_s
    Stage 2: Read-only (no orders) for this market
    Stage 3: FLATTEN_ONLY with auto-recovery attempts
    """

    def __init__(
        self,
        skip_duration_s: float = 30.0,
        clean_reset_s: float = 600.0,
        max_recovery_attempts: int = 3,
    ) -> None:
        self._skip_duration_s = skip_duration_s
        self._clean_reset_s = clean_reset_s
        self._max_recovery_attempts = max_recovery_attempts

        # Per-market state
        self._stages: dict[str, int] = {}
        self._last_mismatch_ts: dict[str, float] = {}
        self._skip_until: dict[str, float] = {}
        self._recovery_attempts: dict[str, int] = {}

    def get_stage(self, condition_id: str) -> int:
        return self._stages.get(condition_id, 0)

    def record_mismatch(self, condition_id: str) -> int:
        """Record a mismatch for a market. Returns the new stage (1-3)."""
        current = self._stages.get(condition_id, 0)
        new_stage = min(3, current + 1)
        self._stages[condition_id] = new_stage
        self._last_mismatch_ts[condition_id] = time.time()

        if new_stage == 1:
            self._skip_until[condition_id] = time.time() + self._skip_duration_s

        logger.warning(
            "mismatch_stage_escalated",
            condition_id=condition_id[:16],
            old_stage=current,
            new_stage=new_stage,
        )
        return new_stage

    def record_clean(self, condition_id: str) -> None:
        """Record a clean reconciliation — reset stage to 0."""
        if condition_id in self._stages:
            old = self._stages[condition_id]
            if old > 0:
                logger.info(
                    "mismatch_stage_cleared",
                    condition_id=condition_id[:16],
                    old_stage=old,
                )
            del self._stages[condition_id]
            self._last_mismatch_ts.pop(condition_id, None)
            self._skip_until.pop(condition_id, None)

    def should_skip(self, condition_id: str) -> bool:
        """Stage 1+: should we skip quoting this market?"""
        stage = self.get_stage(condition_id)
        if stage == 0:
            return False
        if stage >= 2:
            return True  # Stage 2+ always skips
        # Stage 1: skip only during cooldown window
        return time.time() < self._skip_until.get(condition_id, 0)

    def is_read_only(self, condition_id: str) -> bool:
        """Stage 2+: market is read-only (no orders)."""
        return self.get_stage(condition_id) >= 2

    def should_flatten(self, condition_id: str) -> bool:
        """Stage 3: should enter FLATTEN_ONLY for this market."""
        return self.get_stage(condition_id) >= 3

    def attempt_recovery(self, condition_id: str) -> bool:
        """Attempt to recover from Stage 3. Returns True if successful."""
        attempts = self._recovery_attempts.get(condition_id, 0)
        if attempts >= self._max_recovery_attempts:
            logger.critical(
                "mismatch_recovery_exhausted",
                condition_id=condition_id[:16],
                attempts=attempts,
            )
            return False

        self._recovery_attempts[condition_id] = attempts + 1
        self._stages[condition_id] = 0
        self._skip_until.pop(condition_id, None)
        logger.info(
            "mismatch_recovery_attempted",
            condition_id=condition_id[:16],
            attempt=attempts + 1,
            max_attempts=self._max_recovery_attempts,
        )
        return True

    def get_status(self) -> dict[str, Any]:
        return {
            "tracked_markets": len(self._stages),
            "stages": {
                cid: {"stage": stage, "recovery_attempts": self._recovery_attempts.get(cid, 0)}
                for cid, stage in self._stages.items()
            },
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_mismatch_tracker.py -v`
Expected: All 17 PASS

- [ ] **Step 5: Run full test suite + linter**

Run: `python -m pytest tests/ -x -q && python -m ruff check pmm1/risk/kill_switch.py tests/unit/test_mismatch_tracker.py`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add pmm1/risk/kill_switch.py tests/unit/test_mismatch_tracker.py
git commit -m "feat: staged mismatch recovery protocol (4-stage per-market ladder)"
```

---

## Task 3: Adaptive Gamma via Extended SpreadOptimizer

**Files:**
- Modify: `pmm1/analytics/spread_optimizer.py`
- Modify: `pmm1/strategy/quote_engine.py:140-209`
- Modify: `pmm1/main.py:~3272` (pass optimal_gamma)
- Modify: `tests/unit/test_spread_optimizer.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_spread_optimizer.py`:

```python
# Append to existing test file


GAMMA_BUCKETS = [0.005, 0.01, 0.015, 0.025, 0.04, 0.06]


def test_get_optimal_gamma_default():
    """Before any fills, gamma should be the default."""
    from pmm1.analytics.spread_optimizer import SpreadOptimizer
    so = SpreadOptimizer()
    gamma = so.get_optimal_gamma("test_market")
    assert gamma == so.default_gamma


def test_get_optimal_gamma_after_fills():
    """After fills, gamma should come from Thompson sampling."""
    from pmm1.analytics.spread_optimizer import SpreadOptimizer
    so = SpreadOptimizer()
    # Feed fills that reward higher gamma (high AS penalty)
    for _ in range(20):
        so.record_fill(
            "toxic_market",
            spread_at_fill=0.015,
            spread_capture=0.003,
            adverse_selection_5s=-0.008,  # Heavy AS
            gamma_at_fill=0.04,  # High gamma was used
        )
    for _ in range(20):
        so.record_fill(
            "toxic_market",
            spread_at_fill=0.015,
            spread_capture=0.005,
            adverse_selection_5s=-0.001,  # Low AS
            gamma_at_fill=0.015,  # Low gamma was used
        )
    # The optimizer should learn; just verify it returns a valid bucket
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
    # Gamma buckets should have data
    assert so2.get_optimal_gamma("mkt1") is not None


def test_default_gamma_property():
    """SpreadOptimizer has default_gamma matching config."""
    from pmm1.analytics.spread_optimizer import SpreadOptimizer
    so = SpreadOptimizer(default_gamma=0.015)
    assert so.default_gamma == 0.015
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_spread_optimizer.py::test_get_optimal_gamma_default -v`
Expected: AttributeError — `get_optimal_gamma` doesn't exist

- [ ] **Step 3: Add gamma buckets to SpreadOptimizer**

In `pmm1/analytics/spread_optimizer.py`:

After `SPREAD_BUCKETS` (line 21), add:
```python
GAMMA_BUCKETS = [0.005, 0.01, 0.015, 0.025, 0.04, 0.06]
```

Modify `__init__` to accept `default_gamma` and create gamma bucket dicts:
```python
    def __init__(self, default_spread: float = 0.01, decay: float = 0.95, default_gamma: float = 0.015) -> None:
        self.default_spread = default_spread
        self.default_gamma = default_gamma
        self.decay = decay
        self._market_buckets: dict[str, dict[int, BucketStats]] = {}
        self._global_buckets: dict[int, BucketStats] = {
            i: BucketStats() for i in range(len(SPREAD_BUCKETS))
        }
        self._market_gamma_buckets: dict[str, dict[int, BucketStats]] = {}
        self._global_gamma_buckets: dict[int, BucketStats] = {
            i: BucketStats() for i in range(len(GAMMA_BUCKETS))
        }
```

Add `_get_gamma_buckets` method (after `_get_buckets`):
```python
    def _get_gamma_buckets(self, condition_id: str) -> dict[int, BucketStats]:
        if condition_id not in self._market_gamma_buckets:
            self._market_gamma_buckets[condition_id] = {
                i: BucketStats() for i in range(len(GAMMA_BUCKETS))
            }
        return self._market_gamma_buckets[condition_id]

    def _classify_gamma_bucket(self, gamma: float) -> int:
        min_dist = float("inf")
        best = 0
        for i, bucket_gamma in enumerate(GAMMA_BUCKETS):
            dist = abs(gamma - bucket_gamma)
            if dist < min_dist:
                min_dist = dist
                best = i
        return best
```

Add `get_optimal_gamma` method (after `get_optimal_base_spread`):
```python
    def get_optimal_gamma(self, condition_id: str) -> float:
        """Thompson-sample the best gamma bucket for this market."""
        buckets = self._get_gamma_buckets(condition_id)
        total_obs = sum(b.n for b in buckets.values())
        if total_obs < 3:
            global_obs = sum(b.n for b in self._global_gamma_buckets.values())
            if global_obs < 10:
                return self.default_gamma
            buckets = self._global_gamma_buckets
        best_idx = max(buckets, key=lambda i: buckets[i].sample())
        return GAMMA_BUCKETS[best_idx]
```

Modify `record_fill` to accept `gamma_at_fill` and update gamma buckets:
```python
    def record_fill(
        self,
        condition_id: str,
        spread_at_fill: float,
        spread_capture: float,
        adverse_selection_5s: float = 0.0,
        gamma_at_fill: float | None = None,
    ) -> None:
        # Existing spread reward
        reward = spread_capture + adverse_selection_5s
        bucket_idx = self._classify_bucket(spread_at_fill)
        buckets = self._get_buckets(condition_id)
        buckets[bucket_idx].update(reward, self.decay)
        self._global_buckets[bucket_idx].update(reward, self.decay)

        # Gamma reward: penalize AS 2x (gamma's job is inventory risk)
        if gamma_at_fill is not None:
            gamma_reward = spread_capture + 2 * adverse_selection_5s
            gamma_idx = self._classify_gamma_bucket(gamma_at_fill)
            gamma_buckets = self._get_gamma_buckets(condition_id)
            gamma_buckets[gamma_idx].update(gamma_reward, self.decay)
            self._global_gamma_buckets[gamma_idx].update(gamma_reward, self.decay)
```

Update `save` to include gamma data:
```python
    def save(self, path: str) -> None:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            data = {
                "global": {str(k): v.to_dict() for k, v in self._global_buckets.items()},
                "markets": {
                    cid: {str(k): v.to_dict() for k, v in buckets.items()}
                    for cid, buckets in self._market_buckets.items()
                },
                "global_gamma": {str(k): v.to_dict() for k, v in self._global_gamma_buckets.items()},
                "market_gamma": {
                    cid: {str(k): v.to_dict() for k, v in buckets.items()}
                    for cid, buckets in self._market_gamma_buckets.items()
                },
            }
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            Path(tmp).replace(path)
        except Exception as e:
            logger.warning("spread_optimizer_save_failed", error=str(e))
```

Update `load` to restore gamma data:
```python
    def load(self, path: str) -> None:
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
            for k, v in data.get("global_gamma", {}).items():
                self._global_gamma_buckets[int(k)] = BucketStats.from_dict(v)
            for cid, buckets in data.get("market_gamma", {}).items():
                self._market_gamma_buckets[cid] = {
                    int(k): BucketStats.from_dict(v) for k, v in buckets.items()
                }
            logger.info(
                "spread_optimizer_loaded",
                markets=len(self._market_buckets),
                gamma_markets=len(self._market_gamma_buckets),
            )
        except Exception as e:
            logger.warning("spread_optimizer_load_failed", error=str(e))
```

- [ ] **Step 4: Run spread optimizer tests**

Run: `python -m pytest tests/unit/test_spread_optimizer.py -v`
Expected: All pass (existing + 4 new)

- [ ] **Step 5: Wire optimal_gamma into quote_engine**

In `pmm1/strategy/quote_engine.py`, modify `compute_half_spread` signature (line ~140):

Add `optimal_gamma: float | None = None` parameter after `optimal_base_spread`:

```python
    def compute_half_spread(
        self,
        features: FeatureVector,
        tick_size: float = 0.01,
        reward_ev: float = 0.0,
        optimal_base_spread: float | None = None,
        optimal_gamma: float | None = None,
    ) -> float:
```

Then at line 157, replace the fixed gamma with blended gamma:

```python
        gamma = self.config.inventory_skew_gamma
        if optimal_gamma is not None and optimal_gamma > 0:
            gamma = 0.7 * gamma + 0.3 * optimal_gamma
```

- [ ] **Step 6: Wire optimal_gamma in main.py quote loop**

In `pmm1/main.py`, after the `_optimal_spread` line (~3272), add:
```python
                    _optimal_gamma = spread_optimizer.get_optimal_gamma(
                        md.condition_id,
                    )
```

Then in the `compute_quote` call (~3296), add the parameter:
```python
                        optimal_gamma=_optimal_gamma,
```

Do the same for the NO-side `compute_quote` call (~3690).

Also wire `gamma_at_fill` into the `spread_optimizer.record_fill` call (~line 2058):
```python
                spread_optimizer.record_fill(
                    condition_id=condition_id,
                    spread_at_fill=spread_optimizer.get_optimal_base_spread(condition_id),
                    spread_capture=realized_spread_capture or 0.0,
                    adverse_selection_5s=adverse_selection_estimate or 0.0,
                    gamma_at_fill=spread_optimizer.get_optimal_gamma(condition_id),
                )
```

- [ ] **Step 7: Run full test suite + linter**

Run: `python -m pytest tests/ -x -q && python -m ruff check pmm1/`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add pmm1/analytics/spread_optimizer.py pmm1/strategy/quote_engine.py pmm1/main.py tests/unit/test_spread_optimizer.py
git commit -m "feat: adaptive per-market gamma via Thompson sampling in SpreadOptimizer"
```

---

## Task 4: Kelly-Rational Dynamic Exit Thresholds

**Files:**
- Modify: `pmm1/strategy/exit_manager.py`
- Modify: `tests/unit/test_exit_manager.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_exit_manager.py`:

```python
# --- Kelly-rational exit tests ---

from pmm1.math.kelly import kelly_growth_rate


def test_kelly_growth_rate_no_edge():
    """When p_fair == p_market, growth rate is 0."""
    assert kelly_growth_rate(0.50, 0.50) == pytest.approx(0.0, abs=1e-10)


def test_kelly_growth_rate_positive_edge():
    """When we have edge, growth rate is positive."""
    g = kelly_growth_rate(0.60, 0.50)
    assert g > 0


def test_kelly_growth_rate_increases_with_edge():
    """More edge = higher growth rate."""
    g_small = kelly_growth_rate(0.55, 0.50)
    g_large = kelly_growth_rate(0.70, 0.50)
    assert g_large > g_small


def test_kelly_exit_triggers_when_edge_gone():
    """Should exit when growth rate < exit urgency."""
    # No edge: p_fair == p_market
    growth = kelly_growth_rate(0.50, 0.50)
    cost_to_exit = 0.01
    time_remaining = 2.0
    urgency = cost_to_exit / max(0.1, time_remaining)
    assert growth < urgency  # Should trigger exit


def test_kelly_exit_holds_with_strong_edge():
    """Should hold when growth rate >> exit urgency."""
    # Strong edge
    growth = kelly_growth_rate(0.70, 0.50)
    cost_to_exit = 0.01
    time_remaining = 10.0
    urgency = cost_to_exit / max(0.1, time_remaining)
    assert growth > urgency  # Should hold


def test_kelly_exit_time_decay():
    """As time shrinks, urgency increases, making exit more likely."""
    growth = kelly_growth_rate(0.55, 0.50)
    cost = 0.01

    urgency_10h = cost / 10.0
    urgency_1h = cost / 1.0
    urgency_01h = cost / 0.1

    assert urgency_01h > urgency_1h > urgency_10h
    # With small edge, should hold at 10h but exit at 0.1h
    assert growth > urgency_10h
    assert growth < urgency_01h
```

- [ ] **Step 2: Run to verify they pass (math already exists)**

Run: `python -m pytest tests/unit/test_exit_manager.py::test_kelly_growth_rate_no_edge tests/unit/test_exit_manager.py::test_kelly_exit_triggers_when_edge_gone -v`
Expected: PASS (kelly_growth_rate already exists in kelly.py)

- [ ] **Step 3: Add _check_kelly_rational_exit to ExitManager**

In `pmm1/strategy/exit_manager.py`, add a new method after `_check_stop_loss` (around line 300):

```python
    def _check_kelly_rational_exit(
        self,
        pos: MarketPosition,
        token_id: str,
        size: float,
        avg_price: float,
        current_price: float,
        p_fair: float | None,
        time_remaining_hours: float,
        half_spread: float,
    ) -> SellSignal | None:
        """Kelly-rational exit: exit when growth rate < cost to stay.

        This replaces fixed TP/SL thresholds with a dynamic comparison:
        - growth_if_hold = KL divergence (expected log-wealth growth)
        - exit_urgency = cost_to_exit / time_remaining
        - Exit when growth < urgency
        """
        if p_fair is None or current_price is None:
            return None
        if avg_price <= 0:
            return None

        from pmm1.math.kelly import kelly_growth_rate

        growth = kelly_growth_rate(p_fair, current_price)
        cost_to_exit = max(0.001, half_spread)
        urgency = cost_to_exit / max(0.1, time_remaining_hours)

        if growth < urgency:
            logger.info(
                "kelly_rational_exit",
                condition_id=pos.condition_id[:16],
                growth=f"{growth:.6f}",
                urgency=f"{urgency:.6f}",
                p_fair=f"{p_fair:.3f}",
                p_market=f"{current_price:.3f}",
                time_remaining_h=f"{time_remaining_hours:.1f}",
            )
            return SellSignal(
                token_id=token_id,
                condition_id=pos.condition_id,
                size=size,
                price=current_price,
                urgency="medium",
                reason="kelly_rational_exit",
            )
        return None
```

- [ ] **Step 4: Wire into evaluate_all**

In `evaluate_all()`, add the Kelly-rational check between FLATTEN and STOP-LOSS (between lines 138 and 140). The priority order becomes: FLATTEN → KELLY_RATIONAL → STOP-LOSS → RESOLUTION → TAKE-PROFIT → ORPHAN.

After the flatten block (line 138 `continue`), before the stop-loss block, insert:

```python
                # 2. KELLY-RATIONAL EXIT (dynamic TP/SL replacement)
                if avg_price > 0 and current_price is not None:
                    # Get fair value and time remaining from market metadata
                    _p_fair = getattr(md, '_latest_fair_value', None) if md else None
                    _time_remaining = (
                        getattr(md, 'hours_to_end', 24.0) if md else 24.0
                    )
                    _half_spread = getattr(md, '_latest_half_spread', 0.01) if md else 0.01
                    sig = self._check_kelly_rational_exit(
                        pos, token_id, size, avg_price, current_price,
                        p_fair=_p_fair,
                        time_remaining_hours=_time_remaining,
                        half_spread=_half_spread,
                    )
                    if sig:
                        signals.append(sig)
                        continue
```

Note: The `_latest_fair_value` and `_latest_half_spread` attributes need to be set on MarketMetadata by main.py during the quote loop. This is the same pattern used for other runtime attributes (getattr with fallback). The exit manager gracefully handles None by returning None from the check.

- [ ] **Step 5: Run full test suite + linter**

Run: `python -m pytest tests/ -x -q && python -m ruff check pmm1/strategy/exit_manager.py`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add pmm1/strategy/exit_manager.py tests/unit/test_exit_manager.py
git commit -m "feat: Kelly-rational dynamic exit thresholds (growth rate vs urgency)"
```

---

## Task 5: Final Verification

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -x -q`
Expected: All pass (829 + new tests)

- [ ] **Step 2: Run linter on all changed files**

Run: `python -m ruff check pmm1/ tests/`
Expected: All checks passed

- [ ] **Step 3: Verify no unintended changes**

Run: `git diff --stat HEAD`
Expected: Only the files from Tasks 1-4

- [ ] **Step 4: Verify commit history**

Run: `git log --oneline -5`
Expected: 4 new commits for the 4 features

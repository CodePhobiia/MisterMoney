# Sprint 8 Calibration & Attribution — Integration Guide

## What Was Built

Sprint 8 adds a complete calibration and attribution system for tracking and improving PMM-2 performance:

### Modules Created (1,256 lines total)

1. **`reward_tracker.py`** (181 lines)
   - Tracks liquidity reward estimates vs actuals
   - Records scoring snapshots (how many orders are scoring)
   - Computes EMA-based correction factors
   - Alerts when reward capture efficiency drops below 50%

2. **`rebate_tracker.py`** (192 lines)
   - Tracks maker rebate estimates vs actuals
   - Fetches daily rebates from RewardsClient API
   - Compares against internal estimates from market_score table
   - Updates correction factors via EMA (14-day half-life)

3. **`fill_calibrator.py`** (209 lines)
   - Computes actual fill rates from fill_record + queue_state history
   - Compares predicted vs actual fill probabilities
   - Computes Brier score for calibration quality
   - Adjusts FillHazard parameters (lambda, kappa) to improve accuracy

4. **`toxicity_fitter.py`** (224 lines)
   - Fits optimal toxicity weights from fill markout data
   - Uses OLS regression (numpy if available, else simple correlation)
   - Constraints: all weights >= 0, sum to 1
   - Default weights: (0.5, 0.3, 0.2) for (1s, 5s, 30s) markouts
   - Requires minimum 50 fills with complete markout data

5. **`attribution.py`** (322 lines)
   - Daily PnL decomposition into components:
     - Spread capture (execution vs mid)
     - Arb profits (from market_score EV)
     - Liquidity rewards (from reward_actual)
     - Maker rebates (from rebate_actual)
     - Toxicity losses (weighted markouts)
     - Gas costs (fill fees)
     - Net PnL (sum of all)
   - Generates Telegram-friendly daily summaries
   - Tracks key metrics:
     - Fill count, markets traded
     - Scoring uptime %
     - Reward/rebate capture efficiency

6. **`runner.py`** (107 lines)
   - Top-level orchestrator for daily calibration cycle
   - Runs 5-step process:
     1. Pull realized rewards/rebates from API
     2. Update correction factors (EMA)
     3. Calibrate fill probabilities
     4. Fit toxicity weights (if enough data)
     5. Generate and send attribution report

7. **`__init__.py`** (21 lines)
   - Exports all public classes
   - Clean module interface

## Database Schema (Already Exists)

The calibration system uses these tables from `pmm1/storage/schema.sql`:

- `fill_record` — fills with markout tracking
- `reward_actual` — daily realized vs estimated rewards
- `rebate_actual` — daily realized vs estimated rebates
- `scoring_history` — order scoring snapshots
- `market_score` — EV estimates from scorer
- `queue_state` — order queue snapshots

## Integration Points

### 1. Daily Loop (Needs to be Added)

The `CalibrationRunner.run_daily_calibration(date)` should be called **once per day** from a new daily loop in `pmm2/runtime/loops.py`.

**Suggested implementation:**

```python
# In PMM2Runtime.__init__
from pmm2.calibration.runner import CalibrationRunner

self.calibration_runner = CalibrationRunner(
    db=self.db,
    fill_hazard=self.fill_hazard,
    maker_address=config.maker_address,  # Add to PMM2Config
    rewards_client=rewards_client  # Pass from main.py
)

# Add daily loop method
async def _daily_loop(self, bot_state, settings):
    """Daily calibration and attribution.
    
    Runs once per day at 00:00 UTC:
    - Pull realized rewards/rebates
    - Update correction factors
    - Calibrate fill probabilities
    - Fit toxicity weights
    - Generate and send attribution report
    """
    import datetime
    
    last_run_date = None
    
    while not bot_state.shutdown_requested:
        try:
            now = datetime.datetime.utcnow()
            today = now.strftime("%Y-%m-%d")
            
            # Run once per day at 00:00 UTC (or on first boot)
            if last_run_date != today and now.hour == 0:
                logger.info("pmm2_daily_calibration_start", date=today)
                
                yesterday = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                await self.calibration_runner.run_daily_calibration(yesterday)
                
                last_run_date = today
            
        except Exception as e:
            logger.error("pmm2_daily_loop_error", error=str(e), exc_info=True)
        
        # Check every 5 minutes
        await asyncio.sleep(300)

# Add to start() method
asyncio.create_task(self._daily_loop(bot_state, settings)),
```

### 2. Reward/Rebate Recording (Ongoing)

The calibration system needs actual reward/rebate data to compute capture efficiency:

- **Rewards:** Call `reward_tracker.record_reward_actual()` when daily rewards are distributed
- **Rebates:** Automatically fetched via `RebateTracker.fetch_and_record_daily()` (calls RewardsClient API)

### 3. Fill Probability Calibration

The `FillCalibrator` adjusts `FillHazard` parameters dynamically:

- Modifies `fill_hazard.default_depletion_rate` (lambda correction)
- Modifies `fill_hazard.kappa` (queue scaling)
- These adjustments improve fill probability accuracy over time

### 4. Toxicity Weights

Once fitted, toxicity weights can be used by the scorer:

```python
# In MarketEVScorer or ToxicityCostEstimator
weights = calibration_runner.toxicity_fitter.fitted_weights
w_1s, w_5s, w_30s = weights
# Use these weights instead of hardcoded (0.5, 0.3, 0.2)
```

## Testing

### Import Test (Already Passed)
```bash
python3 -c "from pmm2.calibration.runner import CalibrationRunner; from pmm2.calibration.attribution import AttributionEngine; print('OK')"
```

### Manual Test Run (After Integration)
```python
import asyncio
from pmm1.storage.database import Database
from pmm2.queue.hazard import FillHazard
from pmm2.calibration.runner import CalibrationRunner

async def test():
    db = Database("data/pmm1.db")
    await db.init()
    
    fill_hazard = FillHazard()
    runner = CalibrationRunner(
        db=db,
        fill_hazard=fill_hazard,
        maker_address="0xYOUR_ADDRESS"
    )
    
    # Run calibration for yesterday
    import datetime
    yesterday = (datetime.datetime.utcnow() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    await runner.run_daily_calibration(yesterday)
    
    await db.close()

asyncio.run(test())
```

## Next Steps

1. **Add daily loop to PMM2Runtime** (see integration point #1 above)
2. **Add `maker_address` to PMM2Config** (for rebate tracking)
3. **Pass RewardsClient to PMM2Runtime** (for rebate fetching)
4. **Test with live data** (requires at least 1 day of fills for attribution, 7 days for toxicity fitting)
5. **Monitor Telegram reports** (daily summaries will auto-send to chat_id `7916400037`)

## Dependencies

- `structlog` — logging (already installed)
- `aiosqlite` — async SQLite (already installed)
- `aiohttp` — for Telegram notifications (already installed)
- `numpy` — optional, for better OLS fitting (install with `pip install numpy`)

If numpy is not available, the toxicity fitter falls back to simple correlation-based weighting.

## Files Modified

None! All code is new in `pmm2/calibration/`.

## Git Commit

```
commit 1b6d9b2
Sprint 8: Add calibration and attribution system

- reward_tracker.py: Track and calibrate liquidity reward estimates vs actuals
- rebate_tracker.py: Track maker rebate estimates vs actuals
- fill_calibrator.py: Calibrate fill probability estimates against actual fill rates
- toxicity_fitter.py: Fit toxicity weights from fill markout data using OLS
- attribution.py: Daily PnL decomposition (spread, arb, rewards, rebates, toxicity, gas)
- runner.py: Orchestrate daily calibration cycle
- All modules implement database queries and EMA-based correction factors
- Telegram reporting for daily attribution summaries

7 files changed, 1256 insertions(+)
```

Pushed to: https://github.com/CodePhobiia/MisterMoney.git

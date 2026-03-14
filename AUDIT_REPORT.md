# MisterMoney Pre-Deployment Audit Report

## Independent Quantitative Systems Review

**Audit Date**: 2026-03-08
**Codebase**: 39,875 lines across 206 Python files
**Audit Team**: 6 independent auditors (Microstructure, Risk, Quant, Systems, Red Team, Completeness)
**Verdict**: NOT READY for live capital at any NAV level

---

## Summary

| Auditor | Findings | Blockers |
|---------|----------|----------|
| 1. Microstructure & Execution | 20 | 3 |
| 2. Risk & PnL | 17 | 6 |
| 3. Quantitative Models | 20 | 4 |
| 4. Systems & Architecture | 27 | 5 |
| 5. Adversarial / Red Team | 14 | 4 |
| 6. Gap-Finding & Completeness | 32 | 8 |
| **TOTAL** | **130** | **30** |

---

## Auditor 1: Microstructure & Execution (20 Findings)

### E-01 [CRITICAL] Cancel Failure Falls Through to Submission — Double Exposure

**Root Cause**: In `OrderManager.diff_and_apply()` (`pmm1/execution/order_manager.py:265-268`), when cancellation throws a generic `Exception`, the code logs the error but does **not return**. Only specific CLOB exceptions (`ClobRestartError`, `ClobPausedError`, `ClobRateLimitError`, `ClobAuthError`) trigger a `return`. The generic exception handler falls through to the order submission block below.

```python
except Exception as e:
    results["errors"].append(f"cancel_unexpected: {e}")
    logger.error("order_cancel_error", error=str(e))
    # NO return — falls through to submit block
```

**Why It Matters**: If a cancel REST call fails for a transient reason (timeout, DNS, TCP reset), old orders stay live on the exchange while new orders are submitted. The bot ends up double-exposed: stale quotes at old prices plus new quotes at current prices. With 12 markets quoting both YES and NO (48 orders), at $15/order, that is up to $720 of unintended exposure. In a fast-moving market this produces 5-15% NAV loss per event.

**Proposed Fix**: Add `return results` after the generic exception handler, identical to the specific exception handlers above it. The principle is fail-safe: if cancellation fails for any reason, do not proceed to submit new orders.

```python
except Exception as e:
    results["errors"].append(f"cancel_unexpected: {e}")
    logger.error("order_cancel_error", error=str(e))
    return results  # <-- ADD THIS
```

**Why This Solves It**: The system cannot enter a double-exposed state because new order submission is gated on successful cancellation of old orders. The worst case becomes "no quotes for one cycle" instead of "doubled exposure." The next quote cycle (1 second later) will retry both cancels and submits.

**Blocker**: YES

---

### E-02 [CRITICAL] Neg-Risk Conversion Arb Orders Are Not Atomic — On-Chain Step Missing

**Root Cause**: `NegRiskArbDetector.generate_orders()` (`pmm1/strategy/neg_risk_arb.py`) produces a flat list of FOK orders. `OrderManager.execute_arb()` (`pmm1/execution/order_manager.py:461-498`) submits them via `create_orders_batch()`, which iterates one by one (`pmm1/api/clob_private.py:421-422`). There is no atomicity across legs. More critically, the on-chain conversion step (buying all NO tokens and converting via the neg-risk contract) is completely absent from the code — `conversion_cost` is only used in PnL calculation, never executed.

**Why It Matters**: Neg-risk arb requires: buy No_k → on-chain conversion → sell all Yes_j. If the buy-No leg fills but a sell-Yes leg rejects (rate limit, liquidity moved), the bot holds unconverted No tokens with no exit. For a 5-outcome event, failing 4 of 5 exit legs leaves ~80% of the trade unhedged. At max_size=15, loss could reach $12+ per failed attempt.

**Proposed Fix**: Two options (choose one):
1. **Disable neg-risk arb entirely** until atomic execution is implemented. Set `neg_risk_arb.enabled: false` in config.
2. **Implement two-phase commit**: (a) Buy all required No tokens with FOK. If any leg fails, immediately sell back the legs that filled. (b) Only after all legs fill, execute the on-chain conversion. (c) Then sell Yes tokens. Add a rollback handler for each phase.

**Why This Solves It**: Option 1 eliminates the risk entirely. Option 2 provides a proper rollback path so partial fills don't create unbounded directional exposure. The key invariant is: the bot should never hold unconverted tokens from a half-completed arb.

**Blocker**: YES

---

### E-03 [CRITICAL] Order State Machine Accepts Invalid Transitions Silently

**Root Cause**: `TrackedOrder.transition_to()` (`pmm1/state/orders.py:155-192`) logs a warning on invalid transitions but **applies the transition anyway** (line 175-176: `self.state = new_state`). The comment says "Allow it anyway for resilience (WS messages may arrive out of order)." This means a CANCELED order can be re-activated to LIVE via an out-of-order WS message.

**Why It Matters**: Ghost orders in the tracker corrupt diff calculations. `get_active_by_side()` returns orders the exchange has already canceled, causing the diff engine to conclude "unchanged" when it should submit new quotes. Markets go unquoted until the next reconciliation cycle (30 seconds). The "resilience" rationale is backwards: silently accepting invalid transitions creates exactly the state corruption it claims to prevent.

**Proposed Fix**: Reject transitions **from terminal states** (FILLED, CANCELED, EXPIRED, FAILED) — these should never reactivate. Allow transitions between active states (SUBMITTED, LIVE, PARTIAL) with warnings, since WS ordering is genuinely unreliable for those.

```python
def transition_to(self, new_state: OrderState) -> bool:
    TERMINAL = {OrderState.FILLED, OrderState.CANCELED, OrderState.EXPIRED, OrderState.FAILED}
    if self.state in TERMINAL:
        logger.warning("rejected_terminal_transition", order_id=self.id,
                       from_state=self.state, to_state=new_state)
        return False  # Hard reject
    # Allow active-to-active with warning if not in valid_transitions
    self.state = new_state
    return True
```

**Why This Solves It**: Terminal states become truly terminal. An out-of-order WS message cannot resurrect a dead order. Active-to-active transitions remain flexible for WS ordering issues. The reconciler (which has exchange truth) remains the authoritative correction mechanism.

**Blocker**: YES

---

### E-04 [HIGH] 60-Second Stale Book Threshold Allows Quoting on Severely Stale Data

**Root Cause**: `pmm1/main.py:952-954` sets `stale_threshold = 120.0 if paper_mode else 60.0` for per-book staleness. The kill switch checks WS-level staleness (`ws_stale_kill_s=10`), but only for the entire connection, not per-token. A specific token's book can be 59 seconds old while other tokens are actively updating.

**Why It Matters**: On Polymarket prediction markets, prices can move 10-20 cents in 60 seconds during news events. Quoting on minute-old data at $15/side means $1.50 loss per adverse fill. If both sides fill, $3.00 per market. Across 12 markets: up to $36 in a single stale cycle.

**Proposed Fix**: Reduce per-book stale threshold to 10-15 seconds. Add per-token staleness tracking in `BookManager` with a `last_update_ts` per token_id. If a specific book is stale, skip quoting that market only (don't kill the whole system).

**Why This Solves It**: Markets with stale data are individually skipped rather than quoted at dangerous prices. The bot continues quoting fresh markets while pausing on stale ones. The 10s threshold matches the WS-level kill switch timing for consistency.

**Blocker**: NO

---

### E-05 [HIGH] Batch Order Submission Is Sequential, Not Truly Batched

**Root Cause**: `create_orders_batch()` (`pmm1/api/clob_private.py:411-437`) submits orders one at a time in a for loop (line 421-423). The comment acknowledges: "the SDK doesn't have a native batch method that handles signing." Each order pays full round-trip latency (~100-300ms). For 24 orders (12 markets x 2 sides), submission takes 2.4-7.2 seconds. The `quote_cycle_ms` is 1000ms.

**Why It Matters**: Markets quoted last in the loop run on quotes that are seconds stale, creating structural adverse selection bias. During the multi-second submission window, local state is inconsistent (some orders submitted, some pending). Combined with E-01, a mid-batch error creates partial state.

**Proposed Fix**: Use `asyncio.gather()` to submit orders concurrently. Sign all orders first (CPU-bound, fast), then submit all HTTP requests in parallel. Respect rate limits with a semaphore (e.g., max 10 concurrent requests).

```python
async def create_orders_batch(self, orders):
    signed = [self._sign_order(o) for o in orders]  # sync signing
    sem = asyncio.Semaphore(10)
    async def submit(s):
        async with sem:
            return await self._post_order(s)
    return await asyncio.gather(*[submit(s) for s in signed])
```

**Why This Solves It**: All orders are submitted within ~300ms instead of 2.4-7.2s. No market suffers seconds of staleness. The semaphore prevents rate limit violations.

**Blocker**: NO

---

### E-06 [HIGH] Fill Deduplication Set Clears Entirely at 500 Entries — Replay Vulnerability

**Root Cause**: `_notified_fills` (`pmm1/main.py:536-541`) is a `set` that grows until 500 entries, then `_notified_fills.clear()` drops ALL entries at once. After clearing, previously-seen fills could be replayed by the WebSocket and would be processed again.

```python
if len(_notified_fills) > 500:
    _notified_fills.clear()  # ALL history gone
```

**Why It Matters**: After 500 unique fills, a WS replay doubles position tracking. The bot may try to sell shares it doesn't own or over-allocate capital. Reconciliation at 60s partially mitigates, but the 30-60s window is dangerous. The dedup key `f"{order_id}:{size}:{price}"` also uses floats, which can differ in representation.

**Proposed Fix**: Replace with an LRU cache (bounded by count) or a sliding time-window dedup (keep last N minutes). Use string-formatted prices for deterministic keys.

```python
from collections import OrderedDict

class LRUDedup:
    def __init__(self, maxsize=2000):
        self._seen = OrderedDict()
        self._maxsize = maxsize
    def check_and_add(self, key: str) -> bool:
        if key in self._seen:
            return True  # duplicate
        self._seen[key] = True
        if len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)  # evict oldest
        return False
```

**Why This Solves It**: Old entries are evicted one-at-a-time (FIFO) instead of all-at-once. A 2000-entry LRU covers hours of fills without replay vulnerability. String-formatted prices eliminate float comparison issues.

**Blocker**: NO

---

### E-07 [HIGH] Tick Rounding MIN_PRICE/MAX_PRICE Mismatch With Quote Engine Clamping

**Root Cause**: `tick_rounding.py:24-25` defines `MIN_PRICE = Decimal("0.001")` and `MAX_PRICE = Decimal("0.999")`. The quote engine (`quote_engine.py:263-264`) clamps to `[tick_size, 1.0 - tick_size]`. For standard tick_size of 0.01, quote engine clamps to [0.01, 0.99]. But tick_rounding's `round_bid()` can produce 0.001 — a price the CLOB may reject for standard-tick markets.

**Why It Matters**: Invalid orders fail silently. The market goes unquoted on that side until the next cycle. While self-healing, it creates intermittent quoting gaps that hurt reward scoring and queue position.

**Proposed Fix**: Make `MIN_PRICE` and `MAX_PRICE` in tick_rounding dynamic based on the market's tick size: `MIN_PRICE = tick_size`, `MAX_PRICE = 1 - tick_size`. Pass tick_size into the rounding functions.

**Why This Solves It**: Rounding can never produce a price outside the market's valid range. The two clamping layers (quote engine + tick rounding) agree on bounds.

**Blocker**: NO

---

### E-08 [HIGH] Race Between Heartbeat Loop and Quote Cycle on WS Reconnect

**Root Cause**: The heartbeat loop runs on its own `asyncio.sleep(5)` cadence. User WS reconnect triggers `on_reconnect()` → `reconciler.full_reconciliation()`. During reconnection, the heartbeat loop continues. If heartbeat succeeds but User WS is mid-reconnect, the bot thinks it's healthy but has missed order state updates. The reconciler runs asynchronously and may not complete before the next quote cycle.

**Why It Matters**: Post-reconnect, the bot believes it has N active orders, submits N more, resulting in 2N exposure until reconciliation completes (up to 30s). At $15/order across 12 markets: up to $360 of excess exposure.

**Proposed Fix**: Add a `RECONNECTING` state that gates the quote cycle. After WS reconnect, set `RECONNECTING = True`. The quote cycle checks this flag and skips. Only clear after `full_reconciliation()` completes and the book state is rebuilt.

```python
# In main quote loop:
if self._reconnecting:
    logger.info("skipping_quote_cycle_reconnecting")
    continue

# In on_reconnect callback:
self._reconnecting = True
await reconciler.full_reconciliation()
await self._rebuild_books_from_rest()  # new
self._reconnecting = False
```

**Why This Solves It**: Quote generation is paused during the dangerous window between reconnect and full state reconciliation. No duplicate orders can be submitted. Markets are unquoted for a few seconds (safe) instead of double-quoted (dangerous).

**Blocker**: NO

---

### E-09 [HIGH] Binary Parity Arb Legs Are Not Atomic

**Root Cause**: `BinaryParityDetector.scan()` generates two FOK orders (buy YES + buy NO). They're submitted sequentially via `execute_arb()` → `create_orders_batch()` (serial). If leg 1 fills and leg 2 fails (price moved, insufficient liquidity), the bot holds a one-sided position from a trade intended to be market-neutral.

**Why It Matters**: One-sided exposure on a "market-neutral" trade. Max loss bounded by `max_size=15 * price_move`. For a 20c adverse move: $3 per failed arb.

**Proposed Fix**: Submit both legs concurrently (not sequentially). If either fails, immediately submit a market sell on the filled leg to unwind. Track "arb intent" so the risk system knows this is a hedged pair.

**Why This Solves It**: Concurrent submission minimizes the time window for price divergence. The unwind logic ensures one-sided exposure is immediately addressed rather than carried until the next cycle.

**Blocker**: NO

---

### E-10 [MEDIUM] Price String Formatting Can Produce Non-Conformant Strings

**Root Cause**: `tick_rounding.py:113-126` (`price_to_string`) formats prices with up to 10 decimal places then strips trailing zeros. For ultra-fine tick prices (e.g., 0.001), the result has 3 decimal places, which may not match the market's expected precision.

**Why It Matters**: Silent order rejection on markets with mismatched tick precision. Self-heals next cycle.

**Proposed Fix**: Accept tick_size as parameter and format to exactly the number of decimal places in tick_size (e.g., tick=0.01 → 2 decimals, tick=0.001 → 3 decimals).

**Why This Solves It**: Price strings always match the market's tick regime exactly.

**Blocker**: NO

---

### E-11 [MEDIUM] Reconciliation Does Not Integrate With Kill Switch

**Root Cause**: `Reconciler.reconcile_orders()` detects mismatches (unknown orders, missing orders) and logs warnings, but never calls `kill_switch.report_reconciliation_mismatch()`. The method exists in kill_switch.py (line 172) but is never invoked anywhere.

**Why It Matters**: Reconciliation mismatches indicating serious state divergence are logged but never escalate. The bot continues quoting with corrupted order state indefinitely if the mismatch is consistent.

**Proposed Fix**: Call `kill_switch.report_reconciliation_mismatch(mismatch_count)` at the end of `reconcile_orders()` when mismatches exceed a threshold (e.g., >3 mismatches in a single reconciliation).

**Why This Solves It**: State divergence triggers the kill switch, preventing the bot from quoting on corrupt state. The threshold prevents false positives from minor WS ordering issues.

**Blocker**: NO

---

### E-12 [MEDIUM] asyncio.to_thread() for SDK Calls Blocks Thread Pool

**Root Cause**: `pmm1/api/clob_private.py:357-367` uses `asyncio.to_thread(_create_and_post)` for order creation. The default thread pool size is `min(32, os.cpu_count() + 4)`. With 24+ sequential orders per cycle, each taking 200-500ms (ECDSA signing + HTTP), the pool can saturate, starving heartbeat and reconciler tasks.

**Why It Matters**: Heartbeat starvation during heavy order submission. Server cancels all orders if heartbeat is >15s late.

**Proposed Fix**: Use a dedicated `ThreadPoolExecutor` for SDK calls, separate from the default pool. Size it to match concurrent order capacity. Keep heartbeat on the main asyncio loop (not thread-delegated).

**Why This Solves It**: SDK calls get their own pool. Heartbeats and other critical async tasks are never starved by order submission.

**Blocker**: NO

---

### E-13 [MEDIUM] Fill Callback Applies Position Updates With fee=0.0

**Root Cause**: `pmm1/main.py:567-571` calls `pos.apply_fill(token_id, fill_side, size, price, fee=0.0)`. Fees are never accounted for in position PnL tracking. The fee_rate used in the quote engine (0.2% default) may not match market-specific fee rates.

**Why It Matters**: Systematic PnL overestimate. At 20bps per fill on $15, that's $0.03/fill. Over hundreds of fills/day, this accumulates and corrupts drawdown calculations.

**Proposed Fix**: Pass the actual fee from the fill message into `apply_fill()`. If the fill message doesn't include fees, compute from `price * size * fee_rate` using the market's actual fee rate.

**Why This Solves It**: PnL tracking reflects real costs. Drawdown calculations include fee drag.

**Blocker**: NO

---

### E-14 [MEDIUM] Neg-Risk Arb Uses Float Arithmetic for Edge Calculation

**Root Cause**: `pmm1/strategy/neg_risk_arb.py:139-253` uses float arithmetic for all edge calculations. For a 5-outcome event, cumulative sums accumulate 4 float additions.

**Why It Matters**: Negligible in practice — float error ~1e-12 vs epsilon of 0.01. Design smell rather than bug.

**Proposed Fix**: Use `Decimal` for edge calculations if precision is a concern. Alternatively, document that float precision is sufficient given the 1-cent epsilon.

**Why This Solves It**: Eliminates any theoretical precision concern. In practice, no behavioral change.

**Blocker**: NO

---

### E-15 [MEDIUM] WS Reconnect Clears Subscription Set Before Resubscribing

**Root Cause**: `pmm1/ws/market_ws.py:288-291` clears `_subscribed_assets` before calling `subscribe()`. If `subscribe()` fails, the set is empty. On the next reconnect attempt, no assets are resubscribed — the bot connects but receives no book updates.

```python
assets = list(self._subscribed_assets)
self._subscribed_assets.clear()  # <-- Danger: cleared before resubscribe
await self.subscribe(assets)     # <-- If this throws, set is empty
```

**Why It Matters**: Up to 10 seconds of quoting on completely stale data after a failed resubscription (until kill switch fires).

**Proposed Fix**: Don't clear the set until after successful resubscription.

```python
assets = list(self._subscribed_assets)
await self.subscribe(assets)  # subscribe() re-adds to set internally
# Only clear if we need to — or better, don't clear at all
```

**Why This Solves It**: The subscription set is never empty. Failed resubscription retains the previous asset list for the next retry.

**Blocker**: NO

---

### E-16 [MEDIUM] Exit Orders Use `round_bid()` for SELL Price

**Root Cause**: `OrderManager.submit_exit()` (`pmm1/execution/order_manager.py:394`) rounds sell prices with `round_bid()` (rounds DOWN). For exit orders, rounding down gives worse proceeds.

**Why It Matters**: Systematic underfilling of exit proceeds. At standard tick (0.01), each exit leaves $0.01 * size on the table. At 100 shares: $1.00 per exit.

**Proposed Fix**: Use `round_ask()` for normal exits (rounds UP for better proceeds). Use `round_bid()` only for critical/urgent exits where fill speed matters more than price.

**Why This Solves It**: Exit prices are rounded in the favorable direction, improving PnL per exit.

**Blocker**: NO

---

### E-17 [LOW] Heartbeat Loop Has No Jitter

**Root Cause**: `pmm1/state/heartbeats.py:109` sleeps exactly `self._interval_s` (5 seconds). No jitter added.

**Why It Matters**: Minor. If multiple instances overlap, they send heartbeats at the same cadence.

**Proposed Fix**: Add random jitter: `await asyncio.sleep(self._interval_s + random.uniform(-0.5, 0.5))`

**Why This Solves It**: Breaks synchronization between potential overlapping instances.

**Blocker**: NO

---

### E-18 [LOW] Order Tracker Never Cleans Up Empty Index Sets

**Root Cause**: `pmm1/state/orders.py:283-300` (`cleanup_terminal`) removes orders from `_orders` dict but leaves empty sets as keys in `_by_token` and `_by_strategy` dicts.

**Why It Matters**: Slow memory leak over long-running sessions. Negligible compared to network I/O.

**Proposed Fix**: Delete empty sets during cleanup: `if not self._by_token[tid]: del self._by_token[tid]`

**Why This Solves It**: Memory stays bounded regardless of how many unique tokens are cycled through.

**Blocker**: NO

---

### E-19 [LOW] USDC Balance Heuristic Misclassifies Large Balances

**Root Cause**: `pmm1/main.py:403-405` uses `balance = raw_balance / 1e6 if raw_balance > 1000 else raw_balance`. If the account holds $1,500 USDC (returned as `"1500"` in dollar units), this divides by 1e6 and reports $0.0015.

**Why It Matters**: Above $1,000 NAV, the bot reports near-zero balance and enters flatten-only mode. Blocks scaling.

**Proposed Fix**: Check the USDC contract decimals or use a consistent decimal conversion. The CLOB API should document whether it returns raw units or dollar units — handle both cases explicitly.

**Why This Solves It**: Balance is always correctly interpreted regardless of magnitude.

**Blocker**: NO (but becomes a blocker at $1,000+ NAV)

---

### E-20 [LOW] Top-of-Book Clamp Produces Redundant Crossing Guard Logic

**Root Cause**: `pmm1/main.py:1146-1167` clamps bid up to best_bid and ask down to best_ask, which can produce crossed quotes. A separate crossing guard at lines 1213-1222 fixes this. Two places handle the same concern with different logic.

**Why It Matters**: No immediate bug (crossing guard is correct), but increases maintenance risk and confusion.

**Proposed Fix**: Remove the top-of-book clamp entirely (it conflicts with inventory skew — see R-07) or consolidate the crossing logic into a single location.

**Why This Solves It**: Single responsibility for crossing prevention. Eliminates confusion about which logic governs.

**Blocker**: NO

---

## Auditor 2: Risk & PnL (17 Findings)

### R-01 [CRITICAL] Production Risk Limits Are 3-6x Looser Than Spec Hard Caps

**Root Cause**: `config/default.yaml:39-45` overrides `settings.py:91-102` conservative defaults with extremely loose values:
- `per_market_gross_nav: 0.08` (8%) vs spec's 2%
- `per_event_cluster_nav: 0.15` (15%) vs spec's 5%
- `total_directional_nav: 0.60` (60%) vs spec's 10%
- `total_arb_gross_nav: 0.40` (40%) vs spec's 25%

**Why It Matters**: At $100 NAV, the bot can concentrate $60 net directional. A 10% adverse move on concentrated positions wipes $6 (6% NAV), triggering Tier3 flatten. The limits are either too loose for protection or too tight for profitable trading — no goldilocks zone. The spec calls these "Hard Caps" — the config violates the spec.

**Proposed Fix**: Align `default.yaml` to spec values. If the spec values are too conservative for profitable operation, update the spec with justified reasoning — don't silently override.

```yaml
risk:
  per_market_gross_nav: 0.02
  per_event_cluster_nav: 0.05
  total_directional_nav: 0.10
  total_arb_gross_nav: 0.25
```

**Why This Solves It**: Risk limits match the spec's design intent. Directional exposure is capped at 10%, meaning a 20% adverse move on a correlated cluster costs at most 2% NAV instead of 12%.

**Blocker**: YES

---

### R-02 [CRITICAL] No Cross-Market Correlation Tracking

**Root Cause**: `pmm1/risk/limits.py` tracks per-event cluster exposure (markets sharing the same `event_id`), but has zero awareness of cross-event correlation. `positions.py:217-227` computes the L1 norm of individual market nets, not correlation-adjusted portfolio risk. No correlation matrix, no factor model, no stress test.

**Why It Matters**: The bot could simultaneously be long 8% on "Biden wins Pennsylvania," 8% on "Biden wins Michigan," 8% on "Biden wins Wisconsin" across different event_ids. A single news event produces a correlated 3x loss.

**Proposed Fix**: Implement a simple thematic grouping layer. Markets can be tagged with themes (e.g., "US_ELECTION_2026", "CRYPTO_BTC") either manually in config or via keyword matching on market titles. Add a `per_theme_nav` limit (e.g., 15%) that aggregates across event_ids within a theme.

```python
class ThematicCorrelation:
    def __init__(self, themes: dict[str, list[str]]):
        self._themes = themes  # theme_name -> [keyword list]

    def get_theme(self, market_title: str) -> str:
        for theme, keywords in self._themes.items():
            if any(kw.lower() in market_title.lower() for kw in keywords):
                return theme
        return "uncorrelated"
```

**Why This Solves It**: Correlated positions across different event_ids are recognized and limited. Not perfect (keyword matching misses complex correlations), but captures the most dangerous cases.

**Blocker**: YES

---

### R-03 [CRITICAL] Drawdown Governor Computes From Day Start, Not High Water Mark

**Root Cause**: `pmm1/risk/drawdown.py:143-149` computes `drawdown_pct = (day_start_nav - current_nav) / day_start_nav`. The `daily_high_watermark` field is tracked (line 137) but **never used**. If the bot starts at $100, rallies to $110, then drops to $101, drawdown reads -1% (still positive from day start). The $9 intraday loss is invisible.

**Why It Matters**: An intraday drawdown of 9% from peak registers as 0%, triggering no protective action. This completely defeats the purpose of the drawdown governor during volatile sessions.

**Proposed Fix**: Use the high-water mark:

```python
# Replace line 145:
self._state.drawdown_pct = (
    (self._state.daily_high_watermark - current_nav)
    / self._state.daily_high_watermark
)
```

**Why This Solves It**: The drawdown governor measures peak-to-trough, which is the standard definition. A rally-then-crash is detected and protected against.

**Blocker**: YES

---

### R-04 [HIGH] Adverse Selection Calculation Drops Favorable Markouts

**Root Cause**: `pmm1/analytics/pnl.py:183-204` uses `max(0, -as_val)` which only counts markouts where the mid moved against us. Favorable markouts are clamped to zero. The `as_ratio_5s` divides only the adverse component by spread_capture, creating an unrealistically favorable AS ratio.

**Why It Matters**: PnL decomposition systematically understates adverse selection, overstating profitability. Decisions to continue quoting a toxic market are made on biased data.

**Proposed Fix**: Track both adverse and favorable markouts separately. Report net markout (adverse - favorable) as the true AS measure. Keep the one-sided adverse metric for toxicity detection but add the net metric for PnL attribution.

**Why This Solves It**: PnL decomposition reflects reality. The target "AS < 60% of spread capture" uses actual net markouts.

**Blocker**: NO

---

### R-05 [HIGH] NAV Estimation Uses Cost Basis, Not Mark-to-Market

**Root Cause**: `pmm1/state/inventory.py:223-241` (`get_total_nav_estimate()`) uses `pos.yes_avg_price` as fallback mark price when no `price_oracle` is provided. In `main.py:870-872`, `get_total_nav_estimate()` is called **without** a price oracle for live mode. NAV is computed at cost basis.

**Why It Matters**: The drawdown governor receives cost-basis NAV. If the bot buys 10 shares at $0.50 and the market drops to $0.30, NAV still reads $5.00 instead of $3.00 for that position. The $2.00 unrealized loss is hidden. Combined with R-03, the drawdown governor is nearly useless.

**Proposed Fix**: Pass a price oracle (book midpoint) into `get_total_nav_estimate()`. The book manager already has current midpoints per token.

```python
# In main loop NAV calculation:
def _price_oracle(token_id: str) -> float:
    book = book_manager.get_book(token_id)
    return book.midpoint if book and book.midpoint else None

nav = inventory_manager.get_total_nav_estimate(price_oracle=_price_oracle)
```

**Why This Solves It**: NAV reflects current market prices. Unrealized losses are visible to the drawdown governor. The two fixes (R-03 + R-05) together make the drawdown governor functional.

**Blocker**: YES

---

### R-06 [HIGH] Fill Deduplication Set Can Lose State

**Root Cause**: Same as E-06. `_notified_fills` clears at 500 entries. After clearing, replayed fills cause double position updates. The position tracker's `apply_fill` has no idempotency.

**Why It Matters**: Double-counted fills corrupt position tracking, inventory skew, size calculations, and exit logic. Reconciliation corrects within 60s, but the window is dangerous.

**Proposed Fix**: Same as E-06 — use LRU cache. Additionally, add idempotency to `apply_fill` by tracking fill IDs (not just order_id:size:price).

**Why This Solves It**: Double defense: LRU prevents dedup loss, and idempotent apply_fill prevents double-counting even if dedup fails.

**Blocker**: NO

---

### R-07 [HIGH] Top-of-Book Clamp Defeats Inventory Skew

**Root Cause**: `pmm1/main.py:1146-1167` — the quote engine computes reservation price with inventory skew (`r_t = p_hat - gamma * q_t`), but the main loop then clamps the bid to best bid if it's more than 1 tick away. This overrides the skew entirely, placing bids at best bid regardless of inventory position. The gamma parameter is decorative.

**Why It Matters**: The bot joins the queue at best bid even when heavily long, accumulating more inventory when it should step back. The Avellaneda-Stoikov skew model is nullified. The market-maker becomes a pure queue-position strategy with no inventory management.

**Proposed Fix**: Remove the top-of-book clamp for the side where inventory skew is pushing the price away. Only clamp toward best bid/ask when skew is pulling the price tighter (i.e., the bot wants more inventory on that side).

```python
# Only clamp toward top-of-book, not away from it:
# If skew pushes bid lower (we're long, want less), let it.
# If skew pushes bid higher (we're short, want more), clamp to best_bid.
if quote_intent.bid_price > best_bid_float:
    quote_intent.bid_price = best_bid_float  # don't improve the bid beyond best
# Do NOT clamp bid upward when it's below best — that's the skew working.
```

**Why This Solves It**: Inventory skew functions as designed. When long, the bid steps back. When short, the bid is capped at best bid (no queue jumping). The A-S model's convergence properties are preserved.

**Blocker**: YES

---

### R-08 [HIGH] Resolution Risk Coefficients Are Hand-Waved

**Root Cause**: `pmm1/strategy/fair_value.py:127-161` — haircut coefficients `h_0=0.005, k_1=0.5, k_2=0.3, k_3=0.2, k_4=0.1` are static defaults with no calibration. The fair value model coefficients `beta_1=1.0, beta_2-6=0.0` mean the model is literally `sigmoid(logit(midpoint)) = midpoint`.

**Why It Matters**: The system quotes around the raw midpoint with a static half-spread. There is no alpha. The entire edge depends on queue priority plus rewards.

**Proposed Fix**: Either (a) fit coefficients from historical data (need book snapshots + fill outcomes), or (b) honestly acknowledge the model is placeholder and set expectations accordingly. For the haircut: use empirical volatility buckets and resolution proximity data to justify thresholds.

**Why This Solves It**: Option (a) gives real alpha. Option (b) at least removes false confidence.

**Blocker**: NO

---

### R-09 [MEDIUM] Kill Switch Auto-Clear Creates Re-Entry Risk

**Root Cause**: `pmm1/risk/kill_switch.py:97-107, 113-121` — stale feed kill switch auto-clears after 30s. When the feed sends one message after being down for minutes, the kill switch immediately clears. No recovery period or warmup.

**Why It Matters**: The first reconnect message clears the switch, and the bot immediately starts quoting on a partially populated book. Bids/asks from an incomplete book could be mispriced.

**Proposed Fix**: Add a "warmup period" after kill switch clear: require N consecutive healthy heartbeats and a fresh book snapshot before resuming quoting.

**Why This Solves It**: The bot doesn't re-enter until book state is demonstrably fresh.

**Blocker**: NO

---

### R-10 [MEDIUM] Reconciliation Auto-Adopts Unknown Positions With Zero Avg Price

**Root Cause**: `pmm1/state/positions.py:289-305` — when reconciliation finds an exchange position the bot doesn't track, it auto-adopts with `yes_avg_price=0.0`. The stop-loss computation divides by avg_price, producing division by zero or infinite percentages.

**Why It Matters**: Orphaned positions become unmanageable by the exit system.

**Proposed Fix**: Set adopted positions' avg_price to current midpoint (best available estimate), not zero. Add a warning flag so the operator knows the avg_price is estimated.

**Why This Solves It**: Exit system can compute meaningful unrealized PnL. The midpoint estimate is imperfect but far better than zero.

**Blocker**: NO

---

### R-11 [MEDIUM] Taker Bootstrap Bypasses All Risk Limits

**Root Cause**: `pmm1/main.py:1527-1598` — fill escalation taker bootstrap picks the highest-volume market and submits a FAK buy at best ask. It goes through `clob_private.create_order()` directly instead of the risk-checked `order_manager.diff_and_apply()` path. No check of circuit breaker, resolution risk, per-market limits, or drawdown governor.

**Why It Matters**: After 20 minutes of no fills, the bot takes liquidity in a potentially toxic market, bypassing all risk controls. At $5 minimum: 5% of $100 NAV in a single uncontrolled trade.

**Proposed Fix**: Route the taker bootstrap order through the risk limit checker before submission. Add explicit checks for `is_kill_switch_active()`, `check_per_market_limit()`, and `drawdown_governor.tier >= TIER1`.

**Why This Solves It**: Taker orders respect the same risk boundaries as maker orders. No path bypasses the risk system.

**Blocker**: YES

---

### R-12 [MEDIUM] Exit Manager Only Processes One Side of YES/NO Positions

**Root Cause**: `pmm1/strategy/exit_manager.py:92-103` — iterates over positions, checks `yes_size > 0` first, then `elif no_size > 0`. If a position has both YES and NO inventory (common for MM), only YES is evaluated. The `continue` on line 102 skips NO entirely.

**Why It Matters**: NO-side positions with unlimited losses have no stop-loss protection.

**Proposed Fix**: Replace `elif` with separate `if` blocks. Process both sides independently in the same iteration.

**Why This Solves It**: Both sides of every position are evaluated for exit signals.

**Blocker**: NO

---

### R-13 [MEDIUM] Capital Efficiency — Minimum Viable NAV Higher Than Expected

**Root Cause**: With `target_dollar_size: 8.0` and 12 markets, the bot targets $96 deployed on buy side alone. Minimum order is $1.50. At 12 markets x 2 sides x $1.50 = $36 minimum capital tied up in order minimums. At 12 markets x $8 per-market limit = $96. The $100 NAV is essentially fully consumed.

**Why It Matters**: Zero capital buffer for adverse moves. Minimum viable NAV for comfortable operation is closer to $200-300.

**Proposed Fix**: Either reduce `num_markets` to 6-8, or reduce `target_dollar_size` to $5, or increase NAV to $250+. Document the minimum viable NAV calculation.

**Why This Solves It**: Ensures a buffer for adverse moves and gas costs.

**Blocker**: NO

---

### R-14 [MEDIUM] PnL Tracker Disconnected From Live Trading Loop

**Root Cause**: `pmm1/analytics/pnl.py:110-119` — `PnLTracker._fills` and `PnLAttributor._fills` are independent lists. No code path feeds fills into `PnLTracker.record_fill()` in the main loop. `compute_snapshot()` is only called at shutdown. The PnL decomposition system is dead code in production.

**Why It Matters**: The PnL tracker accumulates zero fills during live operation. Acceptance criteria (AS ratio, quote uptime) are never evaluated. You are flying blind.

**Proposed Fix**: Call `pnl_tracker.record_fill(fill_data)` in the fill callback alongside position tracking. Call `compute_snapshot()` periodically (e.g., every 5 minutes) and log/persist results.

**Why This Solves It**: PnL attribution becomes live, enabling real-time monitoring of spread capture, adverse selection, and profitability.

**Blocker**: NO

---

### R-15 [LOW] Drawdown Governor Daily Reset Uses Simple Date Comparison

**Root Cause**: `pmm1/risk/drawdown.py:179-187` compares `now.date() > day_start.date()`. Uses local system clock, potentially mismatching UTC. Hard daily reset forgives losses at midnight even if positions causing the loss are still open.

**Why It Matters**: Minor timezone edge case. Intra-day protection resets arbitrarily.

**Proposed Fix**: Use `datetime.now(timezone.utc)` consistently. Consider a rolling 24h window instead of hard daily reset.

**Why This Solves It**: Consistent timezone handling. Rolling window provides continuous protection.

**Blocker**: NO

---

### R-16 [LOW] Circuit Breaker Baseline Includes Toxic Fills

**Root Cause**: `pmm2/allocator/circuit_breaker.py:91-94` — `markout_1s_avg` includes the toxic fills that caused the trip. After cooldown, the baseline is polluted, desensitizing future detections.

**Why It Matters**: After one toxic episode, the circuit breaker is harder to trip again.

**Proposed Fix**: Exclude fills during tripped state from the rolling average. Or use a pre-trip baseline.

**Why This Solves It**: Circuit breaker sensitivity remains constant across episodes.

**Blocker**: NO

---

### R-17 [LOW] Inventory Carry PnL Is Never Computed

**Root Cause**: `pmm1/analytics/pnl.py:213-228` — `PnLSnapshot.inventory_carry` exists but remains 0.0. `MarketPosition.mark_to_market()` exists but is never called.

**Why It Matters**: Inventory carry is where bulk of risk lies for a market-maker, yet it's untracked.

**Proposed Fix**: Call `mark_to_market()` in the periodic PnL snapshot using current midpoints. Assign the MTM change to `inventory_carry`.

**Why This Solves It**: PnL decomposition captures the largest risk component.

**Blocker**: NO

---

## Auditor 3: Quantitative Models (20 Findings)

### Q-01 [CRITICAL] Fair Value Model = Identity Function — Zero Alpha

**Root Cause**: `settings.py:71-77` ships with `beta_1 = 1.0, beta_2-6 = 0.0`. The model computes `sigmoid(beta_1 * logit(midpoint)) = sigmoid(logit(midpoint)) = midpoint`. No fitting pipeline exists. Even if betas were non-zero, logit(midpoint) and logit(microprice) have >0.95 correlation, making OLS collinear.

**Why It Matters**: The bot has no edge in fair value estimation. All spread capturing depends on queue priority and rewards alone.

**Proposed Fix**: Either (a) implement an offline fitting pipeline using recorded book snapshots and fill outcomes to find meaningful betas, or (b) use microprice directly (volume-weighted mid) as a better-than-midpoint estimator without the logistic wrapper.

```python
def microprice(best_bid, best_ask, bid_size, ask_size):
    return (best_bid * ask_size + best_ask * bid_size) / (bid_size + ask_size)
```

**Why This Solves It**: Option (a) gives data-driven alpha. Option (b) is simple, proven, and strictly better than midpoint for binary markets — it captures imbalance information without fitting.

**Blocker**: YES

---

### Q-02 [CRITICAL] Queue Position Hardcoded to Zero — All Fill Probabilities Are Fantasy

**Root Cause**: `pmm2/scorer/combined.py:79-82` sets `queue_ahead_bid = 0.0` and `queue_ahead_ask = 0.0` with comment "assume we're at the front." The `QueueEstimator` class exists but is never called. With queue_ahead=0, the fill hazard formula gives P(fill) ≈ 1.0. Every bundle is scored as if it fills with certainty.

**Why It Matters**: All EV calculations (spread_ev, reward_ev, rebate_ev) are inflated by a fill probability multiplier of ~1.0 instead of the true value (likely 0.01-0.30). The allocator overestimates returns by 3-100x.

**Proposed Fix**: Wire the `QueueEstimator` into `MarketEVScorer`. Compute queue_ahead from the book snapshot (sum of visible liquidity at our price level ahead of us).

```python
# Replace hardcoded zeros:
queue_ahead_bid = self.queue_estimator.estimate_ahead(
    market.token_id_yes, "BUY", market.best_bid
)
queue_ahead_ask = self.queue_estimator.estimate_ahead(
    market.token_id_no, "BUY", market.best_ask  # or yes ask
)
```

**Why This Solves It**: Fill probabilities reflect actual queue depth. EV calculations become meaningful. The allocator can distinguish between markets where fills are likely vs unlikely.

**Blocker**: YES

---

### Q-03 [CRITICAL] Toxicity Can Be Negative — Turns Cost Into Revenue

**Root Cause**: `pmm2/scorer/toxicity.py:70-86` returns a weighted average of markouts with no floor. If recent fills have favorable markouts (negative values), toxicity is negative. In `combined.py:147`, toxicity is subtracted: `total_value = ... - bundle.tox_cost`. Negative tox_cost becomes addition.

**Why It Matters**: During favorable conditions, the model overestimates EV. Toxicity is a structural cost that should have a floor at zero. Negative markouts should be captured as "alpha" in spread_ev, not as negative toxicity.

**Proposed Fix**: Floor toxicity at zero:

```python
def compute_toxicity(...) -> float:
    raw_tox = w1 * markout_1s + w2 * markout_5s + w3 * markout_30s
    return max(0.0, raw_tox)  # Toxicity is a cost, never negative
```

**Why This Solves It**: Toxicity remains a cost. Favorable markouts don't inflate EV. If you want to capture alpha, add a separate `favorable_markout` component with its own sign.

**Blocker**: YES

---

### Q-04 [HIGH] Rebate EV Double-Counts With Spread EV

**Root Cause**: `pmm2/scorer/rebate_ev.py:67-72` computes `rebate_per_fill = size * price * fee_rate` (fee we collect from takers). `spread_ev.py:36-43` computes `bid_ev = fill_prob * (reservation_price - bid_price) * size`. If reservation_price already reflects the fee structure, the rebate is priced into the spread. Adding both double-counts fee income.

**Why It Matters**: On fee-enabled markets, total EV is inflated by ~10-20 cents per cycle. The allocator over-weights fee-enabled markets.

**Proposed Fix**: Either (a) include maker rebate in the spread EV calculation and remove the separate rebate component, or (b) adjust reservation_price to exclude the rebate component so spread_ev captures pure directional edge only.

**Why This Solves It**: Each dollar of expected revenue is counted exactly once.

**Blocker**: NO

---

### Q-05 [HIGH] Reward EV Proxy Is Unvalidated — Two Different Functions Coexist

**Root Cause**: PMM2 `reward_ev.py:53-69` uses `g(s,v) = (1 - s/v)^2`. PMM1 `rewards.py:79-101` uses `score = size * (1 - (distance/max_distance)^2)`. These are mathematically different. Neither validated against Polymarket's actual reward algorithm. The `q_others` (competitor mass) estimate `max(liquidity * 0.1, 50.0)` has no empirical basis.

**Why It Matters**: If the true scoring function differs, the model overestimates reward EV for tight quotes and underestimates for wider ones. The competitive mass error alone could cause reward share to be off by 10-50x.

**Proposed Fix**: (a) Collect actual reward payouts vs predicted for 50+ epochs. (b) Fit the scoring function from reward/order data. (c) Converge V1 and V2 to use the same validated function. Until validated, apply a 50% confidence discount to reward EV.

**Why This Solves It**: Reward EV is calibrated against observed reality. A single source of truth eliminates divergence.

**Blocker**: NO

---

### Q-06 [HIGH] V3 RouteCalibrator Gradient Descent Has No Regularization

**Root Cause**: `v3/calibration/route_models.py:174-187` runs 1000 iterations of gradient descent with lr=0.01 on 7 features. No L2 regularization, no gradient clipping, no learning rate decay. Beta values can grow without bound. When the dot product exceeds ~700, `math.exp(-x)` underflows, gradients become zero, training stalls.

**Why It Matters**: Unregularized logistic regression can diverge or overfit, pushing calibrated probabilities to 0 or 1, destroying prediction intervals.

**Proposed Fix**: Add L2 regularization (weight decay) and gradient clipping:

```python
for i, b in enumerate(betas):
    grad = ... - lambda_l2 * b  # L2 penalty
    grad = max(-5.0, min(5.0, grad))  # Gradient clipping
    betas[i] += lr * grad
```

**Why This Solves It**: L2 keeps betas bounded. Gradient clipping prevents divergence. Standard practice for logistic regression.

**Blocker**: NO (V3 disabled)

---

### Q-07 [HIGH] Fill Calibrator Uses Placeholder 0.5 — Actively Degrades Accuracy

**Root Cause**: `pmm2/calibration/fill_calibrator.py:148` hardcodes `predicted_rate = 0.5`. The lambda_correction is always `avg_actual / 0.5`. Since actual fill rates on Polymarket are well below 50%, the correction continuously shrinks the depletion rate until it hits the floor.

**Why It Matters**: The calibrator actively degrades model accuracy instead of improving it. It ratchets depletion rates toward zero.

**Proposed Fix**: Compute predicted_rate from the actual fill hazard model for the same time window:

```python
predicted_rate = self.fill_hazard.predicted_fill_prob(
    market_id, horizon=calibration_window
)
lambda_correction = actual_rate / max(predicted_rate, 0.001)
```

**Why This Solves It**: The correction measures model bias (predicted vs actual), not an arbitrary anchor. When the model is correct, correction ≈ 1.0.

**Blocker**: YES

---

### Q-08 [HIGH] Model Haircut Is Additive — Should Be Multiplicative for Joint Risks

**Root Cause**: `pmm1/strategy/fair_value.py:127-161` — `h_t = h_0 + k_1*V + k_2*stale + k_3*res + k_4*err`, clamped to [0.0, 0.5]. Stale AND near resolution should be multiplicatively worse, not additively. Cold start: `model_error = 0.0` gives false confidence.

**Why It Matters**: Joint risk scenarios (stale + near resolution + volatile) are underweighted by ~2-3x. Bot quotes too aggressively during cold start.

**Proposed Fix**: Use multiplicative structure: `h_t = h_0 * (1 + k_1*V) * (1 + k_2*stale) * (1 + k_3*res) * (1 + k_4*err)`. Set cold-start `err = 0.5` (maximum uncertainty) instead of 0.0.

**Why This Solves It**: Joint risks compound. Cold start is conservative instead of overconfident. The cap at 0.5 still applies.

**Blocker**: NO

---

### Q-09 [MEDIUM] Depletion Rate Only Counts Decreases, Ignores Refills

**Root Cause**: `pmm2/queue/depletion.py:70-76` uses `max(prev_depth - curr_depth, 0.0)`. Intervals where depth increased contribute zero to depletion. This conflates gross depletion with net depletion.

**Why It Matters**: Fill probabilities are underestimated. On markets with active MM refreshing, measured depletion understates true order arrival rate.

**Proposed Fix**: Track gross depletion (sum of all decreases) separately from net depletion. Use gross depletion for fill hazard estimation.

**Why This Solves It**: Fill probability reflects actual order flow, not net book changes.

**Blocker**: NO

---

### Q-10 [MEDIUM] B1 Bundles Quote at Existing Best — Zero Edge by Construction

**Root Cause**: `pmm2/scorer/bundles.py:92-117` sets `bid_price = market.best_bid`. `spread_ev.py` computes `bid_edge = mid - best_bid = spread/2 > 0`. This "edge" is illusory — every MM has the same arithmetic. True edge must account for queue cost and conditional adverse selection.

**Why It Matters**: Apparent profitability is an artifact of not modeling competition. The system cannot distinguish markets where it has an actual edge.

**Proposed Fix**: Subtract estimated queue cost from spread_ev: `net_edge = gross_spread/2 - queue_cost_per_fill`. Queue cost = time_in_queue * capital_opportunity_cost.

**Why This Solves It**: Markets are ranked by net edge after queue cost, not gross arithmetic spread.

**Blocker**: NO

---

### Q-11 [MEDIUM] Toxicity Lookback Is Fixed 24h, Not Adaptive

**Root Cause**: `pmm2/scorer/toxicity.py:20-86` uses fixed 24h lookback with weights 0.5/0.3/0.2 at 1s/5s/30s. Assumes stationarity. Polymarket events experience regime changes. The 1s horizon may capture structural latency rather than informed flow.

**Why It Matters**: During regime transitions, toxicity estimates lag by hours. Quoting continues in markets where toxicity has spiked.

**Proposed Fix**: Use exponentially-weighted lookback (more recent fills weighted higher). Add regime detection: if markout variance exceeds 2x the 24h average, shrink the lookback window to 1h.

**Why This Solves It**: Toxicity adapts to regime changes. Recent fills dominate when conditions change.

**Blocker**: NO

---

### Q-12 [MEDIUM] V3 Signal Decay Uses Wrong Half-Life Formula

**Root Cause**: `v3/calibration/decay.py:37-72` uses `lambda = exp(-age / half_life)`. The actual half-life is `half_life * ln(2)`, not `half_life`. When `half_life = 60`, signal reaches 50% at 41.6s, not 60s.

**Why It Matters**: Signals decay ~30% faster than designed. Not catastrophic, but surprising.

**Proposed Fix**: Use `lambda = 0.5 ** (age / half_life)` or equivalently `exp(-age * ln(2) / half_life)`.

**Why This Solves It**: The parameter means what its name says.

**Blocker**: NO

---

### Q-13 [MEDIUM] Conformal Intervals Applied in Probability Space, Not Logit

**Root Cause**: `v3/calibration/route_models.py:100-126` adds/subtracts margin in probability space. For p=0.05, margin=0.20: interval is [-0.15, 0.25], clamped to [0.0, 0.25]. Asymmetric in information content.

**Why It Matters**: Miscalibrated near extreme probabilities (< 0.15 or > 0.85), which is where Polymarket's most liquid markets trade.

**Proposed Fix**: Compute intervals in logit space and transform back.

**Why This Solves It**: Intervals respect probability geometry. Symmetric in information units.

**Blocker**: NO

---

### Q-14 [MEDIUM] PMM-1 Queue Model Parameters Are Assumed, Not Calibrated

**Root Cause**: `pmm1/backtest/queue_model.py:36-45` — `theta_0=-1.0, theta_1=5.0` appear hand-set. theta_1=5.0 means orders 2 ticks from best have 0.45% the fill rate of orders at best. Unrealistically steep without data.

**Why It Matters**: Fill model either dramatically over or underestimates fills. Direction of error unknown.

**Proposed Fix**: Collect fill vs queue position data for 2-4 weeks. Fit thetas via maximum likelihood on actual fill events.

**Why This Solves It**: Parameters reflect Polymarket's actual microstructure.

**Blocker**: NO

---

### Q-15 [MEDIUM] Arb EV Component Is Permanently Zero (Dead Code)

**Root Cause**: `pmm2/scorer/arb_ev.py:16-56` always returns 0.0. Contains only TODO comments. The "seven-component" model is actually six.

**Why It Matters**: Binary parity arb is a real, low-risk revenue source being entirely ignored.

**Proposed Fix**: Implement arb detection in the scorer by checking sum(best_yes_ask + best_no_ask) < 1.0 across binary markets. Compute arb edge as `1.0 - (yes_ask + no_ask) - 2 * fee_rate`.

**Why This Solves It**: A real revenue source is captured in the EV framework.

**Blocker**: NO

---

### Q-16 [LOW] Realized Volatility Computed From Trade Prices (Bid-Ask Bounce Bias)

**Root Cause**: `pmm1/strategy/features.py:151-173` computes log returns from consecutive trade prices, which include bid-ask bounce. Vol is upward-biased by ~2 * var(half_spread) (Roll 1984).

**Why It Matters**: Vol overestimate inflates haircut, making the bot more conservative. Benign direction but misprices risk.

**Proposed Fix**: Use mid prices instead of trade prices for vol computation.

**Why This Solves It**: Standard realized vol computation. No bid-ask bounce bias.

**Blocker**: NO

---

### Q-17 [LOW] Time-to-Resolution Assumes 7-Day Market Lifetime

**Root Cause**: `pmm1/strategy/features.py:288-291` computes fraction assuming all markets last 7 days. Polymarket markets range from hours to months.

**Why It Matters**: Minor feature distortion. Currently unused (beta_6=0).

**Proposed Fix**: Use actual market creation timestamp and end_date_iso for the fraction.

**Why This Solves It**: Feature reflects actual market lifecycle.

**Blocker**: NO

---

### Q-18 [LOW] ToxicityFitter Regresses Markouts on Markouts

**Root Cause**: `pmm2/calibration/toxicity_fitter.py:77-108` — dependent variable `avg_adverse_pnl` is the mean of `markout_1s, 5s, 30s` (the independent variables). OLS always finds weights ≈ (1/3, 1/3, 1/3) with near-perfect R². The procedure is mathematically vacuous.

**Why It Matters**: The "fitted" weights never discover meaningful relative importance. The fitting procedure adds computational cost with zero informational value.

**Proposed Fix**: Regress on a meaningful target (e.g., realized PnL, or binary "was this fill ultimately profitable"). Or simply use fixed weights justified by theory (e.g., weight longer horizons higher for information content).

**Why This Solves It**: The regression learns something real, or the fixed weights are honestly justified.

**Blocker**: NO

---

### Q-19 [LOW] Greedy Allocator Reward Market Count Is Always == Markets Funded

**Root Cause**: `pmm2/allocator/greedy.py:190-192` — counts markets with "B1 or B2 funded," but every funded market gets B1 by construction. The metric is identical to `markets_funded`.

**Why It Matters**: Misleading monitoring metric. No financial impact.

**Proposed Fix**: Count markets where `reward_ev > 0` instead.

**Why This Solves It**: Metric reflects actual reward eligibility.

**Blocker**: NO

---

### Q-20 [LOW] B1 Capital Calculation Is Price-Invariant

**Root Cause**: `pmm2/scorer/bundles.py:97-105` — `b1_capital = b1_bid_size * best_bid + b1_ask_size * best_ask = 2 * b1_size_usdc` always, regardless of price.

**Why It Matters**: Capital allocation is insensitive to price level. A $0.05 market uses the same capital as a $0.50 market despite different risk profiles.

**Proposed Fix**: Scale capital by `min(price, 1-price)` to allocate more capital to markets near 50% (higher risk per share) and less to extremes.

**Why This Solves It**: Capital reflects actual inventory risk per dollar deployed.

**Blocker**: NO

---

## Auditor 4: Systems & Architecture (27 Findings)

### S-01 [CRITICAL] V1 Bridge Execution Methods Are Stubbed Out

**Root Cause**: `pmm2/runtime/v1_bridge.py:174-270` — `_execute_add`, `_execute_cancel`, `_execute_amend` contain only `logger.info(...)` and `return True`. Marked `# TODO: Integrate with actual V1 order manager`. If `shadow_mode` is set to `False`, PMM-2 reports all mutations as "executed" while doing nothing.

**Why It Matters**: Total capital misallocation. PMM-2 believes orders are live when none exist. No guard prevents config from setting `shadow_mode: false`.

**Proposed Fix**: Add a hard guard at the bridge level:

```python
def _execute_add(self, mutation):
    if not self._shadow_mode:
        raise NotImplementedError(
            "V1 bridge live execution not implemented. "
            "Set shadow_mode=True in pmm2 config."
        )
    # shadow logging...
```

**Why This Solves It**: Cannot accidentally deploy PMM-2 in live mode with stubbed execution. The error is loud and unmissable.

**Blocker**: YES

---

### S-02 [CRITICAL] V1 State Snapshot Is Not Atomic

**Root Cause**: `pmm2/shadow/v1_snapshot.py:59-142` — `capture()` iterates over `order_tracker.get_active_orders()` and `position_tracker.get_active_positions()` sequentially with no lock or copy. The main quote loop modifies orders and positions concurrently.

**Why It Matters**: Counterfactual comparisons built on inconsistent snapshots produce unreliable launch-readiness gates. Could pass Gate 1 on noise.

**Proposed Fix**: Use a copy-on-read pattern. Snapshot the trackers' internal dicts at a single point (dict.copy() is atomic in CPython due to GIL for simple dicts). Or add an `asyncio.Lock` that the main loop acquires briefly during snapshot capture.

**Why This Solves It**: Snapshot is internally consistent. Gate evaluations are reliable.

**Blocker**: YES

---

### S-03 [CRITICAL] Fire-and-Forget Tasks Swallow Exceptions Silently

**Root Cause**: `pmm1/main.py:586-600, 629, 1516` — multiple `asyncio.create_task(...)` calls with no `add_done_callback` or exception handler. If `fill_recorder.record_fill()` raises, the exception is silently eaten.

**Why It Matters**: Missed fill records corrupt markout tracking and PnL accounting. No operator visibility.

**Proposed Fix**: Add a done callback that logs exceptions:

```python
def _task_exception_handler(task: asyncio.Task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("background_task_failed", task=task.get_name(), error=str(exc))

task = asyncio.create_task(fill_recorder.record_fill(...))
task.add_done_callback(_task_exception_handler)
```

**Why This Solves It**: Exceptions are logged and visible. No silent data corruption.

**Blocker**: YES

---

### S-04 [CRITICAL] Parquet Flush Is Synchronous Blocking I/O in Async Event Loop

**Root Cause**: `pmm1/storage/parquet.py:73-118` — `flush()` calls `pl.read_parquet()` and `df.write_parquet()` which are synchronous disk I/O. Called from `buffer()` in the async event loop.

**Why It Matters**: Blocks the entire event loop. During disk I/O, no WS messages processed, no heartbeats sent. If >10s, kill switch fires.

**Proposed Fix**: Wrap in `asyncio.to_thread()`:

```python
async def flush(self):
    await asyncio.to_thread(self._sync_flush)

def _sync_flush(self):
    # existing read_parquet/write_parquet logic
```

**Why This Solves It**: Disk I/O runs in a thread. Event loop remains responsive.

**Blocker**: YES

---

### S-05 [HIGH] SQLite Concurrent Access Without Serialization

**Root Cause**: `pmm1/storage/database.py:64-115` wraps a single `aiosqlite.Connection`. Multiple concurrent callers all call `execute()` → `commit()` without transaction batching. aiosqlite serializes through a background thread, but interleaved commit/execute can produce partial states.

**Why It Matters**: Under load, `SQLITE_BUSY` errors. Scoring history and fill records may be partially committed.

**Proposed Fix**: Use WAL mode (likely already set), add a connection pool or serialized write queue. Or migrate to Postgres (as spec requires).

**Why This Solves It**: Proper concurrency handling eliminates write conflicts.

**Blocker**: NO

---

### S-06 [HIGH] Redis Has No Reconnection Logic

**Root Cause**: `pmm1/storage/redis.py:46-69` creates a single `aioredis.from_url()` connection. No pool, no auto-reconnection, no retry wrapper.

**Why It Matters**: If Redis restarts, V3 signal reads fail until manual bot restart. V1's hot state becomes stale.

**Proposed Fix**: Use `aioredis.ConnectionPool` with automatic reconnection. Wrap reads in a retry decorator with exponential backoff.

**Why This Solves It**: Transient Redis failures are handled automatically.

**Blocker**: NO

---

### S-07 [HIGH] Postgres Failure Cascades Into 30s Blocking Connect

**Root Cause**: `pmm2/runtime/loops.py:276, 491-492` — allocator loop calls Postgres operations. `_ensure_pool()` attempts full reconnect with 30s timeout on each call. During Postgres outage, every loop iteration blocks 30s.

**Why It Matters**: 60s cycle becomes 90+s. Stale allocation decisions accumulate.

**Proposed Fix**: Add a circuit breaker for Postgres: after 3 failed connects, stop attempting for 5 minutes. Use a health check task instead of reconnecting on every call.

**Why This Solves It**: Postgres failures are isolated. Allocator degrades gracefully with cached decisions.

**Blocker**: NO

---

### S-08 [HIGH] WS Reconnect Does Not Rebuild Book State From REST

**Root Cause**: `pmm1/ws/market_ws.py:278-303` — on reconnect, resubscribes and waits for WS to send snapshots. No explicit REST book fetch. Between disconnect and new snapshot, local books are stale.

**Why It Matters**: Quotes may be placed at adverse prices during the gap.

**Proposed Fix**: After reconnect, immediately fetch books via REST for all subscribed assets before clearing the `_reconnecting` flag.

**Why This Solves It**: Book state is explicitly refreshed from REST. No gap of stale data.

**Blocker**: NO

---

### S-09 [HIGH] User WS Sends Raw API Credentials — Logged at Debug Level

**Root Cause**: `pmm1/ws/user_ws.py:83-97` — `apiKey`, `secret`, `passphrase` sent in JSON payload. Logged at debug level. Credentials persist in log files.

**Why It Matters**: Log access = full CLOB API control.

**Proposed Fix**: Redact credentials in log output. Log only `apiKey[:6]...` for debugging.

**Why This Solves It**: Credentials are not stored in logs.

**Blocker**: NO

---

### S-10 [HIGH] No Config Hot-Reload

**Root Cause**: `pmm1/settings.py:275-357` — Settings loaded once at startup. `CanaryRamp` writes to YAML on disk, but the running process never re-reads. Config changes require full restart (canceling all orders).

**Why It Matters**: Cannot hot-toggle V3 kill switch. Cannot adjust risk limits without disruption.

**Proposed Fix**: Add a SIGHUP handler or file watcher that re-reads config and updates the Settings object. Gate critical fields (risk limits, kill switches) behind atomic swap.

**Why This Solves It**: Config changes take effect without restart. No quote gap.

**Blocker**: NO

---

### S-11 [HIGH] Naive datetime (`utcnow()`) vs Aware datetime Mismatches

**Root Cause**: `v3/shadow/main.py:151`, `runner.py:88, 155, 226, 227` use `datetime.utcnow()` (naive). V3 integrator uses `datetime.now(timezone.utc)` (aware). Comparing them raises `TypeError`.

**Why It Matters**: Potential runtime crash in signal age comparison. The `.replace(tzinfo=...)` workaround is fragile.

**Proposed Fix**: Use `datetime.now(timezone.utc)` everywhere. Global search-and-replace `datetime.utcnow()`.

**Why This Solves It**: All datetimes are timezone-aware. No comparison errors.

**Blocker**: NO

---

### S-12 [HIGH] Google OAuth Client Secret Embedded in Source Code

**Root Cause**: `v3/providers/google_adapter.py:30-35` — `CLIENT_ID` and `CLIENT_SECRET` are base64-encoded and committed to git. Survived the "scrub hardcoded secrets" commit.

**Why It Matters**: Credential exposure. Any repo reader can decode.

**Proposed Fix**: Move to environment variables. Add to `.env.example`. Remove from source code.

**Why This Solves It**: Credentials are not in version control.

**Blocker**: NO

---

### S-13 [HIGH] Provider Auth Loaded From Hardcoded Linux Path

**Root Cause**: `v3/providers/registry.py:51-54` — loads from `/home/ubuntu/.openclaw/agents/main/agent/auth-profiles.json`. Hardcoded path. Won't work on Windows or different Linux users.

**Why It Matters**: V3 providers silently fail on any non-matching deployment.

**Proposed Fix**: Use an environment variable `V3_AUTH_PROFILES_PATH` with a sensible default.

**Why This Solves It**: Portable across deployment environments.

**Blocker**: NO

---

### S-14 [MEDIUM] Notification Module Creates New aiohttp Session Per Message

**Root Cause**: `pmm1/notifications.py:29-31` creates `aiohttp.ClientSession()` per call. Each creates a new TCP connection with TLS handshake.

**Why It Matters**: Overhead (~50-200ms per message). Socket exhaustion under high fill rate.

**Proposed Fix**: Create a single persistent session at startup. Reuse for all messages.

**Why This Solves It**: Connection reuse. No socket exhaustion.

**Blocker**: NO

---

### S-15 [MEDIUM] Fill Dedup Set Uses Unbounded Clear

**Root Cause**: Same as E-06. Already covered.

**Blocker**: NO

---

### S-16 [MEDIUM] V2 Counterfactual EV Baseline Is Arbitrary

**Root Cause**: `pmm2/shadow/counterfactual.py:117-119` — V1's EV estimated as `v1_scoring_count * 0.01`. Hard-coded guess. Gate 1 evaluates against this arbitrary baseline.

**Why It Matters**: Launch readiness gates are unreliable. PMM-2 could pass because baseline is too low.

**Proposed Fix**: Compute V1 actual EV from its fill data and reward history, not a constant.

**Why This Solves It**: Gates compare against real V1 performance.

**Blocker**: NO

---

### S-17 [MEDIUM] V3 Publisher Uses SET, Integrator Uses HGETALL — Format Mismatch

**Root Cause**: `v3/serving/publisher.py:111-119` writes signals via `redis.set()` (string). `v3/canary/integrator.py:127-153` reads via `redis.hgetall()` (hash). Hash read of a string key returns empty dict. The entire canary pipeline is dead on arrival.

**Why It Matters**: V3 signals published are invisible to V1. `get_blended_fair_value()` always returns `book_midpoint`. Canary ramp is non-functional.

**Proposed Fix**: Align on one format. Use `redis.get()` in the integrator to match the publisher's `redis.set()`.

```python
# In integrator:
raw = await self.redis.get(f"v3:signal:{condition_id}")
if raw:
    signal = Signal.model_validate_json(raw)
```

**Why This Solves It**: Publisher and consumer use the same Redis data format.

**Blocker**: YES (when V3 is enabled)

---

### S-18 [MEDIUM] Gamma Sync Fetches Markets Sequentially

**Root Cause**: `v3/intake/gamma_sync.py:105-136` loops over condition IDs one at a time with sequential HTTP requests. 50 markets = 50 sequential calls.

**Why It Matters**: V3 shadow cycle can take 25+ minutes if Gamma is slow.

**Proposed Fix**: Use `asyncio.gather()` with concurrency limiter (semaphore of 10).

**Why This Solves It**: 50 sequential calls become ~5 batches. 5x speedup.

**Blocker**: NO

---

### S-19 [MEDIUM] No Watchdog or Supervisor Configuration

**Root Cause**: No systemd unit file, no OOM score adjustment, no disk space monitor in the codebase.

**Why It Matters**: OOM kill = dead bot. Disk full = silent data corruption. No auto-restart.

**Proposed Fix**: Create a systemd unit with `Restart=always`, `WatchdogSec=60`, and `OOMScoreAdjust=-500`. Add a pre-flight disk space check.

**Why This Solves It**: Process auto-restarts on crash. Watchdog detects hangs. OOM is less likely.

**Blocker**: NO

---

### S-20 [MEDIUM] Mutation Log Grows Without Bound

**Root Cause**: `pmm2/runtime/v1_bridge.py:51, 129, 162` — `self.mutation_log: list[dict]` appends forever. ~2MB/day.

**Why It Matters**: Memory leak. OOM after weeks.

**Proposed Fix**: Trim to last 24h on each append, or use a deque with maxlen.

**Why This Solves It**: Memory stays bounded.

**Blocker**: NO

---

### S-21 [MEDIUM] Counterfactual Metrics Lists Grow Without Bound

**Root Cause**: `pmm2/shadow/counterfactual.py:50-55` — four lists append every cycle, never trimmed.

**Why It Matters**: Minor memory growth. Symptom of no bounded-data-structure discipline.

**Proposed Fix**: Use `collections.deque(maxlen=1000)`.

**Why This Solves It**: Memory bounded. `avg_last_n(100)` still works.

**Blocker**: NO

---

### S-22 [MEDIUM] Risk Config Defaults vs Production Config Divergence

**Root Cause**: `settings.py` defaults are conservative (10% directional). `default.yaml` overrides to aggressive (60%). `prod.yaml` is nearly empty. The "default" config IS the production config.

**Why It Matters**: Confusing. A new operator might modify `prod.yaml` expecting it to matter.

**Proposed Fix**: Rename `default.yaml` to `dev.yaml`. Make `prod.yaml` contain all production values explicitly. Load based on `bot.env`.

**Why This Solves It**: Clear environment separation. No accidental cross-contamination.

**Blocker**: NO

---

### S-23 [LOW] OpenAI Provider Uses Undocumented Codex API Endpoint

**Root Cause**: `v3/providers/openai_adapter.py:24` — `API_BASE = "https://chatgpt.com/backend-api/codex"`. Not the official OpenAI API.

**Why It Matters**: Can break without notice. No stability guarantee.

**Proposed Fix**: Use the official `api.openai.com` endpoint.

**Why This Solves It**: Official API with versioning and deprecation notices.

**Blocker**: NO

---

### S-24 [LOW] Health Checks Fire Real Billable API Requests

**Root Cause**: All three providers' `health_check()` methods send real "Hello" prompts consuming tokens.

**Why It Matters**: Minor cost. Compounds during crash-restart loops.

**Proposed Fix**: Use a dry-run health check (e.g., list models endpoint) instead of a completions call.

**Why This Solves It**: Zero-cost health checks.

**Blocker**: NO

---

### S-25 [LOW] V3 Shadow DB DSN Hardcoded in Source

**Root Cause**: `v3/shadow/main.py:33` — `"postgresql://mmbot:mmbot_v3_2026@localhost/mistermoney_v3"` committed to git.

**Why It Matters**: Credentials in version control.

**Proposed Fix**: Move to environment variable.

**Why This Solves It**: Credentials not in source.

**Blocker**: NO

---

### S-26 [LOW] No API Version Pinning for Polymarket Endpoints

**Root Cause**: `pmm1/settings.py:117-129` — all endpoints are bare URLs with no version path. `py-clob-client>=0.15.0` uses `>=` not `~=`.

**Why It Matters**: API changes could silently break order submission.

**Proposed Fix**: Pin to `~=0.15.0`. Add schema validation on API responses.

**Why This Solves It**: Breaking changes are detected at dependency update time.

**Blocker**: NO

---

### S-27 [LOW] Logging Lacks Correlation IDs for Forensics

**Root Cause**: Critical paths lack detail for post-incident reconstruction. No correlation ID links a quote cycle to its orders and fills.

**Why It Matters**: After an incident, root cause analysis requires cross-referencing multiple log streams.

**Proposed Fix**: Add a `cycle_id` (UUID) to each quote cycle. Propagate to all resulting orders and fills.

**Why This Solves It**: Any log entry can be traced to its originating cycle.

**Blocker**: NO

---

## Auditor 5: Adversarial / Red Team (14 Findings)

### A-01 [CRITICAL] Cancel-Then-Submit Race: Partial Fill During Repricing

**Root Cause**: `OrderManager.diff_and_apply()` executes cancel-then-submit sequentially. Between cancel REST and exchange processing, a fill can arrive on WS. Combined with dedup clear (E-06), fills can be double-counted.

**Why It Matters**: Double-counted fill of max order size ($15). Bot quotes against phantom inventory. Self-heals via reconciliation in 30-60s, but interim state is corrupt.

**Proposed Fix**: Use `cancel_and_replace` atomic operation if CLOB supports it. If not, gate new submissions on cancel confirmation (wait for WS ack of cancel before submitting).

**Why This Solves It**: No window for stale fills during repricing.

**Blocker**: NO (self-heals, but causes intermittent position errors)

---

### A-02 [CRITICAL] Neg-Risk Arb Leg Risk (Cross-Reference with E-02)

**Root Cause**: Same as E-02. Non-atomic multi-leg execution with no rollback.

**Why It Matters**: $1.50-$3.75 per failed arb attempt. Can accumulate.

**Proposed Fix**: Same as E-02.

**Blocker**: YES

---

### A-03 [CRITICAL] Config Footgun — YAML Risk Limits Are a Deployment Time-Bomb

**Root Cause**: Same as R-01. 3-6x looser than spec. No validation that combined limits are mathematically consistent (12 markets * 8% = 96% > 100% available capital).

**Why It Matters**: $12 loss on a bad day. No pre-flight check warns.

**Proposed Fix**: Same as R-01, plus add a startup validation:

```python
if num_markets * per_market_gross_nav > 0.80:
    raise ValueError(f"Combined market limits ({num_markets * per_market_gross_nav:.0%}) "
                     f"exceed 80% of NAV — reduce num_markets or per_market_gross_nav")
```

**Why This Solves It**: Impossible to deploy with limits that exceed total capital.

**Blocker**: YES

---

### A-04 [HIGH] Toxic Flow Extraction via Predictable Repricing Cadence

**Root Cause**: Fixed 1-second cycle, deterministic reservation price formula, top-of-book clamp makes price predictable, fill escalation makes bot MORE aggressive when not filling (opposite of correct AS response).

**Why It Matters**: $1-5/day in persistent adverse selection drag.

**Proposed Fix**: Add random jitter to cycle timing (±200ms). Remove top-of-book clamp (R-07). Reverse fill escalation direction (widen when not filling, not tighten).

**Why This Solves It**: Repricing is less predictable. Inventory skew works. Fill escalation moves in the correct direction.

**Blocker**: NO

---

### A-05 [HIGH] Exchange Restart Auto-Clear Timing Mismatch

**Root Cause**: `kill_switch.py:116` auto-clears stale feed after 30s. Exchange restart takes 90s. If market WS reconnects before user WS, bot resumes with stale order state. Can create duplicate exposure of $360 on $100 NAV.

**Why It Matters**: Weekly occurrence (every Tuesday). Concrete, recurring loss potential.

**Proposed Fix**: Increase `auto_clear_s` to 120s+. Better: don't auto-clear at all — require BOTH WebSockets connected AND successful reconciliation.

```python
def should_clear_stale_feed(self) -> bool:
    return (self._market_ws.is_connected
            and self._user_ws.is_connected
            and self._reconciler.last_success_age_s < 30)
```

**Why This Solves It**: Kill switch only clears when the system has provably good state.

**Blocker**: YES

---

### A-06 [HIGH] Stale V3 Signal During Canary Skew

**Root Cause**: V3 signal decays toward market mid per route half-life. During decay, canary skew pushes fair value based on stale information. Clamped to max_skew_cents (1c default, up to 5c at canary_5c stage).

**Why It Matters**: At canary_5c: $1.50/market/cycle edge donated. V3 currently disabled.

**Proposed Fix**: Auto-retreat to lower canary stage when signal age exceeds half-life. At full decay, remove canary skew entirely.

**Why This Solves It**: Stale signals automatically lose influence.

**Blocker**: NO (V3 disabled)

---

### A-07 [HIGH] Network Partition: 5-Minute Internet Drop

**Root Cause**: Internet drops → WS disconnect → kill switch → cancel_all (but REST fails) → orders stay live. Exchange auto-cancels at T+15s via heartbeat timeout. Between T+0 and T+15s, orders are live and unmonitored.

**Why It Matters**: 15-second window of live, unmonitored orders. Fills during this window are unknown until reconnect.

**Proposed Fix**: This is actually well-handled by the exchange heartbeat. The main improvement: pre-load position state on startup from exchange (don't rely solely on async reconciliation).

**Why This Solves It**: Crash-recovery starts with known position state.

**Blocker**: NO

---

### A-08 [CRITICAL] Event Cluster Limits Bypassed — event_id=""

**Root Cause**: `main.py:216` hardcodes `event_id=""` for markets fetched from the `/markets` endpoint. The per-event cluster limit checks against event_id, but empty string means all markets are in the same "cluster" — or none, depending on implementation. In practice, the limit is bypassed.

**Why It Matters**: Correlated positions across markets in the same event are unlimited. A multi-outcome event resolving adversely could wipe 45% NAV.

**Proposed Fix**: Fetch event_id from the Gamma API (it's available on the market object). Map condition_id to event_id during universe construction.

**Why This Solves It**: Per-event cluster limits actually enforce across related markets.

**Blocker**: YES

---

### A-09 [CRITICAL] Key/Wallet Compromise = 100% Drain

**Root Cause**: Private key in plaintext in environment. No HSM, no multi-sig, no spending limit, no withdrawal whitelist. `setApprovalForAll` granted to three exchange operators. Full CLOB API access via api key/secret/passphrase.

**Why It Matters**: 100% NAV loss. No mitigation whatsoever.

**Proposed Fix**: (a) Use a hardware wallet or HSM for signing. (b) Deploy a smart contract wallet with daily spending limits and whitelist. (c) At minimum: rotate API keys regularly, use IP whitelisting on Polymarket API, limit wallet balance to operational needs (don't hold excess capital).

**Why This Solves It**: Even if private key leaks, spending limits and whitelists cap the damage.

**Blocker**: YES

---

### A-10 [MEDIUM] Reward Rug: Formula Change Detection Lag

**Root Cause**: No comparison of expected vs actual reward revenue. No alert if scoring rate drops. No circuit breaker on reward formula changes.

**Why It Matters**: Bot tightens spreads for rewards that no longer exist. Days of loss before detection.

**Proposed Fix**: Track actual reward income per epoch vs expected. Alert if actual < 50% of expected for 3 consecutive epochs.

**Why This Solves It**: Formula changes are detected within hours, not days.

**Blocker**: NO

---

### A-11 [MEDIUM] Order State Machine Silent on Invalid Transitions (Cross-ref E-03)

**Root Cause**: Same as E-03.

**Blocker**: NO (covered by E-03 fix)

---

### A-12 [MEDIUM] Fill Dedup Replay Vulnerability (Cross-ref E-06)

**Root Cause**: Same as E-06.

**Blocker**: NO

---

### A-13 [MEDIUM] All State Is In-Memory — Crash Loses Everything

**Root Cause**: Positions, orders, books are in-memory Python dicts. A crash loses ALL state. On restart, positions are not fetched until first reconciliation (30-60s). Bot could over-deploy during that window.

**Why It Matters**: 30-60s of unprotected quoting after every restart.

**Proposed Fix**: Pre-load positions from exchange REST API at startup, before entering the quote loop.

**Why This Solves It**: Bot starts with correct position state. No unprotected window.

**Blocker**: NO

---

### A-14 [LOW] Binary Parity Arb Non-Atomicity (Cross-ref E-09)

**Root Cause**: Same as E-09. Bounded at $3.75 per failed attempt.

**Blocker**: NO

---

### A-15 [LOW] CTF Approval Check Fails Open

**Root Cause**: `ctf_approval.py:57, 103` returns `True` when web3 is not installed or RPC fails. Bot proceeds with missing approvals. All sell orders fail silently.

**Why It Matters**: Bot becomes a one-directional buyer.

**Proposed Fix**: Fail closed: return `False` and block startup if approvals can't be verified.

**Why This Solves It**: Cannot deploy without verified sell capability.

**Blocker**: NO

---

## Auditor 6: Gap-Finding & Completeness (32 Findings)

### G-01 [CRITICAL] No Automated Test Suite — tests/ Directory Empty

**Root Cause**: `tests/unit/`, `tests/integration/`, `tests/chaos/` contain only empty `__init__.py` files. Zero structured tests exist. The spec mandates unit tests for tick rounding, neg-risk math, parity detector, heartbeat state, risk limits, and chaos tests.

**Why It Matters**: Any code change can silently break execution logic. A single regression in tick rounding can cause mass-cancellations or losses.

**Proposed Fix**: Implement the spec-mandated test suite. Minimum: tick rounding (unit), order state machine (unit), kill switch transitions (unit), heartbeat timing (unit), risk limit enforcement (integration), WS reconnect (integration).

**Why This Solves It**: Regression protection for money-handling code. CI can gate deployments.

**Blocker**: YES

---

### G-02 [CRITICAL] Existing Tests Are Not pytest-Compatible

**Root Cause**: Test files (`test_pmm2_universe.py`, `test_scorer.py`, etc.) are standalone scripts using `print("pass")` assertions. `pyproject.toml` sets `testpaths = ["tests"]` but tests/ is empty. Running `pytest` finds zero tests.

**Why It Matters**: Impossible to gate deployments on test passage. Cannot run in CI.

**Proposed Fix**: Convert existing test scripts to proper pytest tests with `assert` statements and fixtures. Move into `tests/` directory structure.

**Why This Solves It**: `pytest` discovers and runs all tests. CI integration possible.

**Blocker**: YES

---

### G-03 [CRITICAL] Zero V1 Execution Core Tests

**Root Cause**: `pmm1/` (10,400 lines) has zero tests. No test for order signing, tick rounding, heartbeat state, kill switch, inventory, book management, order diffing, reconciliation, or error handling.

**Why It Matters**: The code handling real money has zero regression protection.

**Proposed Fix**: Prioritize tests for the highest-risk modules: tick_rounding.py, kill_switch.py, order state machine, diff_and_apply logic. Start with pure-function unit tests that require no mocking.

**Why This Solves It**: The most critical code paths have regression guards.

**Blocker**: YES

---

### G-04 [CRITICAL] No Production Alerting or Paging

**Root Cause**: No Prometheus, Grafana, PagerDuty, or any alerting system. Only Telegram (hardcoded chat_id). No alerts for heartbeat failures, drawdown breaches, limit violations, WS disconnects, or fill anomalies.

**Why It Matters**: At 3 AM when the bot flattens, nobody is paged. Losses discovered next morning.

**Proposed Fix**: Implement critical Telegram alerts (not just fill notifications) for: kill switch activation, drawdown tier changes, reconciliation mismatches, position limit breaches. This is the minimum — full monitoring (Prometheus + Grafana) is the target.

**Why This Solves It**: Operator is immediately notified of critical events.

**Blocker**: YES

---

### G-05 [CRITICAL] No Runbook or Incident Response Procedure

**Root Cause**: Zero runbook files. No procedure for bot flattening, heartbeat mass-cancel, exchange 503 mode, wallet gas depletion, or stuck transactions.

**Why It Matters**: Incident response is ad hoc. Recovery time depends on tribal knowledge.

**Proposed Fix**: Create `docs/RUNBOOK.md` covering: (1) emergency stop procedure, (2) bot restart checklist, (3) exchange restart handling, (4) drawdown investigation, (5) position reconciliation mismatch, (6) wallet gas refill.

**Why This Solves It**: Any operator can handle incidents without guessing.

**Blocker**: YES

---

### G-06 [HIGH] Storage Architecture Diverges From Spec — SQLite Not Postgres

**Root Cause**: Spec mandates Redis + Postgres. V1 uses SQLite. V3 uses Postgres + Redis. Split-brain storage.

**Why It Matters**: SQLite is single-writer, unsuitable for concurrent access from V1 + V2 + V3.

**Proposed Fix**: Migrate V1 to Postgres. The schema and queries are simple — mostly fills, book snapshots, scoring history.

**Why This Solves It**: Uniform storage. Proper concurrent access. Spec compliance.

**Blocker**: NO (works for current single-process scale)

---

### G-07 [HIGH] V3 Providers Partially Broken — Only 2 of 5 Working

**Root Cause**: OpenAI GPT-5.4 has API format mismatch. Google Gemini has expired OAuth. Two of four V3 routes require broken providers.

**Why It Matters**: V3 cannot produce multi-model signals. Canary rollout blocked.

**Proposed Fix**: Fix OpenAI adapter to use official API. Implement OAuth token refresh for Google. Test each provider on startup.

**Why This Solves It**: All V3 routes have functioning providers.

**Blocker**: YES (for V3 go-live)

---

### G-08 [HIGH] Spec-vs-Config Risk Limit Divergence (Cross-ref R-01)

**Root Cause**: Same as R-01.

**Blocker**: YES

---

### G-09 [HIGH] require_clear_rules: false in Production

**Root Cause**: Spec mandates no ambiguous resolution rules. Config disables this check.

**Why It Matters**: Bot trades markets where resolution itself is disputed.

**Proposed Fix**: Set `require_clear_rules: true`. If this filters too many markets, review and whitelist specific markets manually.

**Why This Solves It**: Only markets with clear payout rules are traded.

**Blocker**: NO

---

### G-10 [HIGH] No Cold-Start Data Plan for V2 Calibration

**Root Cause**: V2 calibration needs book snapshots, fill history, reward actuals. None exist yet.

**Why It Matters**: V2 allocator operates on uncalibrated models.

**Proposed Fix**: Run V1 with data recording enabled for 4+ weeks before V2 shadow evaluation. Document the required data collection period.

**Why This Solves It**: V2 calibration has real data to fit against.

**Blocker**: NO (V2 is shadow-only)

---

### G-11 [HIGH] No Cold-Start Data for V3 — 50+ Resolved Markets Needed

**Root Cause**: V3 calibration gate requires 50+ resolved markets. Takes months to collect.

**Why It Matters**: V3 provides zero value over midpoint during cold start.

**Proposed Fix**: Acknowledge timeline. Run V3 shadow in parallel with V1 live. Track resolved market count as a KPI.

**Why This Solves It**: Sets realistic expectations. Data collection proceeds in background.

**Blocker**: NO

---

### G-12 [HIGH] Sell Logic Position vs MarketPosition Class Bug

**Root Cause**: SELL-LOGIC-SPEC.md documents `Position(...)` should be `MarketPosition(...)`. Fix status unknown.

**Why It Matters**: Auto-adopted positions crash the position tracker with NameError.

**Proposed Fix**: Verify the code uses `MarketPosition`. If not, fix it.

**Why This Solves It**: Position adoption works correctly.

**Blocker**: YES (if unfixed)

---

### G-13 [HIGH] No CTF Token Approval Check on Startup

**Root Cause**: Spec mandates startup check for ERC-1155 `setApprovalForAll`. Completion status unknown.

**Why It Matters**: Without approvals, all sell orders fail. Bot accumulates inventory with no exit.

**Proposed Fix**: Verify the check exists and runs at startup. If not, implement it with fail-closed behavior.

**Why This Solves It**: Cannot deploy without verified sell capability.

**Blocker**: YES (if unimplemented)

---

### G-14 [MEDIUM] Trailing Stop Deferred to "v2"

**Root Cause**: SELL-LOGIC-SPEC.md sets `trailing_enabled: false`. No follow-up.

**Why It Matters**: Sub-optimal exit timing on trending markets.

**Proposed Fix**: Implement trailing stop as an exit layer option. Not a blocker but improves PnL.

**Why This Solves It**: Captures more profit on trending positions.

**Blocker**: NO

---

### G-15 [MEDIUM] No Wallet Gas/POL Monitoring

**Root Cause**: No monitoring of POL balance. If wallet runs out of gas, on-chain operations fail silently.

**Why It Matters**: Emergency cancels fail. Token redemptions fail.

**Proposed Fix**: Check POL balance at startup and every hour. Alert if below 0.5 POL.

**Why This Solves It**: Gas depletion is detected before it causes failures.

**Blocker**: NO

---

### G-16 [MEDIUM] No Fill-to-Signal Latency Tracking

**Root Cause**: `MetricsCollector` tracks timestamps but doesn't compute latency distributions or export.

**Why It Matters**: Latency degradation goes undetected.

**Proposed Fix**: Compute P50/P95 fill-to-quote latency per market. Alert on degradation.

**Why This Solves It**: Latency issues are visible.

**Blocker**: NO

---

### G-17 [MEDIUM] No PnL Attribution Dashboard

**Root Cause**: `PnLTracker` defines the model but nothing computes or exports it (cross-ref R-14).

**Why It Matters**: Cannot attribute profitability sources. Flying blind.

**Proposed Fix**: Same as R-14 — wire PnL tracker into live loop. Add periodic export.

**Why This Solves It**: Real-time profitability monitoring.

**Blocker**: NO

---

### G-18 [MEDIUM] prod.yaml Is Empty

**Root Cause**: Only contains `bot: env: prod`. All production parameters come from `default.yaml`.

**Why It Matters**: No environment separation. Dev changes affect production.

**Proposed Fix**: Same as S-22 — rename `default.yaml` to `dev.yaml`, populate `prod.yaml`.

**Why This Solves It**: Clear separation of environments.

**Blocker**: NO

---

### G-19 [MEDIUM] V2 Queue Estimator Has No Calibration Data Path

**Root Cause**: Parameters assumed (beta=0.5). No fitting procedure. No historical data.

**Why It Matters**: Queue estimates systematically biased.

**Proposed Fix**: Same as Q-14 — collect data, fit parameters.

**Why This Solves It**: Queue estimates reflect reality.

**Blocker**: NO

---

### G-20 [MEDIUM] No CI/CD Pipeline

**Root Cause**: No GitHub Actions, Dockerfile, or Makefile. Manual deployment.

**Why It Matters**: Type errors and broken imports ship to production.

**Proposed Fix**: Create `.github/workflows/ci.yml` with: ruff lint, mypy type check, pytest run. Gate merges on CI passing.

**Why This Solves It**: Automated quality gates before deployment.

**Blocker**: NO

---

### G-21 [MEDIUM] Hardcoded Credentials in Code

**Root Cause**: DB DSN with password in V3 shadow main. Telegram chat_id hardcoded. Survived "scrub secrets" commit.

**Why It Matters**: Credentials in version control. Cannot rotate without code changes.

**Proposed Fix**: Move all credentials to `.env`. Add pre-commit hook to scan for credential patterns.

**Why This Solves It**: Credentials are not in source code.

**Blocker**: NO

---

### G-22 [MEDIUM] No Disaster Recovery or Backup

**Root Cause**: All data stored locally. No backup procedure.

**Why It Matters**: Hardware failure destroys all historical data.

**Proposed Fix**: Automated daily backup of SQLite/Postgres to cloud storage. Document recovery procedure.

**Why This Solves It**: Data survives hardware failure.

**Blocker**: NO

---

### G-23 [MEDIUM] Geoblock Check Is Brittle

**Root Cause**: Falls back to `return True` on error. No periodic re-check.

**Why It Matters**: VPN failure mid-operation not detected.

**Proposed Fix**: Periodic re-check (every 15 min). Fail-closed on error (pause trading, don't assume OK).

**Why This Solves It**: Geo-compliance is continuously verified.

**Blocker**: NO

---

### G-24 [MEDIUM] No ToS Compliance Review

**Root Cause**: No documented review of Polymarket's Terms of Service for automated trading.

**Why It Matters**: Account termination or fund seizure.

**Proposed Fix**: Conduct and document a ToS review. Confirm bot operation is permitted.

**Why This Solves It**: Legal basis for operation is established.

**Blocker**: NO (but operationally critical)

---

### G-25 [MEDIUM] Directional Overlay Disabled With No Graduation Plan

**Root Cause**: V3 canary adds directional skew. Directional overlay is disabled. They're the same capability under different names, not reconciled.

**Why It Matters**: Risk limits designed for non-directional may not apply to V3-skewed quotes.

**Proposed Fix**: Reconcile V3 canary skew with directional overlay concept. Apply directional risk limits when V3 skew is active.

**Why This Solves It**: Risk framework is consistent with actual strategy behavior.

**Blocker**: NO

---

### G-26 [LOW] Backtest Layer Not Validated

**Root Cause**: Paper trading engine exists but no evidence of 30 days of paper trading (spec requirement).

**Why It Matters**: Acceptance criteria unvalidated.

**Proposed Fix**: Run 30 days paper trading with the fixed system. Track and document results.

**Why This Solves It**: Spec acceptance criteria met with evidence.

**Blocker**: NO

---

### G-27 [LOW] V4 Spec Exists but No Implementation

**Root Cause**: V4-SPEC.md is pure design, labeled "Not Yet Implemented."

**Why It Matters**: None immediately. Correctly deferred.

**Proposed Fix**: None needed now.

**Why This Solves It**: N/A.

**Blocker**: NO

---

### G-28 [LOW] Weekly Exchange Restart Not Tested

**Root Cause**: 425 handling code exists but has no tests and no logged evidence of survival.

**Why It Matters**: Untested weekly code path.

**Proposed Fix**: Add an integration test simulating 425 response. Run through one Tuesday restart with shadow logging.

**Why This Solves It**: Weekly code path is verified.

**Blocker**: NO

---

### G-29 [LOW] KYC/Wallet Identity Not Addressed

**Root Cause**: No discussion of wallet KYC/AML. May be required at volume thresholds.

**Why It Matters**: Wallet could be flagged or restricted.

**Proposed Fix**: Research Polymarket's KYC requirements for high-volume makers. Document.

**Why This Solves It**: Operational surprise is prevented.

**Blocker**: NO

---

### G-30 [LOW] allow_sports: true Despite Spec Prohibition

**Root Cause**: Spec says "v1 does NOT trade sports." Config says `allow_sports: true`.

**Why It Matters**: Structural risks for MM on sports markets (game-start cancels, 3s delay).

**Proposed Fix**: Set `allow_sports: false` per spec, or document why the spec was overridden.

**Why This Solves It**: Config aligns with spec or deviation is justified.

**Blocker**: NO

---

### G-31 [LOW] No Dispute Resolution Monitoring

**Root Cause**: No automated monitoring of active disputes on held positions.

**Why It Matters**: Bot holds positions through dispute periods without awareness.

**Proposed Fix**: Poll Gamma API for market status changes. Alert on "disputed" or "clarification_pending."

**Why This Solves It**: Operator is aware of resolution risk.

**Blocker**: NO

---

### G-32 [LOW] V3 OAuth Token Refresh Not Validated

**Root Cause**: Token refresh logic exists but not validated in production. Google OAuth expired after ~3 days.

**Why It Matters**: V3 shadow silently stops.

**Proposed Fix**: Implement proactive token refresh (refresh 10 min before expiry). Alert on refresh failure.

**Why This Solves It**: Continuous V3 operation.

**Blocker**: NO

---

## Kill-or-Fix List

The minimum set of issues that MUST be resolved before any real capital is deployed.

### Tier 0: Before Touching Real Capital (12 items)

| # | Fix | Est. Effort | Finding IDs |
|---|-----|-------------|-------------|
| 1 | Fix drawdown to use high-water mark | 30 min | R-03 |
| 2 | Feed mark-to-market prices into NAV | 2 hrs | R-05 |
| 3 | Tighten risk limits to spec values | 15 min | R-01, A-03, G-08 |
| 4 | Populate event_id from Gamma API | 1 hr | A-08 |
| 5 | Add `return` after generic cancel exception | 5 min | E-01 |
| 6 | Reject terminal→active state transitions | 30 min | E-03 |
| 7 | Route taker bootstrap through risk limits | 1 hr | R-11 |
| 8 | Remove/fix top-of-book clamp for inventory skew | 1 hr | R-07 |
| 9 | Increase stale feed auto_clear_s to 120s+ | 5 min | A-05 |
| 10 | Wrap Parquet flush in asyncio.to_thread() | 15 min | S-04 |
| 11 | Add done callbacks to fire-and-forget tasks | 30 min | S-03 |
| 12 | Wire QueueEstimator into scorer (remove queue_ahead=0) | 2 hrs | Q-02 |

### Tier 1: Before Scaling Past $100 NAV (8 items)

| # | Fix | Est. Effort | Finding IDs |
|---|-----|-------------|-------------|
| 13 | Floor toxicity at zero | 15 min | Q-03 |
| 14 | Basic cross-event correlation grouping | 1 day | R-02 |
| 15 | pytest suite for tick rounding, kill switch, heartbeat, order state | 2-3 days | G-01, G-02, G-03 |
| 16 | Production alerting (Telegram alerts for critical events) | 1 day | G-04 |
| 17 | Basic runbook | 4 hrs | G-05 |
| 18 | Fix fill calibrator placeholder | 2 hrs | Q-07 |
| 19 | LRU cache for fill dedup | 30 min | E-06 |
| 20 | Move hardcoded credentials to env vars | 2 hrs | S-12, S-25, G-21 |

---

## Risk Matrix — Top 20 Failure Scenarios

| # | Scenario | P(occur) | Impact (% NAV) | E[Loss] | IDs |
|---|----------|----------|----------------|---------|-----|
| 1 | Risk limits 6x loose + correlated | Certain | 60% | Continuous | R-01, R-02, A-03 |
| 2 | Drawdown blind (cost basis + no HWM) | Certain | 100% unprotected | Continuous | R-03, R-05 |
| 3 | Queue=0, all EV is fantasy | Certain | Misallocation | Continuous | Q-02 |
| 4 | Fair value = midpoint, zero alpha | Certain | 0% edge | Continuous | Q-01 |
| 5 | Event cluster bypass (event_id="") | 0.80 | 45% | $3.60/event | A-08 |
| 6 | Exchange restart + 30s auto-clear | Weekly | 10-260% exposure | $5.00/week | A-05 |
| 7 | Cancel failure → double exposure | 0.05/day | 15-20% | $0.75-1.00/day | E-01 |
| 8 | Neg-risk arb partial fill | 0.10/day | 3-8% | $0.30-0.80/day | E-02, A-02 |
| 9 | Key compromise → full drain | 0.02/year | 100% | $2.00/year | A-09 |
| 10 | Inventory skew nullified | Certain | Accumulation | Continuous | R-07 |
| 11 | Parquet blocking → kill switch | 0.01/hour | All orders canceled | Disruption | S-04 |
| 12 | Silent fill record loss | 0.05/day | PnL corruption | Unquantifiable | S-03 |
| 13 | Toxic flow extraction | Continuous | $108/hr theoretical | $1-5/day | A-04 |
| 14 | Fill dedup clear → double position | 0.02/day | 15% phantom | $0.30/day | E-06 |
| 15 | Taker bootstrap bypasses limits | 0.05/day | 5% | $0.25/day | R-11 |
| 16 | Redis failure → V3 lost | 0.01/week | V3 degradation | Operational | S-06 |
| 17 | Ghost orders (CANCELED→LIVE) | 0.02/day | Unquoted markets | Revenue loss | E-03 |
| 18 | SQLite BUSY under load | 0.10/day | Write failures | Data loss | S-05 |
| 19 | Stale V3 signal + canary | Future | 5c mispricing | $1.50/mkt/cycle | A-06 |
| 20 | V3 Redis format mismatch | When V3 on | Pipeline dead | Non-functional | S-17 |

---

## Architecture Verdict

This system is not ready for live trading at $100, $500, or $5,000 NAV.

The gap is not incremental — it is structural. The drawdown governor, the system's last line of defense, is doubly broken: it computes from day-start instead of high-water mark, and it uses cost-basis NAV instead of mark-to-market, making it blind to both intraday peak-to-trough drops and unrealized losses. The inventory skew model — the core mechanism for inventory management in an Avellaneda-Stoikov market maker — is nullified by a top-of-book clamp that forces quotes to best bid regardless of inventory. The fair value model is the identity function (zero alpha). Queue position is hardcoded to zero, making every EV calculation in the seven-component scorer a fiction. Risk limits in production are 6x looser than the spec's own "Hard Caps," and per-event cluster limits are unenforced because event_id is empty. The neg-risk arb — marketed as a revenue source — has no on-chain conversion step and no atomicity across legs. The fill dedup mechanism clears its entire history every 500 entries. Fire-and-forget tasks silently swallow exceptions. The V2 bridge execution methods are stubbed to `return True`. The V3 canary pipeline has a Redis format mismatch that makes it dead on arrival. There are zero automated tests for the V1 execution engine — the 10,400 lines of code that sign and submit orders with real money.

The codebase has the shape of a sophisticated quantitative system — three-layer architecture, seven-component EV model, five-layer exit system, conformal prediction intervals — but the substance is largely placeholder. Coefficients are defaults. Models are unfit. Calibrators calibrate against constants. The spec describes a system that does not exist yet; the code describes a system that needs 2-4 weeks of focused remediation before the Tier 0 fixes alone are complete.

**To reach $100 NAV readiness**: Complete the 12 Tier 0 fixes, add basic pytest coverage for execution core, and run 30 days of paper trading with the fixed system.

**To reach $500 NAV readiness**: Additionally complete Tier 1 fixes, validate queue position model empirically, and implement correlation-aware position limits.

**To reach $5,000 NAV readiness**: The entire quantitative stack needs to be rebuilt — fit the fair value model, validate reward proxy, calibrate fill hazard from data, implement proper PnL attribution, and add comprehensive monitoring/alerting infrastructure.

The system is a promising research prototype. It is not a production trading system.

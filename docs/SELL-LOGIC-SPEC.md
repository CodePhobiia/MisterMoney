# MisterMoney — Sell & Exit Logic Specification

**Version**: 1.0  
**Date**: 2026-03-07  
**Status**: SPEC — ready for implementation  

---

## 1. Problem Statement

PMM-1 currently operates as a **buy-only market maker**. It posts BUY orders to accumulate positions but has no systematic way to:

1. Exit positions profitably
2. Cut losses on adverse positions
3. Unwind before market resolution
4. Manage inventory turnover (the whole point of market making)

The bot is a market maker — it should be **inventory-neutral over time**. Every share bought must eventually be sold. Without sell logic, inventory accumulates indefinitely until resolution, turning a market maker into a blind directional bettor.

---

## 2. Architecture Overview

Five sell mechanisms, layered from passive to aggressive:

```
┌─────────────────────────────────────────────────────────┐
│  Layer 5: EMERGENCY FLATTEN (operator command)          │
│  ─────────────────────────────────────────────────────  │
│  Layer 4: RESOLUTION EXIT (time-based, mandatory)       │
│  ─────────────────────────────────────────────────────  │
│  Layer 3: STOP-LOSS (drawdown-triggered, mandatory)     │
│  ─────────────────────────────────────────────────────  │
│  Layer 2: TAKE-PROFIT (threshold-triggered, optional)   │
│  ─────────────────────────────────────────────────────  │
│  Layer 1: INVENTORY-AWARE TWO-SIDED QUOTING (passive)   │
└─────────────────────────────────────────────────────────┘
```

**Layer 1** handles 90% of exits. Layers 2–4 are safety nets. Layer 5 is the panic button.

---

## 3. Layer 1 — Inventory-Aware Two-Sided Quoting

### 3.1 Current State

The `QuoteEngine.compute_quote()` already computes both bid and ask prices with **inventory skew** (Avellaneda-Stoikov style):

```python
# Reservation price with inventory penalty
r_t = fair_value - γ·q_t - η·q_cluster

# Asymmetric sizing already exists:
# Long YES → smaller bids (don't accumulate), larger asks (exit faster)
bid_size *= max(0.3, 1.0 - 0.05 * market_inventory)
ask_size *= min(2.0, 1.0 + 0.03 * market_inventory)
```

But `main.py` **kills the ask** when `market_inv <= 0`:

```python
if market_inv <= 0:
    quote_intent.ask_size = None   # ← This removes all sells
    quote_intent.ask_price = None
```

### 3.2 Fix

**Replace the blanket ask suppression with proper inventory-aware logic:**

```python
# SELL LOGIC
if not paper_mode:
    if market_inv <= 0:
        # No YES inventory → no sells on YES side
        quote_intent.ask_size = None
        quote_intent.ask_price = None
    else:
        # We hold inventory → post asks to unwind
        # Cap ask size to actual holdings
        quote_intent.ask_size = min(quote_intent.ask_size or 0, market_inv)
        
        # Enforce Polymarket minimums
        if quote_intent.ask_size < 5.0:
            if market_inv >= 5.0:
                quote_intent.ask_size = 5.0
            else:
                quote_intent.ask_size = None
                quote_intent.ask_price = None
```

This is **already implemented** (from earlier today). ✅

### 3.3 Inventory Skew Amplification

Current γ (inventory_skew_gamma) is 0.015. For a 25-share position, this shifts reservation price by only 0.375¢ — too small. The skew should be **aggressive enough to mean-revert within a few quote cycles**.

**Proposal**: Dynamic γ that scales with position age and size:

```python
# In config
inventory_skew_gamma: 0.015        # base (unchanged)
inventory_skew_gamma_max: 0.05     # max skew for aged inventory
inventory_age_halflife_hours: 4.0  # halftime to reach max skew

# In compute_reservation_price()
age_hours = (now - position.last_update) / 3600
age_factor = 1.0 - math.exp(-0.693 * age_hours / halflife)
effective_gamma = gamma_base + (gamma_max - gamma_base) * age_factor
```

**Effect**: Fresh inventory uses base γ (gentle skew). Inventory held >4 hours gets 3x the skew, making asks much more competitive.

---

## 4. Layer 2 — Take-Profit

### 4.1 Trigger Condition

```
unrealized_pnl_pct = (current_price - avg_entry) / avg_entry
IF unrealized_pnl_pct >= take_profit_threshold THEN trigger
```

### 4.2 Configuration

```yaml
exit:
  take_profit:
    enabled: true
    threshold_pct: 0.15          # +15% unrealized → take profit
    partial_exit_pct: 0.50       # sell 50% of position
    full_exit_pct: 0.30          # +30% → sell 100%
    min_hold_minutes: 30         # don't take profit on positions < 30 min old
    cooldown_minutes: 10         # after partial exit, wait 10 min before re-evaluating
```

### 4.3 Execution

Take-profit sells are **aggressive limit orders** (at best bid), not passive asks:

```python
async def check_take_profit(position, current_price):
    if position.yes_size <= 0 or position.yes_avg_price <= 0:
        return None
    
    unrealized_pct = (current_price - position.yes_avg_price) / position.yes_avg_price
    hold_time = time.time() - position.last_update
    
    if hold_time < min_hold_minutes * 60:
        return None
    
    if unrealized_pct >= full_exit_pct:
        return SellSignal(size=position.yes_size, urgency="high", reason="full_take_profit")
    elif unrealized_pct >= threshold_pct:
        partial = position.yes_size * partial_exit_pct
        return SellSignal(size=partial, urgency="medium", reason="partial_take_profit")
    
    return None
```

### 4.4 Pricing

- **Medium urgency**: Post at `best_bid` (join the queue)
- **High urgency**: Post at `best_bid - 1 tick` (cross to fill immediately)

---

## 5. Layer 3 — Stop-Loss

### 5.1 Trigger Condition

```
unrealized_pnl_pct = (current_price - avg_entry) / avg_entry
IF unrealized_pnl_pct <= -stop_loss_threshold THEN trigger
```

### 5.2 Configuration

```yaml
exit:
  stop_loss:
    enabled: true
    threshold_pct: 0.20          # -20% unrealized → stop loss
    hard_stop_pct: 0.40          # -40% → immediate full exit
    trailing_enabled: false      # trailing stop (v2)
    max_loss_per_trade_usd: 5.0  # absolute dollar cap per position
```

### 5.3 Execution

Stop-loss is **always full exit** and **always aggressive**:

```python
async def check_stop_loss(position, current_price):
    if position.yes_size <= 0 or position.yes_avg_price <= 0:
        return None
    
    unrealized_pct = (current_price - position.yes_avg_price) / position.yes_avg_price
    unrealized_usd = position.yes_size * (current_price - position.yes_avg_price)
    
    if unrealized_pct <= -hard_stop_pct or unrealized_usd <= -max_loss_per_trade_usd:
        return SellSignal(size=position.yes_size, urgency="critical", reason="hard_stop")
    elif unrealized_pct <= -threshold_pct:
        return SellSignal(size=position.yes_size, urgency="high", reason="stop_loss")
    
    return None
```

### 5.4 Pricing

- **High urgency**: Best bid (fill now)
- **Critical urgency**: Best bid - 2 ticks (must fill immediately, accept slippage)

---

## 6. Layer 4 — Resolution Exit

### 6.1 Rationale

Markets approaching resolution are **toxic**: the informed traders know the outcome, market makers get adversely selected. We must exit ALL inventory before resolution.

### 6.2 Configuration

```yaml
exit:
  resolution:
    enabled: true
    exit_start_hours: 6          # start unwinding 6h before end_date
    exit_complete_hours: 2       # be fully flat 2h before end_date  
    aggressive_after_hours: 1    # cross the spread if still holding < 1h out
    block_new_buys_hours: 8      # stop buying 8h before end_date
```

### 6.3 Time Zones

These are relative to each market's `end_date` field from the Gamma API.

### 6.4 Execution

```python
def get_resolution_exit_action(position, end_date):
    hours_left = (end_date - now).total_seconds() / 3600
    
    if hours_left <= 0:
        return "FORCE_EXIT"              # Market already ended, exit at any price
    elif hours_left <= aggressive_after_hours:
        return "AGGRESSIVE_EXIT"         # Cross the spread
    elif hours_left <= exit_complete_hours:
        return "URGENT_EXIT"             # Best bid
    elif hours_left <= exit_start_hours:
        # Linear ramp: sell fraction proportional to time elapsed
        fraction = 1.0 - (hours_left - exit_complete_hours) / (exit_start_hours - exit_complete_hours)
        return ("GRADUAL_EXIT", fraction) # Ramp up sell pressure
    elif hours_left <= block_new_buys_hours:
        return "NO_NEW_BUYS"             # Stop buying, but don't actively sell yet
    
    return None
```

### 6.5 Interaction with Layer 1

During the gradual exit phase, `inventory_skew_gamma` is **multiplied by (1 + fraction)**. This makes asks progressively more aggressive as resolution approaches.

---

## 7. Layer 5 — Emergency Flatten

### 7.1 Triggers

- **Manual**: Operator sends `/flatten` command (NATS message, API call, or config flag)
- **Automatic**: Daily drawdown exceeds `daily_flatten_drawdown_nav` (4%)
- **Automatic**: Kill switch activated (already exists)

### 7.2 Configuration

```yaml
exit:
  flatten:
    config_flag_path: "/tmp/pmm1_flatten"  # touch this file → flatten all
    price_tolerance_pct: 0.05              # accept up to 5% worse than mid
```

### 7.3 Execution

```python
async def emergency_flatten(order_manager, position_tracker, book_manager):
    """Cancel all open orders, then sell all positions at market."""
    await order_manager.cancel_all()
    
    for pos in position_tracker.get_active_positions():
        if pos.yes_size >= 5.0:
            book = book_manager.get(pos.token_id_yes)
            best_bid = book.best_bid if book else None
            if best_bid:
                await order_manager.submit_sell(
                    token_id=pos.token_id_yes,
                    price=best_bid,
                    size=pos.yes_size,
                    urgency="critical",
                )
        # Same for NO side
        if pos.no_size >= 5.0:
            book = book_manager.get(pos.token_id_no)
            best_bid = book.best_bid if book else None
            if best_bid:
                await order_manager.submit_sell(
                    token_id=pos.token_id_no,
                    price=best_bid,
                    size=pos.no_size,
                    urgency="critical",
                )
```

---

## 8. New Module: `pmm1/strategy/exit_manager.py`

Central coordinator for all exit logic:

```python
class SellSignal(BaseModel):
    token_id: str
    condition_id: str
    size: float
    price: float | None = None     # None = market (best bid)
    urgency: str = "low"           # low | medium | high | critical
    reason: str = ""               # take_profit | stop_loss | resolution | flatten | orphan
    
class ExitManager:
    def __init__(self, config: ExitConfig, position_tracker, book_manager):
        self.config = config
        self.positions = position_tracker
        self.books = book_manager
        self._tp_cooldowns: dict[str, float] = {}  # condition_id → last_tp_time
    
    async def evaluate_all(self, active_markets: dict) -> list[SellSignal]:
        """Run all exit checks, return prioritized sell signals."""
        signals: list[SellSignal] = []
        
        for pos in self.positions.get_active_positions():
            current_price = self._get_current_price(pos)
            if current_price is None:
                continue
            
            # Priority order: flatten > stop > resolution > take-profit
            signal = (
                self._check_flatten(pos) or
                self._check_stop_loss(pos, current_price) or
                self._check_resolution(pos, active_markets) or
                self._check_take_profit(pos, current_price)
            )
            if signal:
                signals.append(signal)
        
        return signals
    
    def _get_current_price(self, pos):
        """Best bid = conservative mark for sells."""
        book = self.books.get(pos.token_id_yes)
        return book.best_bid if book and book.best_bid else None
```

### 8.1 Integration Point (main.py)

```python
# After main quote loop, before cycle metrics:
if not paper_mode and order_manager:
    exit_signals = await exit_manager.evaluate_all(state.active_markets)
    for signal in exit_signals:
        result = await order_manager.submit_exit(signal)
        if result.get("submitted"):
            logger.info("exit_order_submitted",
                        reason=signal.reason,
                        token_id=signal.token_id[:16],
                        size=signal.size,
                        price=signal.price,
                        urgency=signal.urgency)
```

---

## 9. Orphan Position Handling

### 9.1 Current State

The unwind loop in main.py iterates `_positions` and sells anything not in `active_markets`. This works but:
- Uses REST book fallback (slow)
- No pricing intelligence
- Runs every cycle (wasteful)

### 9.2 Improvement

Move orphan handling into `ExitManager`:
- Run every 60s (not every 250ms cycle)
- Fetch REST book only for orphans
- Use the same `SellSignal` pipeline as other exits
- Log as `reason="orphan"`

---

## 10. Position State Fixes

### 10.1 Bug: `Position` vs `MarketPosition` (line 262 of positions.py)

```python
# CURRENT (broken):
self._positions[token_id] = Position(...)  # NameError — class is MarketPosition

# FIX:
self._positions[token_id] = MarketPosition(
    condition_id=token_id,
    token_id_yes=token_id,
    yes_size=exchange_size,
)
```

### 10.2 Missing: Cost Basis for Auto-Adopted Positions

When we auto-adopt from exchange, we don't know the entry price. Options:
- **Option A**: Use current mid as entry (imprecise but functional)
- **Option B**: Set entry to 0 and treat all PnL as realized on exit
- **Option C**: Query trade history from CLOB API to reconstruct

**Recommendation**: Option A for v1. Cost basis is only used for PnL display and take-profit/stop-loss thresholds. Using current mid is conservative (it understates profit, which is safe).

### 10.3 CTF Token Approval on Startup

Add a startup check to ensure CTF conditional tokens are approved for all exchange contracts. If not, auto-approve:

```python
async def ensure_ctf_approvals(w3, account, ctf_address, exchange_addresses):
    """Check and set ERC-1155 setApprovalForAll for selling."""
    ctf = w3.eth.contract(address=ctf_address, abi=ERC1155_ABI)
    for name, operator in exchange_addresses:
        if not ctf.functions.isApprovedForAll(account.address, operator).call():
            tx = ctf.functions.setApprovalForAll(operator, True).build_transaction(...)
            # sign and send
```

---

## 11. Configuration Summary

New `exit` section in `config/default.yaml`:

```yaml
exit:
  take_profit:
    enabled: true
    threshold_pct: 0.15
    partial_exit_pct: 0.50
    full_exit_pct: 0.30
    min_hold_minutes: 30
    cooldown_minutes: 10
  
  stop_loss:
    enabled: true
    threshold_pct: 0.20
    hard_stop_pct: 0.40
    max_loss_per_trade_usd: 5.0
  
  resolution:
    enabled: true
    exit_start_hours: 6
    exit_complete_hours: 2
    aggressive_after_hours: 1
    block_new_buys_hours: 8
  
  flatten:
    config_flag_path: "/tmp/pmm1_flatten"
    price_tolerance_pct: 0.05
  
  orphan:
    check_interval_s: 60
    min_size_to_unwind: 5.0

  inventory_skew:
    gamma_max: 0.05
    age_halflife_hours: 4.0
```

---

## 12. Implementation Order

1. **Fix `Position` → `MarketPosition` bug** (5 min, critical)
2. **Add CTF approval startup check** (30 min)
3. **Create `ExitConfig` settings model** (15 min)
4. **Build `ExitManager` class** (1 hour)
5. **Integrate ExitManager into main loop** (30 min)
6. **Add resolution time tracking to universe** (15 min)
7. **Add dynamic γ (inventory age skew)** (30 min)
8. **Add emergency flatten via file flag** (15 min)
9. **Move orphan handling into ExitManager** (15 min)
10. **Paper test** with recorded book data (1 hour)
11. **Deploy and monitor** (ongoing)

**Total estimated effort**: ~4–5 hours

---

## 13. Risk Considerations

- **Stop-loss can crystallize losses prematurely**: A -20% move in prediction markets isn't like equities — the market could bounce. But without stops, a position can go to zero at resolution.
- **Take-profit at +15% may leave money on the table**: Prediction markets have asymmetric payoffs (0 or 1). A YES at 50¢ that moves to 57.5¢ (+15%) may still resolve at $1. Counter-argument: we're market makers, not directional traders. Taking guaranteed edge > hoping for resolution.
- **Resolution exit assumes `end_date` accuracy**: Some markets extend or get early-resolved. The exit ramp should respect `accepting_orders` flag too.
- **Flatten mode can cause slippage**: Selling everything at once in thin markets. The `price_tolerance_pct` prevents extreme fills but doesn't eliminate impact.

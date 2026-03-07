-- PMM-2 Sprint 1 Database Schema

-- Fill records with markout tracking
CREATE TABLE IF NOT EXISTS fill_record (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                    -- ISO8601 timestamp
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    side TEXT NOT NULL,                  -- BUY or SELL
    price REAL NOT NULL,
    size REAL NOT NULL,
    dollar_value REAL NOT NULL,
    fee REAL DEFAULT 0.0,
    -- Markout fields (filled asynchronously after fill)
    markout_1s REAL,                     -- price change at +1s
    markout_5s REAL,                     -- price change at +5s
    markout_30s REAL,                    -- price change at +30s
    mid_at_fill REAL,                    -- book midpoint at fill time
    is_scoring INTEGER DEFAULT 0,        -- was order scoring when filled?
    reward_eligible INTEGER DEFAULT 0    -- is market reward-eligible?
);

-- Book snapshots for queue estimation replay
CREATE TABLE IF NOT EXISTS book_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    best_bid REAL,
    best_ask REAL,
    bid_depth_5 REAL,                    -- total bid size within 5 levels
    ask_depth_5 REAL,                    -- total ask size within 5 levels
    spread_cents REAL,
    mid REAL
);

-- Market scoring history
CREATE TABLE IF NOT EXISTS market_score (
    ts TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    bundle TEXT NOT NULL DEFAULT 'B1',
    spread_ev_bps REAL,
    arb_ev_bps REAL,
    liq_ev_bps REAL,
    rebate_ev_bps REAL,
    tox_cost_bps REAL,
    res_cost_bps REAL,
    carry_cost_bps REAL,
    marginal_return_bps REAL,
    target_capital_usdc REAL,
    allocator_rank INTEGER,
    PRIMARY KEY (ts, condition_id, bundle)
);

-- Queue state snapshots
CREATE TABLE IF NOT EXISTS queue_state (
    order_id TEXT PRIMARY KEY,
    token_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    side TEXT,
    price REAL,
    size_open REAL,
    est_ahead_low REAL,
    est_ahead_mid REAL,
    est_ahead_high REAL,
    eta_sec REAL,
    fill_prob_30s REAL,
    queue_uncertainty REAL,
    is_scoring INTEGER DEFAULT 0,
    entry_time REAL,
    last_update REAL
);

-- Allocation decisions
CREATE TABLE IF NOT EXISTS allocation_decision (
    ts TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    current_capital_usdc REAL,
    target_capital_usdc REAL,
    delta_capital_usdc REAL,
    reason TEXT,
    confidence REAL,
    PRIMARY KEY (ts, condition_id)
);

-- Reward actuals (daily)
CREATE TABLE IF NOT EXISTS reward_actual (
    date TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    realized_liq_reward_usdc REAL,
    est_liq_reward_usdc REAL,
    capture_efficiency REAL,
    PRIMARY KEY (date, condition_id)
);

-- Rebate actuals (daily)
CREATE TABLE IF NOT EXISTS rebate_actual (
    date TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    realized_rebate_usdc REAL,
    est_rebate_usdc REAL,
    capture_efficiency REAL,
    PRIMARY KEY (date, condition_id)
);

-- Scoring history per order
CREATE TABLE IF NOT EXISTS scoring_history (
    ts TEXT NOT NULL,
    order_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    is_scoring INTEGER NOT NULL,
    PRIMARY KEY (ts, order_id)
);

-- Create indices for common queries
CREATE INDEX IF NOT EXISTS idx_fill_record_ts ON fill_record(ts);
CREATE INDEX IF NOT EXISTS idx_fill_record_condition ON fill_record(condition_id);
CREATE INDEX IF NOT EXISTS idx_book_snapshot_ts ON book_snapshot(ts);
CREATE INDEX IF NOT EXISTS idx_book_snapshot_condition ON book_snapshot(condition_id);
CREATE INDEX IF NOT EXISTS idx_scoring_history_order ON scoring_history(order_id);

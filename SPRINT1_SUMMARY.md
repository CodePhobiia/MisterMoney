# PMM-2 Sprint 1 — Storage + Data Collection Layer

**Status:** ✅ Complete

**Completed:** 2026-03-07

## What Was Built

### S1-1: SQLite Database Module
- **File:** `pmm1/storage/database.py`
- **Schema:** `pmm1/storage/schema.sql`
- **Features:**
  - Async SQLite using `aiosqlite`
  - WAL mode for concurrent access
  - Auto-creates tables from schema on init
  - Methods: `init()`, `close()`, `execute()`, `fetch_all()`, `fetch_one()`, `execute_many()`
  - Database path: `data/pmm1.db`

### S1-2: Fill Recorder with Markout Tracking
- **File:** `pmm1/recorder/fill_recorder.py`
- **Features:**
  - Records fills with condition_id, token_id, order_id, side, price, size, fee
  - Captures book midpoint at fill time
  - Tracks scoring status and reward eligibility
  - Schedules async markout calculations at +1s, +5s, +30s
  - Markouts are signed by side (BUY: positive = price went up, SELL: positive = price went down)
- **Integration:** Wired into `on_fill` callback in `main.py` (line ~548)

### S1-3: Book Snapshot Recorder
- **File:** `pmm1/recorder/book_recorder.py`
- **Features:**
  - Records top-of-book snapshots every 10 seconds
  - Captures: best_bid, best_ask, bid_depth_5, ask_depth_5, spread_cents, mid
  - Skips tokens with no book data
- **Integration:** Background loop in `main.py` (line ~710)

### S1-4: Scoring History Persister
- **Integration:** Extended scoring_check_loop in `main.py` (line ~680)
- **Features:**
  - Persists scoring status for all live orders every 30 seconds
  - Records: ts, order_id, condition_id, is_scoring
  - Uses `INSERT OR REPLACE` for idempotency

### S1-5: Data Export CLI
- **File:** `pmm1/tools/export_data.py`
- **Usage:**
  ```bash
  python -m pmm1.tools.export_data --table fill_record --since 24h --format csv
  python -m pmm1.tools.export_data --table book_snapshot --since 7d --format csv
  ```
- **Features:**
  - Export any table to CSV
  - Time filtering: `--since 1h`, `--since 24h`, `--since 7d`
  - Outputs to stdout (pipeable)

## Database Schema

### Tables Created

1. **fill_record** — Fill data with markout tracking
   - Primary fields: ts, condition_id, token_id, order_id, side, price, size
   - Markout fields: markout_1s, markout_5s, markout_30s, mid_at_fill
   - Flags: is_scoring, reward_eligible

2. **book_snapshot** — Top-of-book snapshots
   - Fields: ts, condition_id, token_id, best_bid, best_ask, bid_depth_5, ask_depth_5, spread_cents, mid

3. **scoring_history** — Per-order scoring status
   - Fields: ts, order_id, condition_id, is_scoring

4. **market_score** — Market scoring history (placeholder for Sprint 2)
5. **queue_state** — Queue position snapshots (placeholder for Sprint 2)
6. **allocation_decision** — Capital allocation decisions (placeholder for Sprint 2)
7. **reward_actual** — Daily reward capture (placeholder for Sprint 2)
8. **rebate_actual** — Daily rebate capture (placeholder for Sprint 2)

### Indices
- `idx_fill_record_ts` — Fill records by timestamp
- `idx_fill_record_condition` — Fill records by condition_id
- `idx_book_snapshot_ts` — Book snapshots by timestamp
- `idx_book_snapshot_condition` — Book snapshots by condition_id
- `idx_scoring_history_order` — Scoring history by order_id

## Integration Points

### In `pmm1/main.py`:

1. **Imports** (line ~76):
   ```python
   from pmm1.storage.database import Database
   from pmm1.recorder.fill_recorder import FillRecorder
   from pmm1.recorder.book_recorder import BookRecorder
   ```

2. **Initialization** (line ~455):
   ```python
   db = Database("data/pmm1.db")
   await db.init()
   fill_recorder = FillRecorder(db, state.book_manager)
   book_recorder = BookRecorder(db)
   ```

3. **Fill Recording** (line ~548, in `on_fill` callback):
   - Records fill immediately after position tracker update
   - Captures current book midpoint
   - Schedules markout tracking tasks

4. **Book Snapshot Loop** (line ~710):
   - Runs every 10 seconds
   - Only snapshots when in QUOTING or PAUSED mode

5. **Scoring History** (line ~680, in `scoring_check_loop`):
   - Persists scoring status after each check
   - Batch insert for efficiency

## Testing

All components tested and verified:
- ✅ Database init and schema creation
- ✅ Fill recording with markout scheduling
- ✅ Book snapshot recording
- ✅ Scoring history persistence
- ✅ Data export CLI
- ✅ Bot import check passes
- ✅ Integration test successful

## Dependencies Added

- `aiosqlite>=0.19.0` (added to `pyproject.toml`)

## Files Modified

- `.gitignore` — Added `data/*.db`, `data/*.db-shm`, `data/*.db-wal`
- `pmm1/main.py` — Added data collection wiring
- `pyproject.toml` — Added aiosqlite dependency

## Files Created

- `data/.gitkeep`
- `pmm1/storage/database.py`
- `pmm1/storage/schema.sql`
- `pmm1/recorder/__init__.py`
- `pmm1/recorder/fill_recorder.py`
- `pmm1/recorder/book_recorder.py`
- `pmm1/tools/__init__.py`
- `pmm1/tools/export_data.py`

## Non-Breaking Changes

- All new code runs in background tasks or async callbacks
- No changes to core trading logic
- Database operations are fire-and-forget (errors logged but don't crash bot)
- Can be disabled by commenting out initialization if needed

## Next Steps (Sprint 2)

- Queue position estimation
- Capital allocation logic
- Reward capture tracking
- Market scoring refinement

## Usage

### Starting the bot
Bot will auto-create `data/pmm1.db` on first run. No configuration needed.

### Exporting data
```bash
# Export recent fills
python -m pmm1.tools.export_data --table fill_record --since 24h > fills.csv

# Export all book snapshots from last 7 days
python -m pmm1.tools.export_data --table book_snapshot --since 7d > books.csv

# Export scoring history
python -m pmm1.tools.export_data --table scoring_history --since 24h > scoring.csv
```

### Querying the database directly
```bash
sqlite3 data/pmm1.db "SELECT * FROM fill_record ORDER BY ts DESC LIMIT 10"
```

---

**Built by:** Subagent (Butters)  
**Commit:** `b5320c7`  
**Branch:** `main`

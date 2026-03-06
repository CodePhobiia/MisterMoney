# PMM-1 — Polymarket Production Market-Making Bot

> "The right v1 is not a giant 'AI prediction bot.' It is a disciplined execution system centered on structural arb and reward-aware passive quoting, with directional views as a later overlay."

## Architecture

PMM-1 is a **modular monolith** built on Python 3.12 + asyncio. It connects to Polymarket's CLOB (Central Limit Order Book) to provide liquidity and capture structural alpha.

### Strategy Stack

1. **Binary Parity Arbitrage** — Pure structural arb when YES + NO prices diverge from $1.00
2. **Negative-Risk Conversion Arbitrage** — Exploits conversion relationships in multi-outcome events
3. **Reward-Aware Passive Market Making** — Two-sided quoting optimized for spread capture + liquidity rewards + maker rebates
4. **Directional Overlay** — (Disabled in v1) Only crosses spread when edge survives all costs

### Key Features

- **Logit-space fair value model** with microprice, imbalance, trade flow, and volatility features
- **Queue-aware fill probability model** for optimal quote placement
- **Inventory-skewed reservation pricing** with cluster correlation awareness
- **3-tier drawdown governor** (pause taker → widen quotes → flatten only)
- **Kill switches** for stale feeds, heartbeat failures, position breaches, and exchange pauses
- **Live data recorder** for building proprietary backtest datasets
- **PnL decomposition** into spread capture, adverse selection, arb, rewards, and slippage

## Quick Start

### Prerequisites

- Python 3.12+
- Redis (for hot state)
- PostgreSQL (for durable storage)
- Polymarket API credentials

### Installation

```bash
# Clone and install
cd pmm1
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env with your API credentials and wallet
```

### Configuration

Primary config: `config/default.yaml`
Override for production: `config/prod.yaml`

Environment variables override YAML. Prefix: `PMM1_`, nested with `__`.

```bash
# Example overrides
PMM1_BOT__MAX_MARKETS=10
PMM1_RISK__PER_MARKET_GROSS_NAV=0.01
```

### Running

```bash
# Start the bot
pmm1

# Or directly
python -m pmm1.main
```

## Project Structure

```
pmm1/
├── config/           # YAML configuration
├── pmm1/
│   ├── main.py       # Main loop (§20)
│   ├── settings.py   # Pydantic settings
│   ├── logging.py    # structlog setup
│   ├── api/          # HTTP clients (Gamma, CLOB, Data API)
│   ├── ws/           # WebSocket clients (market, user)
│   ├── state/        # State management (books, orders, positions)
│   ├── strategy/     # Strategy modules (universe, pricing, quoting, arb)
│   ├── risk/         # Risk engine (limits, kill switch, drawdown)
│   ├── execution/    # Order management (diff, batch, reconcile)
│   ├── storage/      # Persistence (Redis, PostgreSQL, Parquet)
│   ├── analytics/    # PnL, metrics, attribution
│   └── backtest/     # Recorder, replay, simulator
└── tests/
```

## Risk Controls

| Limit                    | Default | Description |
|--------------------------|---------|-------------|
| Per-market gross         | 2% NAV  | Max exposure in any single market |
| Per-event cluster        | 5% NAV  | Max exposure across correlated markets |
| Total directional net    | 10% NAV | Total net directional exposure |
| Total arb gross          | 25% NAV | Total arb position size |
| Daily DD → Pause taker   | 1.5% NAV | Stop crossing spreads |
| Daily DD → Widen+cut     | 2.5% NAV | 50% wider quotes, 50% size |
| Daily DD → Flatten only  | 4.0% NAV | Cancel all, stop quoting |

## Acceptance Criteria (§16)

Before going live:
- [ ] 30 consecutive paper days with positive net expectancy
- [ ] Quote uptime > 99%
- [ ] Order reject rate < 0.5%
- [ ] Zero heartbeat mass-cancels from our bug
- [ ] Positive realized spread (with and without rewards)
- [ ] 5-second adverse selection < 60% of spread capture

## Build Order

1. ✅ Recorder + backtester + paper trader
2. ✅ Live execution layer
3. 🔄 Paper trading validation
4. ⬜ Live deployment

## Key Dependencies

- `py-clob-client` — Official Polymarket CLOB SDK
- `aiohttp` + `websockets` — Async HTTP and WebSocket
- `pydantic` — Data models and settings
- `structlog` — Structured JSON logging
- `polars` + `duckdb` — Research and analytics
- `redis` + `asyncpg` — Hot state and durable storage

## Important Notes

- **Matching engine restarts:** Weekly Tuesday 7:00 AM ET, ~90s (HTTP 425)
- **Heartbeat:** Must send every 5s or server cancels all orders
- **Tick size:** Dynamic near extremes — always check `tick_size_change` events
- **Batch limit:** Max 15 orders per POST /orders request
- **GTD expiration:** Set to `now + 60 + N` for effective lifetime of N seconds

---

*Built with 🧈 by PMM-1 Engineering*

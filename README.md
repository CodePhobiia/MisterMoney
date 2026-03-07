# MisterMoney 💰

Autonomous market-making system for [Polymarket](https://polymarket.com). Three-layer architecture: execution engine (V1), capital allocation intelligence (V2), and multi-model resolution prediction (V3).

> Built by a bot, for a bot. ~30,000 lines of Python. Fully autonomous 24/7 operation.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  V3 — Resolution Intelligence (14,800 lines)             │
│  Multi-model AI pipeline: evidence → routes → calibrate  │
│  Sonnet · Opus · GPT-5.4 · Gemini 3 Pro                 │
├──────────────────────────────────────────────────────────┤
│  V2 — Capital Allocation (4,600 lines)                   │
│  Universe scoring · EV bundles · Greedy allocator         │
│  Queue estimation · Persistence optimization              │
├──────────────────────────────────────────────────────────┤
│  V1 — Execution Engine (10,400 lines)                    │
│  CLOB connectivity · Order management · Risk limits       │
│  Inventory-skewed pricing · Fill detection · Exit logic   │
└──────────────────────────────────────────────────────────┘
```

## V1 — Execution Engine (`pmm1/`)

The core trading bot. Connects to Polymarket's CLOB via REST + WebSocket, provides two-sided liquidity, and manages positions.

### Strategy Stack

1. **Binary Parity Arbitrage** — Structural arb when YES + NO prices diverge from $1.00
2. **Negative-Risk Conversion Arbitrage** — Exploits conversion relationships in multi-outcome events
3. **Reward-Aware Passive Market Making** — Two-sided quoting optimized for spread capture + liquidity rewards
4. **Inventory-Skewed Reservation Pricing** — Logit-space fair value with microprice and trade flow features

### Key Features

- Dollar-normalized position sizing (`target_dollar_size` per market)
- 5-layer exit system: inventory-aware quoting → take-profit → stop-loss → resolution exit → emergency flatten
- Real-time fill detection via WebSocket with Telegram notifications
- REST book fallback with TTL cache when WebSocket data is stale
- Top-of-book clamp with crossing guard (post-only compliance)
- Directional risk caps, per-market limits, per-event cluster limits
- Kill switch via `/tmp/pmm1_flatten`

### Quick Start

```bash
pip install -e "."

# Configure
cp .env.example .env
# Add: PRIVATE_KEY, POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE
# Add: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional, for notifications)

# Run
python -m pmm1.main
```

### Configuration

Primary config: `config/default.yaml`

Key settings:
```yaml
bot:
  max_markets: 12
  target_dollar_size: 8.0       # USD per market
  base_half_spread_cents: 0.5   # 0.5¢ half-spread
  quote_cycle_ms: 1000

universe:
  min_volume_24h_usd: 50000
  max_top_spread_cents: 5
  midpoint_min: 0.15
  midpoint_max: 0.85
  allow_sports: true

risk:
  per_market_gross_nav: 0.08
  per_event_cluster_nav: 0.15
  total_directional_nav: 0.60
```

## V2 — Capital Allocation (`pmm2/`)

Intelligence layer that decides *where* to deploy capital. Runs in shadow mode alongside V1, comparing its allocation decisions against V1's actual performance.

### Components

| Module | Purpose |
|--------|---------|
| `universe/` | Enriched market metadata, reward surface, fee surface |
| `scorer/` | Combined EV scorer: spread + arb + liquidity + rebate − toxicity − resolution − carry |
| `queue/` | Queue position estimation and fill hazard modeling |
| `persistence/` | Persistence optimizer — should we hold or cancel? |
| `allocator/` | Discrete greedy capital allocator with constraint checking |
| `planner/` | Quote plan generation from funded bundles |
| `calibration/` | Fill calibrator, toxicity fitter, reward/rebate tracking, attribution |
| `shadow/` | Shadow mode: counterfactual engine, logger, dashboard |
| `runtime/` | Integration hooks into V1 main loop |

### Bundle System

Markets are scored as nested **bundles**:
- **B1** (Reward Core): Minimum viable two-sided quote at inside spread
- **B2** (Reward Depth): Additional depth within reward spread band
- **B3** (Edge Extension): Wider quotes only if spread EV > 0 before rewards

### Shadow Mode

```yaml
pmm2:
  enabled: true
  shadow_mode: true   # Log decisions, don't execute
```

Shadow mode captures V1 state snapshots, runs the full scoring/allocation pipeline, generates counterfactual comparisons, and reports via Telegram. Requires 10+ days of positive counterfactual EV before graduating to live.

## V3 — Resolution Intelligence (`v3/`)

Multi-model AI pipeline that predicts market outcomes. Routes markets to specialized analysis chains based on market type.

### Route Architecture

```
Market → Router → ┬─ Numeric Route    (price targets, dates)
                   ├─ Simple Route     (binary outcomes, news-driven)
                   ├─ Rule-Heavy Route (legal, regulatory, complex rules)
                   └─ Dossier Route    (multi-factor, long-context synthesis)
```

### Model Assignments

| Role | Model | Use |
|------|-------|-----|
| Triage / Orchestrator | GPT-5.4 | Hot-path routing, judge pass |
| Rule Lawyer | Opus 4.6 | Legal analysis, adversarial review, dispute risk |
| Long-Context Synthesis | Gemini 3 Pro | Dossier assembly, evidence synthesis |
| Blind Analysis | Sonnet 4.6 | First-pass reasoning without market anchoring |
| Async Escalation | GPT-5.4-pro | Offline adjudication (rate-limited) |

### Evidence Pipeline

```
Sources → Evidence Collector → Normalizer → EvidenceGraph → Routes
           ├─ Resolution source URLs
           ├─ Web search (DuckDuckGo)
           ├─ Source checkers (CoinGecko, sports APIs, economic data)
           └─ Publisher reliability scoring
```

### Calibration

- Route-specific calibrators with conformal prediction intervals
- Signal decay modeling (numeric signals stale faster than rule-heavy)
- Learnable β weights trained on resolved markets in logit-space
- Brier score tracking per route

### Shadow → Canary → Production

```
Shadow Mode (current)
  └─ Canary 1¢ skew
       └─ Canary 2¢
            └─ Canary 5¢
                 └─ Full Production
```

Kill switch: set `v3_enabled: false` in config.

### Infrastructure

- PostgreSQL 16 + pgvector for evidence storage and semantic search
- Redis for calibration signals and escalation queue
- OAuth consumer endpoints ($0 marginal token cost)

## Project Structure

```
MisterMoney/
├── config/
│   └── default.yaml          # Primary configuration
├── pmm1/                     # V1 — Execution Engine
│   ├── main.py               # Main event loop
│   ├── settings.py           # Pydantic settings
│   ├── api/                  # CLOB REST + Gamma API clients
│   ├── ws/                   # WebSocket clients (market data, user events)
│   ├── risk/                 # Risk limits and position sizing
│   ├── state/                # Position tracking, order management
│   ├── strategy/             # Pricing, fair value, arb detection
│   ├── storage/              # SQLite persistence, parquet recording
│   ├── exit/                 # Exit manager (take-profit, stop-loss, resolution)
│   ├── paper/                # Paper trading engine
│   └── notifications.py      # Telegram alerts
├── pmm2/                     # V2 — Capital Allocation
│   ├── universe/             # Market enrichment, rewards, fees
│   ├── scorer/               # EV scoring (spread, arb, toxicity, etc.)
│   ├── queue/                # Queue estimation and fill hazard
│   ├── persistence/          # Hold vs cancel optimization
│   ├── allocator/            # Greedy capital allocation
│   ├── planner/              # Quote plan generation
│   ├── calibration/          # Fill calibration, attribution
│   ├── shadow/               # Shadow mode engine
│   ├── runtime/              # V1 integration hooks
│   └── config.py             # PMM-2 configuration
├── v3/                       # V3 — Resolution Intelligence
│   ├── providers/            # LLM adapters (Anthropic, OpenAI, Google)
│   ├── evidence/             # Evidence DB, graph, normalizer
│   ├── intake/               # Gamma sync, source checkers, evidence collector
│   ├── routes/               # Numeric, simple, rule-heavy, dossier
│   ├── routing/              # Market-type router
│   ├── calibration/          # Route-specific calibrators
│   ├── shadow/               # Shadow mode runner, Brier tracking
│   ├── canary/               # Staged rollout (1¢ → 2¢ → 5¢ → prod)
│   └── offline/              # Async escalation worker
├── tools/
│   └── dashboard.py          # Real-time CLI dashboard
├── docs/
│   ├── SELL-LOGIC-SPEC.md    # Exit system specification
│   ├── PMM-2-SPEC.md         # V2 specification
│   ├── PMM-2-WBS.md          # V2 work breakdown
│   ├── V3-SPEC.md            # V3 specification
│   └── V3-WBS.md             # V3 work breakdown
└── .env                      # Secrets (gitignored)
```

## Deployment

Runs as systemd user services on Linux:

```bash
# V1 execution bot
systemctl --user start pmm1

# V3 shadow mode
systemctl --user start v3-shadow
```

## Risk Disclaimers

- This is experimental software. Use at your own risk.
- Prediction markets involve real money. You can lose your entire deposit.
- No guarantee of profits. Past performance does not predict future results.
- The authors are not responsible for any financial losses.

## License

Private. All rights reserved.

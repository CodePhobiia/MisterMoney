# MisterMoney Operational Runbook

**Last Updated**: 2026-03-08
**Audience**: Bot operators (currently: Theyab)

---

## 1. Emergency Stop

### Immediate shutdown
```bash
# Stop the systemd service
systemctl --user stop pmm1

# Or if running directly:
kill $(pgrep -f "python.*pmm1")
```

### Manual flatten (if service is still running)
```bash
# Create the flatten flag — bot will attempt to exit all positions
touch /tmp/pmm1_flatten

# Monitor progress in logs
journalctl --user -u pmm1 -f | grep -i flatten
```

### After emergency stop:
1. **Do NOT restart immediately.** Investigate first.
2. Check on-chain positions: `python -m pmm1.tools.export_data --positions`
3. Check open orders on CLOB: verify via Polymarket API or UI
4. If orders are still open, cancel manually via API

---

## 2. Restart Checklist

Before restarting the bot, verify:

- [ ] **USDC balance** — Enough liquid USDC for quoting (`>$20` minimum)
- [ ] **POL/MATIC for gas** — `>0.5 POL` in wallet for on-chain operations
- [ ] **CLOB API credentials** — API key, secret, passphrase are valid (test with a GET /auth endpoint)
- [ ] **CTF approvals** — ERC-1155 approvals are set for the CLOB exchange contract
- [ ] **No lingering flatten flag** — `rm -f /tmp/pmm1_flatten`
- [ ] **Config is correct** — Review `config/default.yaml` and `config/prod.yaml`
- [ ] **Check git status** — `git status` to ensure no uncommitted changes that could cause issues

### Start
```bash
systemctl --user start pmm1
# Or directly:
cd /home/ubuntu/MisterMoney && python -m pmm1.main
```

### Verify healthy startup:
```bash
# Watch logs for startup sequence
journalctl --user -u pmm1 -f

# Look for:
# - "bot_started" log entry
# - "market_ws_connected"
# - "user_ws_connected"
# - "universe_built" with N markets
# - "quoting_started"
```

---

## 3. Exchange Restart (Every Tuesday)

Polymarket CLOB restarts every Tuesday around ~14:00 UTC.

### What happens automatically:
1. WebSocket disconnects → kill switch fires (`STALE_MARKET_FEED`)
2. Auto-clear triggers after **120 seconds** (T0-08 fix)
3. WebSocket reconnects and resubscribes
4. Full reconciliation runs after reconnect
5. Normal quoting resumes

### What to watch:
```bash
# During the restart window (~14:00-14:05 UTC Tuesday)
journalctl --user -u pmm1 -f | grep -E "kill_switch|reconnect|reconcil"
```

### If auto-recovery fails (>5 minutes):
1. Check if WebSocket reconnected: look for `market_ws_connected` in logs
2. If stuck, restart the bot manually (see §2)
3. After restart, verify all positions are reconciled

---

## 4. Drawdown Investigation

### Check current state:
```bash
journalctl --user -u pmm1 --since "1 hour ago" | grep -i drawdown
```

### Drawdown tiers:
| Tier | Trigger | Action |
|------|---------|--------|
| Normal | DD < 1.5% | Normal operation |
| Tier 1 | DD > 1.5% | Taker trades paused |
| Tier 2 | DD > 2.5% | Quotes 50% wider, sizes halved |
| Tier 3 | DD > 4.0% | FLATTEN_ONLY — only exits |

### Investigation steps:
1. **What's the current NAV?** Check logs for `nav_updated` entries
2. **What's the high-water mark?** Check `daily_high_watermark` in drawdown logs
3. **Which market(s) caused the loss?** Check recent fills for large adverse moves
4. **Is it a single event or broad?** Check if multiple correlated markets moved
5. **Is the drawdown real or a NAV calculation error?** Compare on-chain balance with reported NAV

### Recovery:
- Tier 1/2: Will auto-recover when NAV rises back above threshold
- Tier 3: Will auto-recover, but consider manual review first
- Daily reset at UTC midnight clears all tiers

---

## 5. Gas Refill

The bot needs POL (formerly MATIC) for on-chain operations (approvals, conversions).

### Check balance:
```bash
# Check POL balance for wallet
cast balance $WALLET_ADDRESS --rpc-url https://polygon-rpc.com
```

### When to refill:
- **Warning**: < 0.5 POL
- **Critical**: < 0.1 POL (some operations may fail)

### How to refill:
1. Send POL from hot wallet or exchange to the bot's wallet address
2. Typical usage: ~0.01 POL/day (very low)
3. 1 POL should last weeks under normal operation

---

## 6. Position Reconciliation Mismatch

### What it means:
The bot's local state disagrees with the exchange's view of open orders or positions.

### Symptoms:
- Log entries: `order_reconciliation_mismatches`
- If persistent (3+ consecutive): kill switch fires (`RECONCILIATION_MISMATCH`)

### Investigation:
```bash
# Check mismatch details
journalctl --user -u pmm1 --since "30 min ago" | grep -i "reconcil\|mismatch"
```

### Common causes:
1. **Network issues** — WS messages lost during disconnect
2. **Race conditions** — Order filled between snapshot and reconciliation
3. **Exchange bugs** — Rare but possible

### Resolution:
1. If kill switch fired: it will auto-clear when reconciliation succeeds
2. If persistent: restart the bot (it will reconcile on startup)
3. If positions are wrong: use `python -m pmm1.tools.export_data --positions` to compare with on-chain

---

## 7. CLOB API Credential Refresh

### When needed:
- API key expired or revoked
- Auth failure kill switch triggered repeatedly

### Steps:
```bash
# 1. Generate new API credentials using py-clob-client
python -c "
from py_clob_client.client import ClobClient
client = ClobClient(
    host='https://clob.polymarket.com',
    chain_id=137,
    key='YOUR_PRIVATE_KEY',
)
creds = client.create_or_derive_api_creds()
print(f'API Key: {creds.api_key}')
print(f'Secret: {creds.api_secret}')
print(f'Passphrase: {creds.api_passphrase}')
"

# 2. Update .env file
nano .env
# Set POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE

# 3. Restart bot
systemctl --user restart pmm1
```

---

## 8. Log Analysis Quick Reference

```bash
# Recent errors
journalctl --user -u pmm1 --since "1 hour ago" -p err

# Kill switch events
journalctl --user -u pmm1 | grep kill_switch

# Fill history
journalctl --user -u pmm1 | grep fill_confirmed

# NAV tracking
journalctl --user -u pmm1 | grep nav_updated

# Drawdown tier changes
journalctl --user -u pmm1 | grep drawdown_tier_changed

# Reconciliation
journalctl --user -u pmm1 | grep reconciliation
```

---

## 9. Key File Locations

| File | Purpose |
|------|---------|
| `config/default.yaml` | Base configuration |
| `config/prod.yaml` | Production overrides |
| `.env` | Secrets (API keys, wallet key) |
| `data/pmm1.db` | SQLite database (fills, books, queue state) |
| `data/parquet/` | Historical data (Parquet format) |
| `/tmp/pmm1_flatten` | Flatten flag (touch to trigger) |

---

## 10. Emergency Contacts & Resources

- **Polymarket Discord**: Server status announcements
- **Polymarket Status**: https://status.polymarket.com
- **CLOB API Docs**: https://docs.polymarket.com
- **Polygon RPC**: https://polygon-rpc.com (check chain health)

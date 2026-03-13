# PMM2 5% Canary Checklist

_Last updated: 2026-03-12 UTC_

This document describes the exact steps to enable a **5% PMM2 live canary**.

## Safety principle

PMM2 live control is **off by default**.

A 5% canary requires **all** of the following:

- `pmm2.enabled: true`
- `pmm2.shadow_mode: false`
- `pmm2.live_enabled: true`
- `pmm2.live_capital_pct: 0.05`
- `pmm2.canary.enabled: true`
- environment variable: `PMM1_ACK_PMM2_LIVE=YES`

If any of these are missing or inconsistent, config validation should fail.

---

## 1. Pre-flight requirements

Before enabling the canary:

- [ ] working tree is clean (`git status` should not show accidental local drift)
- [ ] full test suite passes on the intended release checkout
- [ ] PMM1 is already stable in live mode without PMM2 live control
- [ ] PMM2 shadow has been running and generating diagnostics
- [ ] operator understands rollback path
- [ ] Telegram / notification path is working

Recommended additional check:

```bash
cd /home/ubuntu/.openclaw/workspace/MisterMoney
pytest -q
```

---

## 2. Exact config for 5% canary

In the active config, set the PMM2 block to the equivalent of:

```yaml
pmm2:
  enabled: true
  shadow_mode: false
  live_enabled: true
  live_capital_pct: 0.05
  canary:
    enabled: true
    max_markets: 4
    require_reward_eligible: true
    exclude_neg_risk: true
    require_clean_outcomes: true
    max_ambiguity_score: 0.15
    min_volume_24h: 50000.0
    min_liquidity: 2500.0
    max_spread_cents: 5.0
    min_hours_to_resolution: 24.0
```

Notes:
- `0.05` is an allowed rollout stage.
- Partial rollout stages require `canary.enabled: true`.
- Full live mode (`1.0`) requires `canary.enabled: false`.

---

## 3. Required environment acknowledgement

Before restart, export:

```bash
export PMM1_ACK_PMM2_LIVE=YES
```

Without this, PMM2 live mode should refuse to initialize.

If using a systemd user service, ensure the environment is present in the service context before restart.

---

## 4. Restart sequence

Recommended:

```bash
cd /home/ubuntu/.openclaw/workspace/MisterMoney
systemctl --user restart pmm1
systemctl --user is-active pmm1
```

Expected result:
- service returns `active`
- PMM2 startup logs indicate live/canary stage instead of shadow-only

Helpful log check:

```bash
journalctl --user -u pmm1 --no-pager --since '10 min ago' \
  | grep -E 'pmm2_config_loaded|pmm2_initialized|pmm2_runtime_initialized|controller|stage|live_pct|ready_for_live|shadow_cycle_logged'
```

What you want to see:
- `enabled: true`
- `shadow_mode: false`
- `live_enabled: true`
- `live_pct: 0.05`
- stage/controller labels consistent with canary mode

---

## 5. Immediate post-enable checks

After the restart:

- [ ] PMM1 still active
- [ ] no startup config validation error
- [ ] PMM2 bridge initialized in live-capable mode
- [ ] no reconciliation storm
- [ ] no drawdown flatten-only trigger
- [ ] no excessive churn burst
- [ ] PMM2 cycle diagnostics still logging

Suggested checks:

```bash
journalctl --user -u pmm1 --no-pager --since '15 min ago' \
  | grep -E 'reconciliation|drawdown|kill_switch|shadow_cycle_logged|pmm2_allocator_cycle_complete|quote_cycle_summary'
```

---

## 6. Canary observation criteria

The canary should run under close observation before promotion.

Watch for:

- reconciliation mismatches
- order/cancel storms
- abnormal drawdown tier changes
- PMM2-controlled markets violating canary restrictions
- negative change in reward capture vs shadow expectation
- degraded PMM1 quote quality / uptime

Recommended minimum observation window:
- **24h**, preferably **24–48h**

---

## 7. Promotion rules

Only promote if the 5% canary is clean.

Suggested progression:
- `0.05` → `0.10` → `0.25` → `1.0`

At each step:
- [ ] config updated intentionally
- [ ] tests / sanity checks pass
- [ ] previous stage observation window passed cleanly
- [ ] no unresolved operational alerts

---

## 8. Rollback instructions

If anything looks wrong, revert to safe shadow mode immediately.

Set:

```yaml
pmm2:
  enabled: true
  shadow_mode: true
  live_enabled: false
  live_capital_pct: 0.0
  canary:
    enabled: false
```

Then restart:

```bash
systemctl --user restart pmm1
```

---

## 9. Do not do these

- Do **not** set `live_capital_pct > 0` while leaving `shadow_mode: true`
- Do **not** set `live_enabled: true` without the environment acknowledgement
- Do **not** use ad-hoc percentages like `0.07`
- Do **not** enable full live mode (`1.0`) while leaving `canary.enabled: true`
- Do **not** launch the canary from a dirty, unreviewed working tree

---

## 10. Final operator statement

A 5% PMM2 canary should be treated as a **controlled production experiment**, not a full promotion.

The framework is present in code, but confidence comes from:
- clean config
- explicit acknowledgement
- restart verification
- observation window
- disciplined promotion / rollback

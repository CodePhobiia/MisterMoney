#!/usr/bin/env python3
"""MisterMoney Real-Time Dashboard — run anytime for full bot snapshot."""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime

# Add parent to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp
from web3 import Web3

# ── Config ──
BOT_ADDRESS = os.getenv("BOT_ADDRESS", "0x6eDA534fFcF2Cfa5991A328a7A58CE02daFE24A6")
USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_CONTRACT = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
RPC_URL = "https://polygon-bor-rpc.publicnode.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

# ERC20 ABI (balanceOf only)
ERC20_ABI = json.loads(
    '[{"constant":true,"inputs":[{"name":"_owner",'
    '"type":"address"}],"name":"balanceOf","outputs":'
    '[{"name":"balance","type":"uint256"}],"type":"function"}]'
)


async def get_wallet_balances() -> dict:
    """Get on-chain wallet balances."""
    w3 = Web3(Web3.HTTPProvider(RPC_URL))

    # POL (native)
    pol_wei = w3.eth.get_balance(Web3.to_checksum_address(BOT_ADDRESS))
    pol = float(w3.from_wei(pol_wei, "ether"))

    # USDC.e (6 decimals)
    usdc_e = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_E_CONTRACT), abi=ERC20_ABI
    )
    usdc_e_raw = usdc_e.functions.balanceOf(Web3.to_checksum_address(BOT_ADDRESS)).call()
    usdc_e_bal = usdc_e_raw / 1e6

    # Native USDC (6 decimals)
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_CONTRACT), abi=ERC20_ABI
    )
    usdc_raw = usdc.functions.balanceOf(Web3.to_checksum_address(BOT_ADDRESS)).call()
    usdc_bal = usdc_raw / 1e6

    return {"pol": pol, "usdc_e": usdc_e_bal, "usdc": usdc_bal, "total_usd": usdc_e_bal + usdc_bal}


async def get_positions() -> list[dict]:
    """Get open positions from Polymarket data API."""
    async with aiohttp.ClientSession() as s:
        # CLOB positions
        try:
            r = await s.get(
                f"{CLOB_URL}/data/position",
                params={"address": BOT_ADDRESS.lower()},
            )
            if r.status == 200:
                data = await r.json()
                return data if isinstance(data, list) else []
        except Exception:
            pass
    return []


async def get_open_orders() -> list[dict]:
    """Get open orders from CLOB API."""
    async with aiohttp.ClientSession() as s:
        try:
            r = await s.get(
                f"{CLOB_URL}/data/orders",
                params={"address": BOT_ADDRESS.lower(), "state": "LIVE"},
            )
            if r.status == 200:
                data = await r.json()
                return data if isinstance(data, list) else []
        except Exception:
            pass
    return []


async def get_market_info(condition_ids: list[str]) -> dict[str, dict]:
    """Get market info from Gamma for display."""
    info = {}
    async with aiohttp.ClientSession() as s:
        for cid in condition_ids[:20]:  # Cap to avoid spam
            try:
                r = await s.get(f"{GAMMA_URL}/markets", params={"conditionId": cid})
                if r.status == 200:
                    data = await r.json()
                    if data:
                        m = data[0]
                        info[cid] = {
                            "question": m.get("question", "?")[:60],
                            "volume24hr": float(m.get("volume24hr", 0)),
                            "spread": float(m.get("spread", 0)),
                            "bestBid": float(m.get("bestBid", 0)),
                            "bestAsk": float(m.get("bestAsk", 0)),
                        }
            except Exception:
                pass
    return info


async def get_recent_logs(lines: int = 200) -> str:
    """Get recent bot logs via journalctl."""
    proc = await asyncio.create_subprocess_exec(
        "journalctl", "--user", "-u", "pmm1", "--no-pager",
        "--since", "5 min ago", "-o", "cat",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode()[-8000:]  # Last 8KB


def parse_bot_metrics(logs: str) -> dict:
    """Extract key metrics from recent logs."""
    metrics = {
        "mode": "UNKNOWN",
        "nav": "?",
        "cycle": 0,
        "markets_quoted": 0,
        "orders_submitted": 0,
        "fills": 0,
        "errors": [],
        "escalation_level": 0,
        "last_order_prices": [],
    }

    for line in logs.split("\n"):
        try:
            if "quote_cycle_summary" in line:
                d = json.loads(line)
                metrics["mode"] = d.get("mode", "?")
                metrics["nav"] = d.get("nav", "?")
                metrics["cycle"] = d.get("cycle", 0)
                metrics["markets_quoted"] = d.get("markets_quoted", 0)
                metrics["orders_submitted"] = d.get("submitted", 0)
            elif "fill_detected" in line:
                metrics["fills"] += 1
            elif "escalation" in line.lower():
                try:
                    d = json.loads(line)
                    metrics["escalation_level"] = d.get("escalation_ticks", 0)
                except Exception:
                    pass
            elif "order_created" in line:
                try:
                    d = json.loads(line)
                    metrics["last_order_prices"].append({
                        "token": d.get("token_id", "")[:16],
                        "side": d.get("side"),
                        "price": d.get("price"),
                    })
                except Exception:
                    pass
            elif '"level": "error"' in line or '"level": "critical"' in line:
                # Extract short error
                try:
                    d = json.loads(line)
                    evt = d.get("event", "unknown_error")
                    if evt not in [e.get("event") for e in metrics["errors"][-5:]]:
                        msg = str(
                            d.get("error", d.get("message", ""))
                        )[:80]
                        metrics["errors"].append(
                            {"event": evt, "msg": msg}
                        )
                except Exception:
                    pass
        except Exception:
            continue

    # Dedupe order prices, keep last 10
    metrics["last_order_prices"] = metrics["last_order_prices"][-10:]
    metrics["errors"] = metrics["errors"][-5:]
    return metrics


async def main():
    now = datetime.now(UTC)
    print(f"\n{'='*70}")
    print(f"  💰 MisterMoney Dashboard — {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*70}")

    # ── Wallet ──
    print("\n📊 WALLET BALANCES")
    print("-" * 40)
    try:
        bal = await get_wallet_balances()
        print(f"  USDC.e:  ${bal['usdc_e']:>10.2f}")
        print(f"  USDC:    ${bal['usdc']:>10.2f}")
        print(f"  POL:      {bal['pol']:>10.4f}")
        print("  ─────────────────────")
        print(f"  Total:   ${bal['total_usd']:>10.2f}")
    except Exception as e:
        print(f"  ❌ Error: {e}")

    # ── Positions ──
    print("\n📈 OPEN POSITIONS")
    print("-" * 40)
    try:
        positions = await get_positions()
        if not positions:
            print("  (none)")
        else:
            total_value = 0
            condition_ids = set()
            for p in positions:
                size = float(p.get("size", 0))
                if size <= 0:
                    continue
                cid = p.get("conditionId", p.get("condition_id", "?"))
                condition_ids.add(cid)
                token = p.get("tokenId", p.get("token_id", "?"))[:16]
                side = p.get("outcome", "?")
                avg_price = float(p.get("avgPrice", p.get("avg_price", 0)))
                cur_price = float(p.get("curPrice", p.get("cur_price", avg_price)))
                value = size * cur_price
                pnl = (cur_price - avg_price) * size if avg_price > 0 else 0
                pnl_pct = (pnl / (avg_price * size) * 100) if avg_price * size > 0 else 0
                total_value += value
                pnl_emoji = "🟢" if pnl >= 0 else "🔴"
                print(
                    f"  {pnl_emoji} {size:.1f} shares"
                    f" @ ${avg_price:.3f} → ${cur_price:.3f}"
                    f"  PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%)"
                )
                print(f"     Token: {token}...")
            print("  ─────────────────────")
            print(f"  Position value: ${total_value:.2f}")
    except Exception as e:
        print(f"  ❌ Error: {e}")

    # ── Open Orders ──
    print("\n📋 OPEN ORDERS")
    print("-" * 40)
    try:
        orders = await get_open_orders()
        if not orders:
            print("  (none)")
        else:
            buys = [o for o in orders if o.get("side", "").upper() == "BUY"]
            sells = [o for o in orders if o.get("side", "").upper() == "SELL"]
            print(f"  BUY orders:  {len(buys)}")
            print(f"  SELL orders: {len(sells)}")
            # Show top 5 by price
            for o in sorted(orders, key=lambda x: float(x.get("price", 0)), reverse=True)[:8]:
                side = o.get("side", "?")
                price = o.get("price", "?")
                size = o.get("original_size", o.get("size", "?"))
                token = o.get("asset_id", o.get("token_id", "?"))[:16]
                emoji = "🟩" if side == "BUY" else "🟥"
                print(f"  {emoji} {side} {size} @ ${price}  [{token}...]")
    except Exception as e:
        print(f"  ❌ Error: {e}")

    # ── Bot Metrics ──
    print("\n🤖 BOT STATUS")
    print("-" * 40)
    try:
        logs = await get_recent_logs()
        m = parse_bot_metrics(logs)
        print(f"  Mode:      {m['mode']}")
        print(f"  NAV:       ${m['nav']}")
        print(f"  Cycle:     {m['cycle']}")
        print(f"  Markets:   {m['markets_quoted']} quoted")
        print(f"  Fills (5m): {m['fills']}")
        if m.get('escalation_level', 0) > 0:
            print(f"  ⚡ Escalation: +{m['escalation_level']} ticks")

        if m["last_order_prices"]:
            print("\n  Recent Orders:")
            for op in m["last_order_prices"][-6:]:
                emoji = "🟩" if op["side"] == "BUY" else "🟥"
                print(f"    {emoji} {op['side']} @ ${op['price']}  [{op['token']}...]")

        if m["errors"]:
            print("\n  ⚠️  Recent Errors:")
            for e in m["errors"][-3:]:
                print(f"    ❌ {e['event']}: {e['msg'][:60]}")
    except Exception as e:
        print(f"  ❌ Error: {e}")

    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    asyncio.run(main())

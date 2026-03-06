#!/usr/bin/env python3
"""Quick Gamma API field inspector.

Usage:
  python3 scripts/debug_gamma_fields.py
"""

from __future__ import annotations

import asyncio
import json

import aiohttp

from pmm1.api.gamma import GammaMarket


async def main() -> None:
    # 1) Print raw fields from Gamma directly
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "limit": "5"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            resp.raise_for_status()
            raw_markets = await resp.json()

    print(f"Fetched {len(raw_markets)} raw markets from Gamma")
    for i, m in enumerate(raw_markets[:3], 1):
        print(f"\n--- Raw market #{i} ---")
        interesting = {
            "conditionId": m.get("conditionId"),
            "question": m.get("question"),
            "active": m.get("active"),
            "closed": m.get("closed"),
            "enableOrderBook": m.get("enableOrderBook"),
            "volume24hr": m.get("volume24hr"),
            "liquidity": m.get("liquidity"),
            "spread": m.get("spread"),
            "bestBid": m.get("bestBid"),
            "bestAsk": m.get("bestAsk"),
            "clobTokenIds": m.get("clobTokenIds"),
        }
        print(json.dumps(interesting, indent=2, default=str)[:1200])

    # 2) Print parsed fields through our model
    markets = [GammaMarket.model_validate(m) for m in raw_markets]
    print(f"\nParsed {len(markets)} markets through GammaMarket model")
    for i, m in enumerate(markets[:3], 1):
        print(f"\n--- Parsed market #{i} ---")
        print(f"condition_id: {m.condition_id}")
        print(f"question: {m.question[:100]}")
        print(f"volume_24hr: {m.volume_24hr} ({type(m.volume_24hr).__name__})")
        print(f"liquidity: {m.liquidity} ({type(m.liquidity).__name__})")
        print(f"spread: {m.spread}")
        print(f"best_bid / best_ask: {m.best_bid} / {m.best_ask}")
        print(f"token_ids: {m.token_ids[:2]}")


if __name__ == "__main__":
    asyncio.run(main())

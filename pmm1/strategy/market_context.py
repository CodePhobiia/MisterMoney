"""Market context builder for the LLM reasoner.

Fetches and formats price history, book depth, and cross-market
signals for injection into Opus prompts.
"""

from __future__ import annotations

import math
import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class MarketContextBuilder:
    """Builds rich context strings for LLM prompts from live bot state."""

    def __init__(self, data_api: Any = None) -> None:
        self.data_api = data_api
        self._price_cache: dict[str, tuple[float, str]] = {}
        self._cache_ttl = 120.0  # 2 min cache

    def build_book_summary(self, book: Any) -> str:
        """Build detailed order book summary from OrderBook object.

        Includes: best bid/ask with sizes, spread, microprice,
        imbalance, and depth within 2 cents.
        """
        if book is None:
            return ""

        mid = book.get_midpoint()
        microprice = book.get_microprice()
        imbalance = book.get_imbalance()

        bids = book.get_bids(3)
        asks = book.get_asks(3)

        best_bid = bids[0].price if bids else 0.0
        best_ask = asks[0].price if asks else 1.0
        bid_size = bids[0].size if bids else 0.0
        ask_size = asks[0].size if asks else 0.0
        spread = best_ask - best_bid

        # Depth within 2 cents
        bid_depth = book.get_depth_within(0.02, side="bid")
        ask_depth = book.get_depth_within(0.02, side="ask")

        lines = [
            f"Best bid: {best_bid:.3f} ({bid_size:.0f} shares)",
            f"Best ask: {best_ask:.3f} ({ask_size:.0f} shares)",
            f"Spread: {spread:.3f} ({spread*100:.1f} cents)",
            f"Midpoint: {mid:.3f}",
        ]

        if microprice is not None:
            micro_skew = (microprice - mid) * 100
            direction = "buy pressure" if micro_skew > 0 else "sell pressure"
            lines.append(
                f"Microprice: {microprice:.3f} "
                f"({micro_skew:+.1f}\u00a2 = {direction})"
            )

        if abs(imbalance) > 0.01:
            pct = imbalance * 100
            direction = "buy-heavy" if imbalance > 0 else "sell-heavy"
            lines.append(f"Imbalance: {pct:+.0f}% ({direction})")

        lines.append(
            f"Depth within 2\u00a2: "
            f"Bid {bid_depth:.0f}, Ask {ask_depth:.0f} shares"
        )

        # Show top 3 levels if available
        if len(bids) >= 2 or len(asks) >= 2:
            levels = []
            for i, b in enumerate(bids[:3]):
                levels.append(f"  Bid L{i+1}: {b.price:.3f} x {b.size:.0f}")
            for i, a in enumerate(asks[:3]):
                levels.append(f"  Ask L{i+1}: {a.price:.3f} x {a.size:.0f}")
            lines.append("Order book depth:\n" + "\n".join(levels))

        return "\n".join(lines)

    def build_market_metadata(self, md: Any) -> str:
        """Format market metadata for prompt injection."""
        lines = []

        # Category
        if getattr(md, "is_sports", False):
            category = "Sports"
        elif getattr(md, "is_crypto_intraday", False):
            category = "Crypto (intraday)"
        elif getattr(md, "theme", ""):
            category = md.theme
        else:
            category = "General"
        lines.append(f"Category: {category}")

        # Time to resolution
        end_date = getattr(md, "end_date", None)
        if end_date:
            from datetime import UTC, datetime
            now = datetime.now(UTC)
            if hasattr(end_date, 'tzinfo') and end_date.tzinfo is None:
                # Naive datetime, assume UTC
                end_date = end_date.replace(tzinfo=UTC)
            remaining = end_date - now
            hours = remaining.total_seconds() / 3600
            if hours < 1:
                lines.append(f"Resolves in: {hours*60:.0f} minutes (URGENT)")
            elif hours < 24:
                lines.append(f"Resolves in: {hours:.1f} hours")
            elif hours < 168:
                lines.append(f"Resolves in: {hours/24:.1f} days")
            else:
                lines.append(f"Resolves in: {hours/24:.0f} days")

        # Volume and liquidity
        vol = getattr(md, "volume_24h", 0)
        liq = getattr(md, "liquidity", 0)
        if vol > 0:
            lines.append(f"24h volume: ${vol:,.0f}")
        if liq > 0:
            lines.append(f"Liquidity: ${liq:,.0f}")

        # Toxicity / adverse selection
        tox = getattr(md, "toxicity_estimate", 0)
        if tox > 0.005:
            lines.append(
                f"Adverse selection risk: {tox*100:.1f} bps "
                f"({'HIGH' if tox > 0.02 else 'moderate'})"
            )

        # Reward eligibility
        if getattr(md, "reward_eligible", False):
            rate = getattr(md, "reward_daily_rate", 0)
            lines.append(f"Reward eligible: Yes (rate: {rate:.4f})")

        # Neg-risk
        if getattr(md, "neg_risk", False):
            lines.append("Neg-risk market: Yes")

        # Fees
        if getattr(md, "fees_enabled", False):
            fee = getattr(md, "fee_rate", 0)
            lines.append(f"Fees: {fee*100:.2f}%")

        return "\n".join(lines)

    async def build_price_context(
        self, token_id: str,
    ) -> str:
        """Fetch and format 24h price history for a token.

        Returns trend, volatility, and price range summary.
        """
        if not self.data_api:
            return ""

        # Check cache
        cached = self._price_cache.get(token_id)
        if cached and (time.time() - cached[0]) < self._cache_ttl:
            return cached[1]

        try:
            import time as _time
            now = int(_time.time())
            start = now - 86400  # 24h ago

            points = await self.data_api.get_prices_history(
                token_id=token_id,
                interval="1h",
                fidelity=24,
                start_ts=start,
                end_ts=now,
            )

            if not points or len(points) < 2:
                return ""

            prices = [float(p.price) for p in points if hasattr(p, 'price')]
            if not prices:
                return ""

            current = prices[-1]
            open_24h = prices[0]
            high = max(prices)
            low = min(prices)
            change_24h = current - open_24h
            change_pct = (change_24h / open_24h * 100) if open_24h > 0 else 0

            # Compute simple volatility
            if len(prices) >= 3:
                returns = [
                    (prices[i] - prices[i-1]) / max(prices[i-1], 0.001)
                    for i in range(1, len(prices))
                ]
                mean_ret = sum(returns) / len(returns)
                var = sum((r - mean_ret)**2 for r in returns) / len(returns)
                vol = math.sqrt(var) * 100  # as percentage
            else:
                vol = 0.0

            # Recent trend (last 4 hours)
            recent = prices[-4:] if len(prices) >= 4 else prices
            recent_change = recent[-1] - recent[0]
            recent_direction = "rising" if recent_change > 0.005 else (
                "falling" if recent_change < -0.005 else "flat"
            )

            lines = [
                f"24h change: {change_24h:+.3f} ({change_pct:+.1f}%)",
                f"24h range: {low:.3f} \u2014 {high:.3f}",
                f"Volatility: {vol:.1f}%",
                f"Recent trend (4h): {recent_direction} "
                f"({recent_change:+.3f})",
            ]

            result = "\n".join(lines)
            self._price_cache[token_id] = (time.time(), result)
            return result

        except Exception as e:
            logger.debug(
                "price_context_fetch_failed",
                token_id=token_id[:16],
                error=str(e),
            )
            return ""

    def build_cross_market_context(
        self,
        condition_id: str,
        event_id: str,
        active_markets: dict[str, Any],
        book_manager: Any,
    ) -> str:
        """Find related markets in the same event and format context.

        Detects parity violations and inconsistencies.
        """
        if not event_id:
            return ""

        related = []
        for cid, md in active_markets.items():
            if cid == condition_id:
                continue
            if getattr(md, "event_id", "") == event_id:
                tid = getattr(md, "token_id_yes", "")
                book = book_manager.get(tid) if book_manager else None
                mid = book.get_midpoint() if book else 0.5
                question = getattr(md, "question", "")
                related.append({
                    "question": question[:80],
                    "midpoint": mid,
                    "condition_id": cid,
                })

        if not related:
            return ""

        lines = ["RELATED MARKETS (same event):"]
        total_prob = 0.0
        for r in related:
            lines.append(
                f"  - \"{r['question']}\" @ {r['midpoint']:.3f}"
            )
            total_prob += float(r["midpoint"])

        # Add current market to total
        # (caller should have the current midpoint)
        if len(related) >= 1:
            # Flag if sum of related probabilities seems off
            if total_prob > 1.1:
                lines.append(
                    f"  \u26a0 Sum of related markets: "
                    f"{total_prob:.2f} (possible overpricing)"
                )
            elif total_prob < 0.8 and len(related) >= 2:
                lines.append(
                    f"  \u26a0 Sum of related markets: "
                    f"{total_prob:.2f} (possible underpricing)"
                )

        return "\n".join(lines)

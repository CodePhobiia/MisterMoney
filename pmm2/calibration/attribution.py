"""Daily PnL decomposition and reporting."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pmm1.storage.database import Database

logger = structlog.get_logger(__name__)


class AttributionEngine:
    """Daily PnL decomposition and reporting.

    Decomposes realized PnL into:
    - Spread capture
    - Arb profits
    - Liquidity rewards
    - Maker rebates
    - Toxicity losses
    - Resolution losses
    - Gas costs
    - Net PnL
    """

    def __init__(self, db: Database):
        """Initialize attribution engine.

        Args:
            db: Database instance
        """
        self.db = db

    async def compute_daily_attribution(self, date: str) -> dict[str, Any]:
        """Compute PnL attribution for a given date.

        Returns:
        {
            'date': '2026-03-07',
            'spread_pnl': 1.23,
            'arb_pnl': 0.45,
            'reward_income': 0.80,
            'rebate_income': 0.12,
            'toxicity_loss': -0.35,
            'resolution_loss': 0.0,
            'gas_cost': -0.05,
            'net_pnl': 2.20,
            'fills_count': 15,
            'markets_traded': 8,
            'scoring_uptime_pct': 85.2,
            'reward_capture_eff': 0.78,
            'rebate_capture_eff': 0.65,
        }

        Args:
            date: Date string (YYYY-MM-DD)
        """
        result: dict[str, Any] = {
            'date': date,
            'spread_pnl': 0.0,
            'arb_pnl': 0.0,
            'reward_income': 0.0,
            'rebate_income': 0.0,
            'toxicity_loss': 0.0,
            'resolution_loss': 0.0,
            'gas_cost': 0.0,
            'net_pnl': 0.0,
            'fills_count': 0,
            'markets_traded': 0,
            'scoring_uptime_pct': 0.0,
            'reward_capture_eff': 0.0,
            'rebate_capture_eff': 0.0,
        }

        # 1. Fetch fills for the day
        fills = await self.db.fetch_all(
            """
            SELECT
                condition_id,
                side,
                price,
                size,
                dollar_value,
                fee,
                markout_1s,
                markout_5s,
                markout_30s,
                mid_at_fill,
                is_scoring
            FROM fill_record
            WHERE DATE(ts) = ?
            """,
            (date,)
        )

        result['fills_count'] = len(fills)

        if fills:
            markets = set(fill['condition_id'] for fill in fills)
            result['markets_traded'] = len(markets)

        # 2. Compute spread capture
        # Spread PnL = sum of (execution_price - mid) * size for each fill
        spread_pnl = 0.0
        for fill in fills:
            mid = fill.get('mid_at_fill', fill['price'])
            if fill['side'] == 'BUY':
                # For BUY, spread capture is negative if we paid above mid
                spread_delta = mid - fill['price']
            else:  # SELL
                # For SELL, spread capture is positive if we sold above mid
                spread_delta = fill['price'] - mid

            spread_pnl += spread_delta * fill['size']

        result['spread_pnl'] = spread_pnl

        # 3. Compute toxicity loss
        # Toxicity = weighted average of adverse markouts
        # Default weights: (0.5, 0.3, 0.2)
        toxicity_loss = 0.0
        for fill in fills:
            if all(fill.get(k) is not None for k in ['markout_1s', 'markout_5s', 'markout_30s']):
                # Adverse markout = negative for profitable, positive for adverse
                # For BUY: if markout_1s > 0 (price went up), that's good (negative adverse)
                # For SELL: if markout_1s < 0 (price went down), that's good (negative adverse)
                if fill['side'] == 'BUY':
                    adverse_1s = -fill['markout_1s'] * fill['size']
                    adverse_5s = -fill['markout_5s'] * fill['size']
                    adverse_30s = -fill['markout_30s'] * fill['size']
                else:  # SELL
                    adverse_1s = fill['markout_1s'] * fill['size']
                    adverse_5s = fill['markout_5s'] * fill['size']
                    adverse_30s = fill['markout_30s'] * fill['size']

                weighted_adverse = (
                    0.5 * adverse_1s +
                    0.3 * adverse_5s +
                    0.2 * adverse_30s
                )
                toxicity_loss += weighted_adverse

        result['toxicity_loss'] = toxicity_loss

        # 4. Compute gas costs (from fill fees)
        gas_cost = sum(fill.get('fee', 0.0) for fill in fills)
        result['gas_cost'] = -gas_cost  # Negative because it's a cost

        # 5. Fetch reward income
        rewards = await self.db.fetch_all(
            """
            SELECT
                SUM(realized_liq_reward_usdc) as total_realized,
                AVG(capture_efficiency) as avg_efficiency
            FROM reward_actual
            WHERE date = ?
            """,
            (date,)
        )

        if rewards and rewards[0]['total_realized'] is not None:
            result['reward_income'] = rewards[0]['total_realized']
            result['reward_capture_eff'] = rewards[0]['avg_efficiency'] or 0.0

        # 6. Fetch rebate income
        rebates = await self.db.fetch_all(
            """
            SELECT
                SUM(realized_rebate_usdc) as total_realized,
                AVG(capture_efficiency) as avg_efficiency
            FROM rebate_actual
            WHERE date = ?
            """,
            (date,)
        )

        if rebates and rebates[0]['total_realized'] is not None:
            result['rebate_income'] = rebates[0]['total_realized']
            result['rebate_capture_eff'] = rebates[0]['avg_efficiency'] or 0.0

        # 7. Compute scoring uptime
        # Count how many fills were scoring vs total fills
        scoring_fills = sum(1 for fill in fills if fill.get('is_scoring'))
        if int(result['fills_count']) > 0:
            result['scoring_uptime_pct'] = (scoring_fills / int(result['fills_count'])) * 100.0

        # 8. Arb PnL estimation
        # Fetch arb EV from market_score table for the day
        arb_scores = await self.db.fetch_all(
            """
            SELECT
                SUM(arb_ev_bps * target_capital_usdc / 10000.0) as total_arb_ev
            FROM market_score
            WHERE DATE(ts) = ?
            """,
            (date,)
        )

        if arb_scores and arb_scores[0]['total_arb_ev'] is not None:
            # Estimate realized arb as 50% of EV (conservative)
            result['arb_pnl'] = arb_scores[0]['total_arb_ev'] * 0.5

        # 9. Resolution loss (for now, assume 0 - would need position tracking)
        result['resolution_loss'] = 0.0

        # 10. Compute net PnL
        result['net_pnl'] = (
            float(result['spread_pnl']) +
            float(result['arb_pnl']) +
            float(result['reward_income']) +
            float(result['rebate_income']) +
            float(result['toxicity_loss']) +  # Already negative if it's a loss
            float(result['resolution_loss']) +
            float(result['gas_cost'])  # Already negative
        )

        logger.info(
            "daily_attribution_computed",
            date=date,
            net_pnl=result['net_pnl'],
            fills=result['fills_count'],
            markets=result['markets_traded']
        )

        return result

    async def generate_telegram_summary(self, date: str) -> str:
        """Generate a Telegram-friendly daily summary.

        Example:
        📊 Daily Report — 2026-03-07

        💰 Net PnL: +$2.20

        Revenue:
        • Spread: +$1.23
        • Arb: +$0.45
        • Rewards: +$0.80
        • Rebates: +$0.12

        Costs:
        • Toxicity: -$0.35
        • Gas: -$0.05

        📈 15 fills across 8 markets
        🎯 Scoring uptime: 85.2%

        Args:
            date: Date string (YYYY-MM-DD)
        """
        attr = await self.compute_daily_attribution(date)

        # Format net PnL with sign
        net_pnl = attr['net_pnl']
        pnl_sign = '+' if net_pnl >= 0 else ''
        pnl_str = f"{pnl_sign}${net_pnl:.2f}"

        # Format revenue items (positive values)
        revenue_items = []
        if attr['spread_pnl'] != 0:
            revenue_items.append(f"• Spread: ${attr['spread_pnl']:.2f}")
        if attr['arb_pnl'] != 0:
            revenue_items.append(f"• Arb: ${attr['arb_pnl']:.2f}")
        if attr['reward_income'] != 0:
            revenue_items.append(f"• Rewards: ${attr['reward_income']:.2f}")
        if attr['rebate_income'] != 0:
            revenue_items.append(f"• Rebates: ${attr['rebate_income']:.2f}")

        revenue_section = '\n'.join(revenue_items) if revenue_items else "• None"

        # Format cost items (negative values)
        cost_items = []
        if attr['toxicity_loss'] < 0:
            cost_items.append(f"• Toxicity: ${attr['toxicity_loss']:.2f}")
        if attr['gas_cost'] < 0:
            cost_items.append(f"• Gas: ${attr['gas_cost']:.2f}")
        if attr['resolution_loss'] < 0:
            cost_items.append(f"• Resolution: ${attr['resolution_loss']:.2f}")

        cost_section = '\n'.join(cost_items) if cost_items else "• None"

        # Build summary
        summary = f"""📊 Daily Report — {date}

💰 Net PnL: {pnl_str}

Revenue:
{revenue_section}

Costs:
{cost_section}

📈 {attr['fills_count']} fills across {attr['markets_traded']} markets"""

        # Add scoring uptime if we had fills
        if attr['fills_count'] > 0:
            summary += f"\n🎯 Scoring uptime: {attr['scoring_uptime_pct']:.1f}%"

        # Add capture efficiency if we had rewards/rebates
        if attr['reward_income'] > 0 or attr['rebate_income'] > 0:
            summary += f"\n💎 Reward capture: {attr['reward_capture_eff']*100:.0f}%"
            if attr['rebate_income'] > 0:
                summary += f", Rebate capture: {attr['rebate_capture_eff']*100:.0f}%"

        return summary

    async def send_daily_report(
        self, date: str,
        chat_id: str = os.getenv("TELEGRAM_CHAT_ID", ""),
    ) -> None:
        """Generate and send daily report via Telegram.

        Args:
            date: Date string (YYYY-MM-DD)
            chat_id: Telegram chat ID to send to
        """
        from pmm1.notifications import send_telegram

        summary = await self.generate_telegram_summary(date)
        if summary:
            await send_telegram(summary)
            logger.info("daily_report_sent", date=date, chat_id=chat_id)

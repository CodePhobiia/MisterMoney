"""V1 state snapshot — capture V1 bot's current trading state.

Called at the start of each PMM-2 allocator cycle to capture:
- Which markets V1 is currently quoting
- Live orders (price, size, side, scoring status)
- Positions (size, cost basis, unrealized P&L)
- Capital deployment metrics
- NAV (net asset value)

This snapshot serves as the "actual" baseline for counterfactual comparison.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class V1StateSnapshot:
    """Capture V1's current state for counterfactual comparison.

    Called at the start of each PMM-2 allocator cycle.
    """

    @staticmethod
    def capture(bot_state) -> dict[str, Any]:
        """Capture V1's current trading state.

        Returns:
        {
            'timestamp': ISO string,
            'markets': set of condition_ids being quoted,
            'orders': list of {token_id, side, price, size, status, is_scoring},
            'positions': list of {condition_id, size, cost_basis, unrealized_pnl},
            'scoring_count': int,
            'reward_eligible_count': int,
            'total_capital_deployed': float,
            'nav': float,
        }

        Args:
            bot_state: V1 bot state object (has wallet, order_tracker, positions, etc.)
        """
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "markets": set(),
            "orders": [],
            "positions": [],
            "scoring_count": 0,
            "reward_eligible_count": 0,
            "total_capital_deployed": 0.0,
            "nav": 100.0,  # Default fallback
        }

        try:
            # Get NAV (net asset value)
            nav = V1StateSnapshot._get_nav(bot_state)
            snapshot["nav"] = nav

            # Get active orders
            if hasattr(bot_state, "order_tracker"):
                active_orders = bot_state.order_tracker.get_active_orders(token_id=None)

                for order in active_orders:
                    # Extract order details
                    order_dict = {
                        "order_id": getattr(order, "order_id", ""),
                        "token_id": getattr(order, "token_id", ""),
                        "condition_id": getattr(order, "condition_id", ""),
                        "side": getattr(order, "side", ""),
                        "price": float(getattr(order, "price", 0.0)),
                        "size": float(getattr(order, "size", 0.0)),
                        "status": getattr(order, "status", ""),
                        "is_scoring": getattr(order, "is_scoring", False),
                    }

                    snapshot["orders"].append(order_dict)

                    # Track which markets we're in
                    cid = order_dict.get("condition_id")
                    if cid:
                        snapshot["markets"].add(cid)

                    # Count scoring orders
                    if order_dict.get("is_scoring", False):
                        snapshot["scoring_count"] += 1

                    # Estimate capital deployed (size * price for each side)
                    size = order_dict.get("size", 0.0)
                    price = order_dict.get("price", 0.0)
                    if order_dict.get("side") == "BUY":
                        snapshot["total_capital_deployed"] += size * price
                    else:  # SELL
                        snapshot["total_capital_deployed"] += size * (1 - price)

            # Get positions
            if hasattr(bot_state, "position_tracker"):
                positions = bot_state.position_tracker.get_active_positions()

                for pos in positions:
                    pos_dict = {
                        "condition_id": getattr(pos, "condition_id", ""),
                        "token_id": getattr(pos, "token_id", ""),
                        "size": float(getattr(pos, "size", 0.0)),
                        "cost_basis": float(getattr(pos, "cost_basis", 0.0)),
                        "unrealized_pnl": float(getattr(pos, "unrealized_pnl", 0.0)),
                    }

                    snapshot["positions"].append(pos_dict)

            # Estimate reward-eligible count
            # Markets with scoring orders are likely reward-eligible
            snapshot["reward_eligible_count"] = len(
                [o for o in snapshot["orders"] if o.get("is_scoring", False)]
            )

            # Convert markets set to list for JSON serialization
            snapshot["markets"] = list(snapshot["markets"])

            logger.debug(
                "v1_state_captured",
                markets=len(snapshot["markets"]),
                orders=len(snapshot["orders"]),
                positions=len(snapshot["positions"]),
                scoring_count=snapshot["scoring_count"],
                nav=snapshot["nav"],
            )

        except Exception as e:
            logger.error(
                "v1_state_capture_failed",
                error=str(e),
                exc_info=True,
            )
            # Return partial snapshot on error
            snapshot["error"] = str(e)

        return snapshot

    @staticmethod
    def _get_nav(bot_state) -> float:
        """Get current NAV from bot state.

        Args:
            bot_state: V1 bot state object

        Returns:
            NAV in USDC (default 100.0 if not available)
        """
        # Try different possible attributes
        if hasattr(bot_state, "nav"):
            return float(bot_state.nav)
        if hasattr(bot_state, "wallet_balance"):
            return float(bot_state.wallet_balance)
        if hasattr(bot_state, "total_equity"):
            return float(bot_state.total_equity)

        # Fallback to a default
        logger.warning("nav_not_available_using_default")
        return 100.0

    @staticmethod
    def summarize(snapshot: dict[str, Any]) -> str:
        """Generate human-readable summary of V1 state.

        Args:
            snapshot: snapshot dict from capture()

        Returns:
            Multi-line summary string
        """
        lines = [
            f"V1 State @ {snapshot.get('timestamp', 'unknown')}",
            f"Markets: {len(snapshot.get('markets', []))}",
            f"Orders: {len(snapshot.get('orders', []))}",
            f"Scoring: {snapshot.get('scoring_count', 0)}",
            f"Reward eligible: {snapshot.get('reward_eligible_count', 0)}",
            f"Capital deployed: ${snapshot.get('total_capital_deployed', 0):.2f}",
            f"NAV: ${snapshot.get('nav', 0):.2f}",
        ]

        return "\n".join(lines)

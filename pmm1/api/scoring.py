"""Order scoring probe — check if orders are scoring for rewards.

Uses the py-clob-client SDK's are_orders_scoring() method (Level 2 auth).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


async def check_orders_scoring(
    client: Any,  # ClobPrivateClient
    order_ids: list[str],
) -> dict[str, bool]:
    """Check if orders are scoring for rewards using SDK.

    Args:
        client: ClobPrivateClient instance (with SDK client initialized)
        order_ids: List of order IDs to check

    Returns:
        Dict of {order_id: is_scoring}
    """
    if not order_ids:
        return {}

    # Get the SDK client (lazy-initialized in ClobPrivateClient)
    sdk_client = client._get_sdk_client()

    def _check_scoring():
        """Synchronous SDK call — run in thread."""
        try:
            # SDK method: are_orders_scoring(order_ids: list[str]) -> dict
            # Returns: {"12345": True, "67890": False}
            result = sdk_client.are_orders_scoring(order_ids)
            return result
        except Exception as e:
            logger.error(
                "sdk_scoring_check_failed",
                order_ids=order_ids,
                error=str(e),
            )
            # Return all False on error
            return {oid: False for oid in order_ids}

    try:
        data = await asyncio.to_thread(_check_scoring)

        # Normalize result: ensure all order_ids are present
        result = {}
        for oid in order_ids:
            result[oid] = data.get(oid, False)

        scoring_count = sum(1 for v in result.values() if v)
        logger.debug(
            "order_scoring_checked",
            total=len(order_ids),
            scoring=scoring_count,
        )

        return result

    except Exception as e:
        logger.error("scoring_check_exception", error=str(e))
        return {oid: False for oid in order_ids}


async def check_order_scoring(
    client: Any,  # ClobPrivateClient
    order_id: str,
) -> bool:
    """Check if a single order is scoring for rewards.

    Args:
        client: ClobPrivateClient instance
        order_id: Order ID to check

    Returns:
        True if order is scoring, False otherwise
    """
    result = await check_orders_scoring(client, [order_id])
    return result.get(order_id, False)

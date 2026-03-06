"""Order batcher — batch orders in chunks of 15 per §2.

POST /orders capped at 15 per request.
"""

from __future__ import annotations

from typing import TypeVar

import structlog

logger = structlog.get_logger(__name__)

T = TypeVar("T")

MAX_BATCH_SIZE = 15


class OrderBatcher:
    """Splits order lists into batches of max 15 for the CLOB API."""

    def __init__(self, max_batch_size: int = MAX_BATCH_SIZE) -> None:
        self._max_batch_size = max_batch_size

    def batch(self, orders: list[T]) -> list[list[T]]:
        """Split orders into batches of max_batch_size.

        Args:
            orders: List of order requests.

        Returns:
            List of batches, each containing up to max_batch_size orders.
        """
        if not orders:
            return []

        batches: list[list[T]] = []
        for i in range(0, len(orders), self._max_batch_size):
            batch = orders[i : i + self._max_batch_size]
            batches.append(batch)

        if len(batches) > 1:
            logger.info(
                "orders_batched",
                total_orders=len(orders),
                num_batches=len(batches),
                max_batch_size=self._max_batch_size,
            )

        return batches

    def prioritize_and_batch(
        self,
        cancels: list[T],
        new_orders: list[T],
    ) -> tuple[list[list[T]], list[list[T]]]:
        """Batch cancels and new orders separately.

        Cancels should execute before new orders to free up capacity.

        Returns:
            (cancel_batches, new_order_batches)
        """
        return self.batch(cancels), self.batch(new_orders)

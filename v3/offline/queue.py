"""
Escalation Queue — Redis sorted set queue for markets needing async review

Priority factors:
- Market notional (higher $ = higher priority)
- Dispute risk (from rule-heavy route)
- Model disagreement (from dossier route)
- Time to resolution (closer = higher priority)
- Uncertainty (higher = more valuable to resolve)
"""

import json
from datetime import datetime
from typing import Optional
import redis.asyncio as redis
import structlog

log = structlog.get_logger()


class EscalationQueue:
    """
    Redis sorted set queue for markets needing async review.
    Priority score = urgency (higher = process first).
    """
    
    QUEUE_KEY = "v3:escalation_queue"
    METADATA_PREFIX = "v3:escalation:"
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        """
        Initialize escalation queue
        
        Args:
            redis_url: Redis connection URL
        """
        self.redis_url = redis_url
        self.client: Optional[redis.Redis] = None
        
    async def connect(self) -> None:
        """Connect to Redis"""
        if self.client is not None:
            log.debug("escalation_queue_already_connected")
            return
            
        log.info("escalation_queue_connecting", url=self.redis_url)
        self.client = await redis.from_url(
            self.redis_url,
            decode_responses=True,
            encoding='utf-8'
        )
        log.info("escalation_queue_connected")
        
    async def close(self) -> None:
        """Close Redis connection"""
        if self.client is None:
            return
            
        log.info("escalation_queue_closing")
        await self.client.close()
        self.client = None
        log.info("escalation_queue_closed")
        
    async def enqueue(
        self,
        condition_id: str,
        reason: str,
        priority: float,
        metadata: dict
    ) -> None:
        """
        Add market to escalation queue.
        
        Uses Redis sorted set 'v3:escalation_queue' with priority as score.
        Also stores metadata in 'v3:escalation:{condition_id}'.
        Deduplicates: if already queued, update priority if higher.
        
        Args:
            condition_id: Polymarket condition ID
            reason: Why this market was escalated
            priority: Priority score (higher = more urgent)
            metadata: Additional context (notional, route, etc.)
        """
        if self.client is None:
            await self.connect()
            
        # Check if already queued
        existing_priority = await self.client.zscore(self.QUEUE_KEY, condition_id)
        
        if existing_priority is not None:
            # Already queued — update priority if higher
            if priority > existing_priority:
                log.info(
                    "escalation_priority_updated",
                    condition_id=condition_id,
                    old_priority=round(existing_priority, 2),
                    new_priority=round(priority, 2),
                    reason=reason
                )
                await self.client.zadd(self.QUEUE_KEY, {condition_id: priority})
                
                # Update metadata
                metadata_key = f"{self.METADATA_PREFIX}{condition_id}"
                enriched_metadata = {
                    **metadata,
                    "reason": reason,
                    "priority": priority,
                    "enqueued_at": datetime.utcnow().isoformat(),
                    "updated_at": datetime.utcnow().isoformat(),
                }
                await self.client.set(metadata_key, json.dumps(enriched_metadata))
            else:
                log.debug(
                    "escalation_priority_unchanged",
                    condition_id=condition_id,
                    existing_priority=round(existing_priority, 2),
                    new_priority=round(priority, 2)
                )
        else:
            # New escalation
            log.info(
                "market_escalated",
                condition_id=condition_id,
                priority=round(priority, 2),
                reason=reason,
                metadata=metadata
            )
            
            # Add to sorted set
            await self.client.zadd(self.QUEUE_KEY, {condition_id: priority})
            
            # Store metadata
            metadata_key = f"{self.METADATA_PREFIX}{condition_id}"
            enriched_metadata = {
                **metadata,
                "reason": reason,
                "priority": priority,
                "enqueued_at": datetime.utcnow().isoformat(),
            }
            await self.client.set(metadata_key, json.dumps(enriched_metadata))
            
    async def dequeue(self) -> Optional[tuple[str, dict]]:
        """
        Pop highest-priority item from queue
        
        Returns:
            Tuple of (condition_id, metadata) or None if queue is empty
        """
        if self.client is None:
            await self.connect()
            
        # Pop highest-priority item (ZREVRANGE + ZREM)
        items = await self.client.zrevrange(self.QUEUE_KEY, 0, 0)
        
        if not items:
            return None
            
        condition_id = items[0]
        
        # Remove from queue
        await self.client.zrem(self.QUEUE_KEY, condition_id)
        
        # Get metadata
        metadata_key = f"{self.METADATA_PREFIX}{condition_id}"
        metadata_json = await self.client.get(metadata_key)
        
        if metadata_json:
            metadata = json.loads(metadata_json)
            # Delete metadata after dequeue
            await self.client.delete(metadata_key)
        else:
            metadata = {}
            
        log.info(
            "market_dequeued",
            condition_id=condition_id,
            priority=metadata.get("priority")
        )
        
        return (condition_id, metadata)
        
    async def peek(self, n: int = 10) -> list[dict]:
        """
        View top N items without removing
        
        Args:
            n: Number of items to return
            
        Returns:
            List of {condition_id, priority, metadata} dicts
        """
        if self.client is None:
            await self.connect()
            
        # Get top N items with scores
        items = await self.client.zrevrange(self.QUEUE_KEY, 0, n - 1, withscores=True)
        
        result = []
        for condition_id, priority in items:
            metadata_key = f"{self.METADATA_PREFIX}{condition_id}"
            metadata_json = await self.client.get(metadata_key)
            
            metadata = json.loads(metadata_json) if metadata_json else {}
            
            result.append({
                "condition_id": condition_id,
                "priority": priority,
                **metadata
            })
            
        return result
        
    async def size(self) -> int:
        """Get current queue size"""
        if self.client is None:
            await self.connect()
            
        return await self.client.zcard(self.QUEUE_KEY)
        
    async def remove(self, condition_id: str) -> bool:
        """
        Remove a specific market from queue
        
        Args:
            condition_id: Market to remove
            
        Returns:
            True if removed, False if not in queue
        """
        if self.client is None:
            await self.connect()
            
        # Remove from sorted set
        removed = await self.client.zrem(self.QUEUE_KEY, condition_id)
        
        if removed:
            # Delete metadata
            metadata_key = f"{self.METADATA_PREFIX}{condition_id}"
            await self.client.delete(metadata_key)
            
            log.info("market_removed_from_queue", condition_id=condition_id)
            return True
        else:
            log.debug("market_not_in_queue", condition_id=condition_id)
            return False

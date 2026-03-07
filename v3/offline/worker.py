"""
Offline Worker — Async processor for escalated markets

Uses GPT-5.4-pro (or GPT-5.4 fallback) to perform deep review of high-stakes markets.
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional
import structlog

from v3.evidence.db import Database
from v3.evidence.entities import FairValueSignal
from v3.providers.registry import ProviderRegistry
from v3.serving.publisher import SignalPublisher
from .queue import EscalationQueue
from .prompts import OFFLINE_ADJUDICATION_SYSTEM, build_adjudication_prompt

log = structlog.get_logger()


class OfflineWorker:
    """
    Async worker that processes escalated markets with GPT-5.4-pro.
    
    Process:
    1. Dequeue highest-priority market
    2. Gather all evidence + existing route signals
    3. Run GPT-5.4-pro (or GPT-5.4 fallback) with high reasoning effort
    4. Compare with existing signals — flag if strong disagreement
    5. Update signal in DB + Redis
    6. Notify via Telegram if significant change
    """
    
    TELEGRAM_CHAT_ID = "7916400037"
    
    def __init__(
        self,
        db: Database,
        registry: ProviderRegistry,
        queue: EscalationQueue,
        publisher: SignalPublisher
    ):
        """
        Initialize offline worker
        
        Args:
            db: Database instance
            registry: Provider registry
            queue: Escalation queue
            publisher: Signal publisher
        """
        self.db = db
        self.registry = registry
        self.queue = queue
        self.publisher = publisher
        
        # Rate limiting
        self.processed_this_hour = 0
        self.hour_start = datetime.utcnow()
        
    async def _get_adjudication_provider(self):
        """
        Get provider for adjudication (GPT-5.4-pro or GPT-5.4 fallback)
        
        Returns:
            Provider instance and model name used
        """
        # Try GPT-5.4-pro first
        provider = await self.registry.get("gpt54pro")
        if provider:
            log.info("using_gpt54pro_for_adjudication")
            return provider, "gpt-5.4-pro"
            
        # Fallback to GPT-5.4
        provider = await self.registry.get("gpt54")
        if provider:
            log.warning("gpt54pro_unavailable_using_gpt54_fallback")
            return provider, "gpt-5.4"
            
        # No providers available
        log.error("no_adjudication_provider_available")
        return None, None
        
    async def _gather_context(self, condition_id: str) -> dict:
        """
        Gather all context for a market
        
        Args:
            condition_id: Market condition ID
            
        Returns:
            Dict with {question, rules, evidence_items, previous_estimates, current_mid}
        """
        # Get market metadata (mock for now — would fetch from Polymarket API)
        # In production, this would query the markets table
        question = f"Market {condition_id}"
        rules = "Standard resolution rules apply"
        current_mid = 0.5
        
        # Get evidence items
        evidence_query = """
            SELECT evidence_id, condition_id, polarity, claim, reliability, ts_observed
            FROM evidence_items
            WHERE condition_id = $1
            ORDER BY ts_observed DESC
            LIMIT 50
        """
        evidence_rows = await self.db.fetch(evidence_query, condition_id)
        evidence_items = [
            {
                "evidence_id": row["evidence_id"],
                "polarity": row["polarity"],
                "claim": row["claim"],
                "reliability": row["reliability"],
            }
            for row in evidence_rows
        ]
        
        # Get previous route signals
        signals_query = """
            SELECT route, p_calibrated, uncertainty
            FROM fair_value_signals
            WHERE condition_id = $1
            ORDER BY generated_at DESC
            LIMIT 10
        """
        signal_rows = await self.db.fetch(signals_query, condition_id)
        previous_estimates = [
            {
                "route": row["route"],
                "p_hat": row["p_calibrated"],
                "uncertainty": row["uncertainty"],
            }
            for row in signal_rows
        ]
        
        return {
            "question": question,
            "rules": rules,
            "evidence_items": evidence_items,
            "previous_estimates": previous_estimates,
            "current_mid": current_mid,
        }
        
    async def process_one(self, condition_id: str, metadata: dict) -> dict:
        """
        Process a single escalated market.
        
        Args:
            condition_id: Market condition ID
            metadata: Escalation metadata
            
        Returns:
            {
                condition_id, model_used, p_hat, uncertainty,
                previous_p, delta, processing_time_s,
                action: "updated" | "confirmed" | "disagreed"
            }
        """
        start_time = datetime.utcnow()
        
        log.info(
            "processing_escalated_market",
            condition_id=condition_id,
            reason=metadata.get("reason"),
            priority=metadata.get("priority")
        )
        
        # Get provider
        provider, model_used = await self._get_adjudication_provider()
        if not provider:
            log.error("cannot_process_no_provider", condition_id=condition_id)
            return {
                "condition_id": condition_id,
                "model_used": None,
                "action": "error",
                "error": "No provider available"
            }
            
        # Gather context
        context = await self._gather_context(condition_id)
        
        # Build prompt
        user_prompt = build_adjudication_prompt(
            question=context["question"],
            rules=context["rules"],
            evidence_items=context["evidence_items"],
            previous_estimates=context["previous_estimates"],
            current_mid=context["current_mid"]
        )
        
        # Call provider with high reasoning effort
        try:
            messages = [
                {"role": "system", "content": OFFLINE_ADJUDICATION_SYSTEM},
                {"role": "user", "content": user_prompt}
            ]
            
            response = await provider.complete(
                messages=messages,
                response_format={"type": "json_object"},
                reasoning_effort="high",  # High reasoning for adjudication
            )
            
            # Parse JSON response
            result = json.loads(response.text)
            
            p_hat = result["p_hat"]
            uncertainty = result["uncertainty"]
            evidence_ids = result["evidence_ids"]
            reasoning = result.get("reasoning_summary", "")
            avoid_market = result.get("avoid_market", False)
            
        except Exception as e:
            log.error("adjudication_failed", condition_id=condition_id, error=str(e))
            return {
                "condition_id": condition_id,
                "model_used": model_used,
                "action": "error",
                "error": str(e)
            }
            
        # Get previous signal for comparison
        previous_signal = await self.publisher.get_latest(condition_id)
        previous_p = previous_signal.p_calibrated if previous_signal else 0.5
        delta = abs(p_hat - previous_p)
        
        # Determine action
        if delta > 0.15:
            action = "disagreed"
        elif delta > 0.05:
            action = "updated"
        else:
            action = "confirmed"
            
        # Create new signal
        new_signal = FairValueSignal(
            condition_id=condition_id,
            p_calibrated=p_hat,
            p_low=max(0.0, p_hat - uncertainty),
            p_high=min(1.0, p_hat + uncertainty),
            uncertainty=uncertainty,
            hurdle_met=delta > 0.05,  # Met hurdle if significant change
            route="offline",
            evidence_ids=evidence_ids,
            counterevidence_ids=[],
            models_used=[model_used],
        )
        
        # Publish signal
        await self.publisher.publish(new_signal)
        
        processing_time = (datetime.utcnow() - start_time).total_seconds()
        
        result_dict = {
            "condition_id": condition_id,
            "model_used": model_used,
            "p_hat": p_hat,
            "uncertainty": uncertainty,
            "previous_p": previous_p,
            "delta": delta,
            "processing_time_s": processing_time,
            "action": action,
            "avoid_market": avoid_market,
        }
        
        log.info(
            "market_processed",
            **result_dict
        )
        
        # Notify if significant disagreement
        if action == "disagreed":
            await self._notify_disagreement(condition_id, previous_p, p_hat, reasoning)
            
        return result_dict
        
    async def _notify_disagreement(
        self,
        condition_id: str,
        previous_p: float,
        new_p: float,
        reasoning: str
    ) -> None:
        """
        Send Telegram notification for significant disagreement
        
        Args:
            condition_id: Market condition ID
            previous_p: Previous probability estimate
            new_p: New probability estimate
            reasoning: Reasoning summary
        """
        delta = new_p - previous_p
        direction = "↑" if delta > 0 else "↓"
        
        message = f"""🚨 V3 Offline Worker — Significant Disagreement

Market: {condition_id}
Previous: {previous_p:.2%}
New: {new_p:.2%} {direction}
Delta: {abs(delta):.2%}

Reasoning:
{reasoning[:300]}..."""

        try:
            # Import message tool dynamically to avoid circular imports
            # In production, would use proper notification system
            log.info(
                "would_send_telegram_notification",
                chat_id=self.TELEGRAM_CHAT_ID,
                message=message
            )
        except Exception as e:
            log.error("notification_failed", error=str(e))
            
    async def run_loop(
        self,
        poll_interval: int = 60,
        max_per_hour: int = 20
    ) -> None:
        """
        Main worker loop.
        
        Polls queue every poll_interval seconds.
        Respects rate limit (max_per_hour).
        Logs everything.
        
        Args:
            poll_interval: Seconds between queue polls
            max_per_hour: Maximum markets to process per hour
        """
        log.info(
            "offline_worker_started",
            poll_interval=poll_interval,
            max_per_hour=max_per_hour
        )
        
        while True:
            try:
                # Check rate limit
                now = datetime.utcnow()
                if (now - self.hour_start).total_seconds() >= 3600:
                    # New hour — reset counter
                    self.hour_start = now
                    self.processed_this_hour = 0
                    log.info("rate_limit_reset", processed_last_hour=self.processed_this_hour)
                    
                if self.processed_this_hour >= max_per_hour:
                    log.warning(
                        "rate_limit_reached",
                        processed=self.processed_this_hour,
                        max=max_per_hour
                    )
                    await asyncio.sleep(poll_interval)
                    continue
                    
                # Check queue
                queue_size = await self.queue.size()
                if queue_size == 0:
                    log.debug("queue_empty", waiting_seconds=poll_interval)
                    await asyncio.sleep(poll_interval)
                    continue
                    
                log.info("queue_not_empty", size=queue_size)
                
                # Dequeue and process
                item = await self.queue.dequeue()
                if item:
                    condition_id, metadata = item
                    result = await self.process_one(condition_id, metadata)
                    
                    if result.get("action") != "error":
                        self.processed_this_hour += 1
                        
                await asyncio.sleep(poll_interval)
                
            except Exception as e:
                log.error("worker_loop_error", error=str(e), exc_info=True)
                await asyncio.sleep(poll_interval)

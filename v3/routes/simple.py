"""
V3 Simple Route
Clear YES/NO markets with single verifiable events
"""

import json
import re
from datetime import datetime
from typing import List
import structlog

from v3.providers.registry import ProviderRegistry
from v3.evidence.graph import EvidenceGraph
from v3.evidence.entities import (
    EvidenceItem,
    BlindEstimate,
    MarketAwareDecision,
    FairValueSignal,
)
from v3.intake.schemas import MarketMeta
from v3.routes.prompts import (
    SIMPLE_BLIND_SYSTEM,
    build_simple_blind_prompt,
    SIMPLE_JUDGE_SYSTEM,
    build_simple_judge_prompt,
)

log = structlog.get_logger()


class SimpleRoute:
    """Simple markets: clear YES/NO, single verifiable event"""
    
    def __init__(self, registry: ProviderRegistry, evidence_graph: EvidenceGraph):
        """
        Initialize simple route
        
        Args:
            registry: Provider registry instance
            evidence_graph: Evidence graph instance
        """
        self.registry = registry
        self.evidence_graph = evidence_graph
    
    async def blind_pass(self,
                        condition_id: str,
                        evidence_bundle: List[EvidenceItem],
                        rule_text: str,
                        clarifications: List[str] = []) -> BlindEstimate:
        """
        Sonnet 4.6 blind probability estimate.
        
        CRITICAL: The model NEVER sees current market price in this pass.
        This prevents anchoring bias.
        
        Args:
            condition_id: Market condition ID
            evidence_bundle: List of evidence items
            rule_text: Resolution rules text
            clarifications: List of clarification texts
            
        Returns:
            BlindEstimate with p_hat, uncertainty, evidence_ids, reasoning
        """
        log.info("simple_blind_pass_start", condition_id=condition_id, evidence_count=len(evidence_bundle))
        
        # Get Sonnet provider
        sonnet = await self.registry.get("sonnet")
        
        # Convert evidence items to dicts for prompt
        evidence_dicts = [
            {
                "evidence_id": item.evidence_id,
                "claim": item.claim,
                "polarity": item.polarity,
                "reliability": item.reliability,
            }
            for item in evidence_bundle
        ]
        
        # Build prompt (question from evidence context or fallback)
        question = f"Market condition {condition_id}"
        user_prompt = build_simple_blind_prompt(
            question=question,
            rules=rule_text,
            evidence=evidence_dicts,
            clarifications=clarifications
        )
        
        # Call Sonnet
        messages = [
            {"role": "system", "content": SIMPLE_BLIND_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]
        
        response = await sonnet.complete(
            messages=messages,
            reasoning_effort="medium",
        )
        
        log.info("simple_blind_pass_response",
                condition_id=condition_id,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_ms=response.latency_ms)
        
        # Parse JSON response
        parsed = self._parse_json_response(response.text)
        
        # Build BlindEstimate
        blind = BlindEstimate(
            p_hat=parsed.get("p_hat", 0.5),
            uncertainty=parsed.get("uncertainty", 0.2),
            evidence_ids=parsed.get("evidence_ids", []),
            model="claude-sonnet-4-6",
            reasoning_summary=parsed.get("reasoning_summary", ""),
        )
        
        log.info("simple_blind_pass_complete",
                condition_id=condition_id,
                p_hat=blind.p_hat,
                uncertainty=blind.uncertainty)
        
        return blind
    
    async def market_aware_pass(self,
                               condition_id: str,
                               blind: BlindEstimate,
                               current_mid: float,
                               volume_24h: float,
                               spread: float) -> MarketAwareDecision:
        """
        GPT-5.4 market-aware decision.
        
        Sees: blind estimate + current market state.
        Decides: Is the edge real? Is it worth trading?
        
        Args:
            condition_id: Market condition ID
            blind: Blind estimate from first pass
            current_mid: Current market midpoint
            volume_24h: 24-hour volume
            spread: Current spread
            
        Returns:
            MarketAwareDecision with action, p_adjusted, edge_cents, reasoning
        """
        log.info("simple_judge_pass_start", condition_id=condition_id)
        
        # Get GPT-5.4 provider
        gpt54 = await self.registry.get("gpt54")
        
        # Build prompt
        blind_dict = {
            "p_hat": blind.p_hat,
            "uncertainty": blind.uncertainty,
            "reasoning_summary": blind.reasoning_summary or "",
        }
        
        market_state = {
            "current_mid": current_mid,
            "volume_24h": volume_24h,
            "spread": spread,
        }
        
        user_prompt = build_simple_judge_prompt(
            blind=blind_dict,
            market_state=market_state
        )
        
        # Call GPT-5.4
        messages = [
            {"role": "system", "content": SIMPLE_JUDGE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]
        
        response = await gpt54.complete(
            messages=messages,
            reasoning_effort="low",
        )
        
        log.info("simple_judge_pass_response",
                condition_id=condition_id,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_ms=response.latency_ms)
        
        # Parse JSON response
        parsed = self._parse_json_response(response.text)
        
        # Build MarketAwareDecision
        decision = MarketAwareDecision(
            blind_estimate=blind,
            current_mid=current_mid,
            edge_cents=parsed.get("edge_cents", 0.0),
            hurdle_cents=parsed.get("hurdle_cents", 5.0),
            action=parsed.get("action", "WAIT"),
        )
        
        log.info("simple_judge_pass_complete",
                condition_id=condition_id,
                action=decision.action,
                edge_cents=decision.edge_cents,
                hurdle_cents=decision.hurdle_cents)
        
        return decision
    
    async def execute(self,
                     condition_id: str,
                     market: MarketMeta,
                     evidence_bundle: List[EvidenceItem],
                     rule_text: str,
                     clarifications: List[str] = []) -> FairValueSignal:
        """
        End-to-end simple route: blind → judge → signal
        
        Args:
            condition_id: Market condition ID
            market: Market metadata
            evidence_bundle: List of evidence items
            rule_text: Resolution rules
            clarifications: List of clarification texts
            
        Returns:
            FairValueSignal with calibrated probability and metadata
        """
        log.info("simple_route_execute_start", condition_id=condition_id)
        
        # Pass 1: Blind estimate
        blind = await self.blind_pass(
            condition_id=condition_id,
            evidence_bundle=evidence_bundle,
            rule_text=rule_text,
            clarifications=clarifications
        )
        
        # Pass 2: Market-aware judge
        decision = await self.market_aware_pass(
            condition_id=condition_id,
            blind=blind,
            current_mid=market.current_mid,
            volume_24h=market.volume_24h,
            spread=0.02,  # Default spread; would come from market data in production
        )
        
        # Build FairValueSignal
        signal = FairValueSignal(
            condition_id=condition_id,
            generated_at=datetime.utcnow(),
            p_calibrated=blind.p_hat,
            p_low=max(0.0, blind.p_hat - blind.uncertainty),
            p_high=min(1.0, blind.p_hat + blind.uncertainty),
            uncertainty=blind.uncertainty,
            skew_cents=None,
            hurdle_cents=decision.hurdle_cents,
            hurdle_met=(decision.action == "TRADE"),
            route="simple",
            evidence_ids=blind.evidence_ids,
            counterevidence_ids=[],
            models_used=["claude-sonnet-4-6", "gpt-5.4-pro"],
            expires_at=datetime.utcnow() + __import__('datetime').timedelta(minutes=15),
        )
        
        log.info("simple_route_execute_complete",
                condition_id=condition_id,
                p_calibrated=signal.p_calibrated,
                action=decision.action)
        
        return signal
    
    def _parse_json_response(self, text: str) -> dict:
        """
        Parse JSON from model response, handling markdown code blocks and extra text
        
        Args:
            text: Raw model response
            
        Returns:
            Parsed JSON dict
        """
        # Try to extract JSON from markdown code block
        code_block_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if code_block_match:
            text = code_block_match.group(1)
        
        # Try to find JSON object in text
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            text = json_match.group(0)
        
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            log.error("json_parse_failed", text=text[:200], error=str(e))
            # Return safe defaults
            return {
                "p_hat": 0.5,
                "uncertainty": 0.2,
                "evidence_ids": [],
                "reasoning_summary": "Failed to parse model response",
                "action": "WAIT",
                "p_adjusted": 0.5,
                "edge_cents": 0.0,
                "hurdle_cents": 5.0,
                "reasoning": "Failed to parse model response"
            }

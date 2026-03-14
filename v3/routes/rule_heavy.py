"""
V3 Rule-Heavy Route
Ambiguous/disputed markets with complex resolution rules
"""

import json
import re
from datetime import datetime, timedelta

import structlog

from v3.evidence.entities import (
    BlindEstimate,
    EvidenceItem,
    FairValueSignal,
    MarketAwareDecision,
)
from v3.evidence.graph import EvidenceGraph
from v3.intake.schemas import MarketMeta
from v3.providers.registry import ProviderRegistry
from v3.routes.prompts.rule_heavy_v1 import (
    RULE_HEAVY_SYSTEM,
    build_rule_heavy_prompt,
)
from v3.routes.prompts.rule_judge_v1 import (
    RULE_JUDGE_SYSTEM,
    build_rule_judge_prompt,
)

log = structlog.get_logger()


class RuleHeavyRoute:
    """Rule-heavy markets: ambiguous rules, high dispute risk, legal complexity"""

    def __init__(self, registry: ProviderRegistry, evidence_graph: EvidenceGraph):
        """
        Initialize rule-heavy route

        Args:
            registry: Provider registry instance
            evidence_graph: Evidence graph instance
        """
        self.registry = registry
        self.evidence_graph = evidence_graph

    async def opus_rule_pass(self,
                            condition_id: str,
                            rule_text: str,
                            clarifications: list[str],
                            evidence: list[EvidenceItem]) -> BlindEstimate:
        """
        Opus 4.6 deep rule analysis for ambiguous/disputed markets.

        Opus acts as a legal analyst — focuses on:
        - Rule ambiguity ("notwithstanding", "unless", "at the sole discretion of")
        - Edge cases in the rules (what happens if X but also Y?)
        - Dispute risk (how likely is UMA dispute?)
        - Clarification interpretation (do clarifications change the outcome?)
        - Historical precedent (similar markets that resolved controversially)

        CRITICAL: Model NEVER sees current market price (blind pass).

        Prompt output includes extra fields:
        - dispute_risk: float [0, 1] — probability of UMA dispute
        - rule_clarity: float [0, 1] — how clear/ambiguous the rules are
        - edge_cases: list[str] — identified edge cases

        Args:
            condition_id: Market condition ID
            rule_text: Resolution rules text
            clarifications: List of clarification texts
            evidence: List of evidence items

        Returns:
            BlindEstimate with extended fields (dispute_risk, rule_clarity, edge_cases)
        """
        log.info("opus_rule_pass_start",
                condition_id=condition_id,
                evidence_count=len(evidence),
                clarifications_count=len(clarifications))

        # Get Opus provider
        opus = await self.registry.get("opus")

        # Convert evidence items to dicts for prompt
        evidence_dicts = [
            {
                "evidence_id": item.evidence_id,
                "claim": item.claim,
                "polarity": item.polarity,
                "reliability": item.reliability,
            }
            for item in evidence
        ]

        # Build prompt (question from evidence context or fallback)
        question = f"Market condition {condition_id}"
        user_prompt = build_rule_heavy_prompt(
            question=question,
            rules=rule_text,
            evidence=evidence_dicts,
            clarifications=clarifications
        )

        # Call Opus with high reasoning effort (this is complex legal analysis)
        messages = [
            {"role": "system", "content": RULE_HEAVY_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]

        response = await opus.complete(
            messages=messages,
            reasoning_effort="high",  # Maximum reasoning for rule analysis
        )

        log.info("opus_rule_pass_response",
                condition_id=condition_id,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                latency_ms=response.latency_ms)

        # Parse JSON response
        parsed = self._parse_json_response(response.text)

        # Build BlindEstimate with extended fields
        blind = BlindEstimate(
            p_hat=parsed.get("p_hat", 0.5),
            uncertainty=parsed.get("uncertainty", 0.2),
            evidence_ids=parsed.get("evidence_ids", []),
            model="claude-opus-4-6",
            reasoning_summary=parsed.get("reasoning_summary", ""),
        )

        # Store extended fields in reasoning_summary as JSON for now
        # (In production, you might extend BlindEstimate model)
        extended_data = {
            "dispute_risk": parsed.get("dispute_risk", 0.0),
            "rule_clarity": parsed.get("rule_clarity", 1.0),
            "edge_cases": parsed.get("edge_cases", []),
            "base_reasoning": parsed.get("reasoning_summary", ""),
        }

        log.info("opus_rule_pass_complete",
                condition_id=condition_id,
                p_hat=blind.p_hat,
                uncertainty=blind.uncertainty,
                dispute_risk=extended_data["dispute_risk"],
                rule_clarity=extended_data["rule_clarity"],
                edge_cases_count=len(extended_data["edge_cases"]))

        # Attach extended data to blind estimate
        # Store in reasoning_summary as structured data
        blind.reasoning_summary = json.dumps(extended_data)

        return blind

    async def judge_pass(self,
                        condition_id: str,
                        blind: BlindEstimate,
                        current_mid: float,
                        volume_24h: float,
                        spread: float) -> MarketAwareDecision:
        """
        GPT-5.4 market-aware judge — rule-heavy variant.

        Extra considerations vs simple judge:
        - Factor in dispute_risk (high dispute = wider hurdle)
        - Factor in rule_clarity (low clarity = wider hurdle)
        - Hurdle formula: h = 0.03 + spread/2 + (0.03 * dispute_risk) + (0.02 * (1 - rule_clarity))

        Args:
            condition_id: Market condition ID
            blind: Blind estimate from Opus rule pass
            current_mid: Current market midpoint
            volume_24h: 24-hour volume
            spread: Current spread

        Returns:
            MarketAwareDecision with action, p_adjusted, edge_cents, reasoning
        """
        log.info("rule_judge_pass_start", condition_id=condition_id)

        # Get GPT-5.4 provider
        gpt54 = await self.registry.get("gpt54")

        # Extract extended fields from blind estimate
        try:
            extended_data = json.loads(blind.reasoning_summary or "{}")
            dispute_risk = extended_data.get("dispute_risk", 0.0)
            rule_clarity = extended_data.get("rule_clarity", 1.0)
            base_reasoning = extended_data.get("base_reasoning", "")
        except (json.JSONDecodeError, TypeError):
            dispute_risk = 0.0
            rule_clarity = 1.0
            base_reasoning = blind.reasoning_summary or ""

        # Build prompt
        blind_dict = {
            "p_hat": blind.p_hat,
            "uncertainty": blind.uncertainty,
            "reasoning_summary": base_reasoning,
            "dispute_risk": dispute_risk,
            "rule_clarity": rule_clarity,
        }

        market_state = {
            "current_mid": current_mid,
            "volume_24h": volume_24h,
            "spread": spread,
        }

        user_prompt = build_rule_judge_prompt(
            blind=blind_dict,
            market_state=market_state
        )

        # Call GPT-5.4
        messages = [
            {"role": "system", "content": RULE_JUDGE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]

        response = await gpt54.complete(
            messages=messages,
            reasoning_effort="medium",  # Medium reasoning for judge
        )

        log.info("rule_judge_pass_response",
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

        log.info("rule_judge_pass_complete",
                condition_id=condition_id,
                action=decision.action,
                edge_cents=decision.edge_cents,
                hurdle_cents=decision.hurdle_cents,
                dispute_risk=dispute_risk,
                rule_clarity=rule_clarity)

        return decision

    def should_escalate_async(self,
                              blind: BlindEstimate,
                              market_notional: float = 0.0) -> bool:
        """
        Decides if this market needs GPT-5.4-pro async adjudication.

        Escalate if ANY of:
        - dispute_risk > 0.3
        - rule_clarity < 0.5
        - market_notional > $50k
        - uncertainty > 0.25

        Note: GPT-5.4-pro is not yet available via Codex endpoint.
        This method is for future use — for now, it just flags and logs.

        Args:
            blind: Blind estimate from Opus rule pass
            market_notional: Market notional value in USD

        Returns:
            True if escalation recommended, False otherwise
        """
        # Extract extended fields from blind estimate
        try:
            extended_data = json.loads(blind.reasoning_summary or "{}")
            dispute_risk = extended_data.get("dispute_risk", 0.0)
            rule_clarity = extended_data.get("rule_clarity", 1.0)
        except (json.JSONDecodeError, TypeError):
            dispute_risk = 0.0
            rule_clarity = 1.0

        # Check escalation criteria
        should_escalate = (
            dispute_risk > 0.3 or
            rule_clarity < 0.5 or
            market_notional > 50000.0 or
            blind.uncertainty > 0.25
        )

        if should_escalate:
            log.warning("escalation_recommended",
                       dispute_risk=dispute_risk,
                       rule_clarity=rule_clarity,
                       market_notional=market_notional,
                       uncertainty=blind.uncertainty)
        else:
            log.info("no_escalation_needed",
                    dispute_risk=dispute_risk,
                    rule_clarity=rule_clarity,
                    market_notional=market_notional,
                    uncertainty=blind.uncertainty)

        return should_escalate

    async def execute(self,
                     condition_id: str,
                     market: MarketMeta,
                     evidence_bundle: list[EvidenceItem],
                     rule_text: str,
                     clarifications: list[str] = []) -> FairValueSignal:
        """
        End-to-end rule-heavy route: Opus blind → GPT-5.4 judge → signal

        Args:
            condition_id: Market condition ID
            market: Market metadata
            evidence_bundle: List of evidence items
            rule_text: Resolution rules
            clarifications: List of clarification texts

        Returns:
            FairValueSignal with calibrated probability and metadata
        """
        log.info("rule_heavy_route_execute_start", condition_id=condition_id)

        # Pass 1: Opus rule analysis (blind)
        blind = await self.opus_rule_pass(
            condition_id=condition_id,
            rule_text=rule_text,
            clarifications=clarifications,
            evidence=evidence_bundle
        )

        # Check if escalation is needed
        escalate = self.should_escalate_async(
            blind=blind,
            market_notional=market.volume_24h  # Use 24h volume as proxy for notional
        )

        # Pass 2: GPT-5.4 judge (market-aware)
        decision = await self.judge_pass(
            condition_id=condition_id,
            blind=blind,
            current_mid=market.current_mid,
            volume_24h=market.volume_24h,
            spread=0.02,  # Default spread; would come from market data in production
        )

        # Extract extended fields for signal metadata
        try:
            extended_data = json.loads(blind.reasoning_summary or "{}")
            edge_cases = extended_data.get("edge_cases", [])
        except (json.JSONDecodeError, TypeError):
            edge_cases = []

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
            route="rule",
            evidence_ids=blind.evidence_ids,
            counterevidence_ids=[],
            models_used=["claude-opus-4-6", "gpt-5.4-pro"],
            expires_at=datetime.utcnow() + timedelta(minutes=20),  # Longer expiry for rule-heavy
        )

        log.info("rule_heavy_route_execute_complete",
                condition_id=condition_id,
                p_calibrated=signal.p_calibrated,
                action=decision.action,
                escalate=escalate,
                edge_cases_count=len(edge_cases))

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
                "dispute_risk": 0.0,
                "rule_clarity": 1.0,
                "edge_cases": [],
                "action": "WAIT",
                "p_adjusted": 0.5,
                "edge_cents": 0.0,
                "hurdle_cents": 5.0,
                "reasoning": "Failed to parse model response"
            }

"""
V3 Dossier Route
Document-heavy markets requiring multi-source synthesis
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
    SourceDocument,
)
from v3.evidence.graph import EvidenceGraph
from v3.intake.schemas import MarketMeta
from v3.providers.registry import ProviderRegistry
from v3.routes.prompts import (
    DOSSIER_CHALLENGE_SYSTEM,
    DOSSIER_SYSTEM,
    build_dossier_challenge_prompt,
    build_dossier_synthesis_prompt,
)

log = structlog.get_logger()


class DossierRoute:
    """Document-heavy markets requiring multi-source synthesis"""

    def __init__(self, registry: ProviderRegistry, evidence_graph: EvidenceGraph):
        """
        Initialize dossier route

        Args:
            registry: Provider registry instance
            evidence_graph: Evidence graph instance
        """
        self.registry = registry
        self.evidence_graph = evidence_graph

    async def gemini_synthesis(self,
                              condition_id: str,
                              documents: list[SourceDocument],
                              evidence: list[EvidenceItem],
                              rule_text: str,
                              clarifications: list[str] = []) -> BlindEstimate:
        """
        Gemini long-context dossier synthesis.

        Gemini's strength: process large volumes of evidence (up to 1M tokens).

        Process:
        1. Compile all evidence into a structured dossier
        2. Include document summaries, key claims, dates, contradictions
        3. Ask Gemini to synthesize and estimate probability
        4. Detect contradictions across sources

        CRITICAL: Model NEVER sees current market price (blind pass).

        If Gemini unavailable (rate-limited): fall back to Sonnet with truncated evidence.

        Output includes extra fields:
        - contradictions: list[str] — detected contradictions in evidence
        - source_quality: float [0, 1] — overall evidence quality assessment
        - key_documents: list[str] — most important doc_ids

        Args:
            condition_id: Market condition ID
            documents: List of source documents
            evidence: List of evidence items
            rule_text: Resolution rules text
            clarifications: List of clarification texts

        Returns:
            BlindEstimate with extended metadata
        """
        log.info("dossier_synthesis_start",
                condition_id=condition_id,
                doc_count=len(documents),
                evidence_count=len(evidence))

        # Try Gemini first
        try:
            gemini = await self.registry.get("gemini")

            # Check if Gemini is available (unavailable providers are removed during initialization)
            if not gemini:
                log.warning("gemini_unavailable_fallback_to_sonnet", condition_id=condition_id)
                return await self._synthesis_fallback_sonnet(
                    condition_id, documents, evidence, rule_text, clarifications
                )

            # Build dossier prompt
            question = f"Condition {condition_id}"  # Will be enhanced with actual question

            # Convert documents to dicts
            doc_dicts = []
            for doc in documents:
                doc_dict = {
                    "doc_id": doc.doc_id,
                    "title": doc.title or "Untitled",
                    "source_type": doc.source_type,
                    "publisher": doc.publisher or "Unknown",
                    # TODO: Load from text_path
                    "content_summary": (
                        "(Full content would be loaded here)"
                    )
                }
                doc_dicts.append(doc_dict)

            # Convert evidence to dicts
            evidence_dicts = []
            for item in evidence:
                evidence_dicts.append({
                    "evidence_id": item.evidence_id,
                    "claim": item.claim,
                    "polarity": item.polarity,
                    "reliability": item.reliability,
                    "doc_id": item.doc_id or "unknown",
                })

            prompt = build_dossier_synthesis_prompt(
                question=question,
                rules=rule_text,
                documents=doc_dicts,
                evidence=evidence_dicts,
                clarifications=clarifications,
            )

            # Call Gemini
            messages = [
                {"role": "system", "content": DOSSIER_SYSTEM},
                {"role": "user", "content": prompt},
            ]
            response = await gemini.complete(
                messages=messages,
                reasoning_effort="medium",
            )

            log.info("gemini_synthesis_complete",
                    condition_id=condition_id,
                    latency_ms=response.latency_ms,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens)

            # Parse JSON response
            synthesis_data = self._parse_json_response(response.text)

            # Create BlindEstimate
            # Extended metadata (contradictions, source_quality,
            # key_documents) is captured in reasoning_summary
            reasoning_parts = [synthesis_data.get("reasoning_summary", "")]

            contradictions = synthesis_data.get("contradictions", [])
            if contradictions:
                reasoning_parts.append(f"Contradictions: {'; '.join(contradictions[:3])}")

            source_quality = synthesis_data.get("source_quality", 0.5)
            reasoning_parts.append(f"Source quality: {source_quality:.2f}")

            estimate = BlindEstimate(
                p_hat=synthesis_data.get("p_hat", 0.5),
                uncertainty=synthesis_data.get("uncertainty", 0.3),
                evidence_ids=synthesis_data.get("evidence_ids", []),
                model="gemini-3-pro-preview",
                reasoning_summary=" | ".join(reasoning_parts),
            )

            return estimate

        except Exception as e:
            log.warning("gemini_synthesis_failed_fallback",
                       condition_id=condition_id,
                       error=str(e))
            return await self._synthesis_fallback_sonnet(
                condition_id, documents, evidence, rule_text, clarifications
            )

    async def _synthesis_fallback_sonnet(self,
                                        condition_id: str,
                                        documents: list[SourceDocument],
                                        evidence: list[EvidenceItem],
                                        rule_text: str,
                                        clarifications: list[str]) -> BlindEstimate:
        """
        Fallback to Sonnet when Gemini is unavailable.
        Truncate evidence to fit Sonnet's smaller context.

        Args:
            condition_id: Market condition ID
            documents: List of source documents (will be truncated)
            evidence: List of evidence items (will be truncated)
            rule_text: Resolution rules
            clarifications: Clarifications

        Returns:
            BlindEstimate from Sonnet
        """
        log.info("dossier_fallback_sonnet", condition_id=condition_id)

        sonnet = await self.registry.get("sonnet")

        # Truncate to top 10 evidence items by reliability
        truncated_evidence = sorted(evidence, key=lambda e: e.reliability, reverse=True)[:10]

        # Simple prompt for Sonnet (no full documents, just evidence)
        question = f"Condition {condition_id}"

        evidence_dicts = []
        for item in truncated_evidence:
            evidence_dicts.append({
                "evidence_id": item.evidence_id,
                "claim": item.claim,
                "polarity": item.polarity,
                "reliability": item.reliability,
                "doc_id": item.doc_id or "unknown",
            })

        # Use simpler synthesis prompt for Sonnet
        prompt = build_dossier_synthesis_prompt(
            question=question,
            rules=rule_text,
            documents=[],  # Skip documents for Sonnet
            evidence=evidence_dicts,
            clarifications=clarifications,
        )

        messages = [
            {"role": "system", "content": DOSSIER_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        response = await sonnet.complete(
            messages=messages,
        )

        log.info("sonnet_fallback_complete",
                condition_id=condition_id,
                latency_ms=response.latency_ms)

        synthesis_data = self._parse_json_response(response.text)

        # Extended metadata captured in reasoning_summary
        reasoning_parts = [synthesis_data.get("reasoning_summary", "")]

        contradictions = synthesis_data.get("contradictions", [])
        if contradictions:
            reasoning_parts.append(f"Contradictions: {'; '.join(contradictions[:3])}")

        source_quality = synthesis_data.get("source_quality", 0.5)
        reasoning_parts.append(f"Source quality: {source_quality:.2f}")

        estimate = BlindEstimate(
            p_hat=synthesis_data.get("p_hat", 0.5),
            uncertainty=synthesis_data.get("uncertainty", 0.3),
            evidence_ids=synthesis_data.get("evidence_ids", []),
            model="claude-sonnet-4-6",
            reasoning_summary=" | ".join(reasoning_parts),
        )

        return estimate

    async def opus_challenge(self,
                           condition_id: str,
                           synthesis_estimate: BlindEstimate,
                           evidence: list[EvidenceItem],
                           rule_text: str) -> BlindEstimate:
        """
        Opus adversarial challenge to the dossier synthesis.

        Opus sees Gemini's estimate and evidence, then tries to:
        - Find counterevidence that Gemini may have underweighted
        - Flag overconfidence (is uncertainty too narrow?)
        - Identify dispute risk from rule ambiguity
        - Challenge the reasoning (steel-man the opposite position)

        Output: Independent BlindEstimate (may disagree significantly)

        Args:
            condition_id: Market condition ID
            synthesis_estimate: Estimate from Gemini synthesis
            evidence: List of evidence items
            rule_text: Resolution rules

        Returns:
            Independent BlindEstimate from Opus
        """
        log.info("opus_challenge_start",
                condition_id=condition_id,
                synthesis_p=synthesis_estimate.p_hat)

        opus = await self.registry.get("opus")

        # Build challenge prompt
        question = f"Condition {condition_id}"

        # Convert synthesis estimate to dict
        # Extract contradictions and source_quality from reasoning_summary if present
        reasoning = synthesis_estimate.reasoning_summary or ""
        contradictions = []
        source_quality = 0.5

        if "Contradictions:" in reasoning:
            # Parse contradictions from reasoning summary
            parts = reasoning.split(" | ")
            for part in parts:
                if part.startswith("Contradictions:"):
                    contradictions_str = part.replace("Contradictions:", "").strip()
                    contradictions = [c.strip() for c in contradictions_str.split(";")]

        if "Source quality:" in reasoning:
            # Parse source quality
            parts = reasoning.split(" | ")
            for part in parts:
                if part.startswith("Source quality:"):
                    try:
                        source_quality = float(part.replace("Source quality:", "").strip())
                    except ValueError:
                        pass

        synthesis_dict = {
            "p_hat": synthesis_estimate.p_hat,
            "uncertainty": synthesis_estimate.uncertainty,
            "reasoning_summary": synthesis_estimate.reasoning_summary,
            "contradictions": contradictions,
            "source_quality": source_quality,
        }

        # Convert evidence to dicts
        evidence_dicts = []
        for item in evidence:
            evidence_dicts.append({
                "evidence_id": item.evidence_id,
                "claim": item.claim,
                "polarity": item.polarity,
                "reliability": item.reliability,
                "doc_id": item.doc_id or "unknown",
            })

        prompt = build_dossier_challenge_prompt(
            question=question,
            rules=rule_text,
            synthesis_estimate=synthesis_dict,
            evidence=evidence_dicts,
            clarifications=[],
        )

        messages = [
            {"role": "system", "content": DOSSIER_CHALLENGE_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        response = await opus.complete(
            messages=messages,
        )

        log.info("opus_challenge_complete",
                condition_id=condition_id,
                latency_ms=response.latency_ms)

        # Parse JSON response
        challenge_data = self._parse_json_response(response.text)

        # Create BlindEstimate with challenge metadata in reasoning_summary
        reasoning_parts = [challenge_data.get("reasoning_summary", "")]

        challenge_points = challenge_data.get("challenge_points", [])
        if challenge_points:
            reasoning_parts.append(f"Challenges: {'; '.join(challenge_points[:3])}")

        agreement_level = challenge_data.get("agreement_level", "medium")
        reasoning_parts.append(f"Agreement: {agreement_level}")

        challenge_estimate = BlindEstimate(
            p_hat=challenge_data.get("p_hat", 0.5),
            uncertainty=challenge_data.get("uncertainty", 0.3),
            evidence_ids=challenge_data.get("evidence_ids", []),
            model="claude-opus-4-6",
            reasoning_summary=" | ".join(reasoning_parts),
        )

        return challenge_estimate

    async def resolve_disagreement(self,
                                  condition_id: str,
                                  synthesis: BlindEstimate,
                                  challenge: BlindEstimate,
                                  current_mid: float,
                                  volume_24h: float,
                                  spread: float) -> MarketAwareDecision:
        """
        GPT-5.4 judges disagreement between Gemini synthesis and Opus challenge.

        If |synthesis.p_hat - challenge.p_hat| < 0.05: use average
        If |synthesis.p_hat - challenge.p_hat| >= 0.05: GPT-5.4 adjudicates
        If |diff| >= 0.15 and notional > $50: flag for GPT-5.4-pro async (future)

        Args:
            condition_id: Market condition ID
            synthesis: Gemini synthesis estimate
            challenge: Opus challenge estimate
            current_mid: Current market midpoint
            volume_24h: 24h trading volume
            spread: Current spread

        Returns:
            MarketAwareDecision with final estimate and action
        """
        diff = abs(synthesis.p_hat - challenge.p_hat)

        log.info("resolve_disagreement_start",
                condition_id=condition_id,
                synthesis_p=synthesis.p_hat,
                challenge_p=challenge.p_hat,
                diff=diff)

        # Case 1: Close agreement (< 5%) → simple average
        if diff < 0.05:
            log.info("close_agreement_simple_average", condition_id=condition_id)

            from pmm1.math.ensemble import log_pool
            final_p = log_pool([synthesis.p_hat, challenge.p_hat])
            final_uncertainty = max(synthesis.uncertainty, challenge.uncertainty)

            final_estimate = BlindEstimate(
                p_hat=final_p,
                uncertainty=final_uncertainty,
                evidence_ids=list(set(synthesis.evidence_ids + challenge.evidence_ids)),
                model="average-consensus",
                reasoning_summary=(
                    f"Synthesis and challenge agree within"
                    f" 5% (diff={diff:.3f}). Using average."
                ),
            )

        # Case 2: Moderate disagreement (5-15%) OR significant disagreement → GPT-5.4 judges
        else:
            log.info("disagreement_gpt54_adjudication",
                    condition_id=condition_id,
                    diff=diff)

            gpt54 = await self.registry.get("gpt54")

            # Build adjudication prompt
            judge_prompt = f"""Two analysts estimated the probability for a prediction market.

SYNTHESIS ESTIMATE (Gemini):
- Probability: {synthesis.p_hat:.2f} ± {synthesis.uncertainty:.2f}
- Reasoning: {synthesis.reasoning_summary}
- Model: {synthesis.model}

CHALLENGE ESTIMATE (Opus):
- Probability: {challenge.p_hat:.2f} ± {challenge.uncertainty:.2f}
- Reasoning: {challenge.reasoning_summary}
- Model: {challenge.model}
- Challenge points: {getattr(challenge, 'challenge_points', [])}

DISAGREEMENT: {diff:.3f} ({diff*100:.1f}%)

TASK:
Adjudicate this disagreement. Which estimate is more credible? Or should they be weighted?

Provide your final probability estimate and explain your reasoning.

Output ONLY valid JSON:
{{
  "p_hat": 0.42,
  "uncertainty": 0.18,
  "reasoning_summary": "...",
  "synthesis_weight": 0.6,
  "challenge_weight": 0.4
}}"""

            judge_system = """You are a senior analyst adjudicating \
disagreements between two probability estimates.

Your job:
1. Identify which reasoning is more sound
2. Check for logical errors or overconfidence
3. Determine appropriate weights for each estimate
4. Provide a final calibrated probability

Output ONLY valid JSON."""

            messages = [
                {"role": "system", "content": judge_system},
                {"role": "user", "content": judge_prompt},
            ]
            response = await gpt54.complete(
                messages=messages,
            )

            log.info("gpt54_adjudication_complete",
                    condition_id=condition_id,
                    latency_ms=response.latency_ms)

            judge_data = self._parse_json_response(response.text)

            from pmm1.math.ensemble import log_pool
            final_estimate = BlindEstimate(
                p_hat=judge_data.get("p_hat", log_pool([synthesis.p_hat, challenge.p_hat])),
                uncertainty=judge_data.get("uncertainty", 0.25),
                evidence_ids=list(set(synthesis.evidence_ids + challenge.evidence_ids)),
                model="gpt-5.4-adjudicated",
                reasoning_summary=judge_data.get("reasoning_summary", ""),
            )

        # Calculate edge vs market
        edge_cents = (final_estimate.p_hat - current_mid) * 100
        hurdle_cents = 10.0  # Default hurdle

        # Determine action
        if abs(edge_cents) >= hurdle_cents:
            action = "TRADE"
        else:
            action = "NO_EDGE"

        decision = MarketAwareDecision(
            blind_estimate=final_estimate,
            current_mid=current_mid,
            edge_cents=edge_cents,
            hurdle_cents=hurdle_cents,
            action=action,
        )

        log.info("disagreement_resolved",
                condition_id=condition_id,
                final_p=final_estimate.p_hat,
                edge_cents=edge_cents,
                action=action)

        return decision

    async def execute(self,
                     condition_id: str,
                     market: MarketMeta,
                     documents: list[SourceDocument],
                     evidence: list[EvidenceItem],
                     rule_text: str,
                     clarifications: list[str] = []) -> FairValueSignal:
        """
        End-to-end dossier route: Gemini synthesis → Opus challenge → resolve → signal

        Args:
            condition_id: Market condition ID
            market: Market metadata
            documents: List of source documents
            evidence: List of evidence items
            rule_text: Resolution rules
            clarifications: List of clarifications

        Returns:
            FairValueSignal with final probability and action
        """
        log.info("dossier_route_execute_start",
                condition_id=condition_id,
                doc_count=len(documents),
                evidence_count=len(evidence))

        start_time = datetime.utcnow()

        # Step 1: Gemini synthesis
        synthesis = await self.gemini_synthesis(
            condition_id=condition_id,
            documents=documents,
            evidence=evidence,
            rule_text=rule_text,
            clarifications=clarifications,
        )

        # Step 2: Opus adversarial challenge
        challenge = await self.opus_challenge(
            condition_id=condition_id,
            synthesis_estimate=synthesis,
            evidence=evidence,
            rule_text=rule_text,
        )

        # Step 3: Resolve disagreement
        decision = await self.resolve_disagreement(
            condition_id=condition_id,
            synthesis=synthesis,
            challenge=challenge,
            current_mid=market.current_mid,
            volume_24h=market.volume_24h,
            spread=0.02,  # TODO: Get from market
        )

        end_time = datetime.utcnow()
        latency_ms = int((end_time - start_time).total_seconds() * 1000)

        # Build FairValueSignal
        final_estimate = decision.blind_estimate

        signal = FairValueSignal(
            condition_id=condition_id,
            generated_at=datetime.utcnow(),
            p_calibrated=final_estimate.p_hat,
            p_low=max(0.0, final_estimate.p_hat - final_estimate.uncertainty),
            p_high=min(1.0, final_estimate.p_hat + final_estimate.uncertainty),
            uncertainty=final_estimate.uncertainty,
            skew_cents=decision.edge_cents,
            hurdle_cents=decision.hurdle_cents,
            hurdle_met=(decision.action == "TRADE"),
            route="dossier",
            evidence_ids=final_estimate.evidence_ids,
            counterevidence_ids=[],
            models_used=[synthesis.model, challenge.model, final_estimate.model],
            expires_at=datetime.utcnow() + timedelta(hours=2),
        )

        log.info("dossier_route_complete",
                condition_id=condition_id,
                latency_ms=latency_ms,
                final_p=signal.p_calibrated,
                action=decision.action)

        return signal

    def _parse_json_response(self, text: str) -> dict:
        """
        Parse JSON from LLM response, handling markdown code blocks

        Args:
            text: Raw LLM response text

        Returns:
            Parsed JSON dict
        """
        # Remove markdown code blocks
        text = re.sub(r'```json\s*', '', text)
        text = re.sub(r'```\s*', '', text)
        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            log.error("json_parse_failed", text=text[:200], error=str(e))
            # Return safe defaults
            return {
                "p_hat": 0.5,
                "uncertainty": 0.3,
                "evidence_ids": [],
                "reasoning_summary": f"JSON parse failed: {e}",
            }

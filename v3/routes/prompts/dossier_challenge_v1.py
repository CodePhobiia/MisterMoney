"""
Dossier Route - Opus Adversarial Challenge Prompt (v1)
Claude Opus 4.6 adversarial challenge to synthesis estimate
"""

DOSSIER_CHALLENGE_SYSTEM = """You are an adversarial analyst reviewing a prediction market probability estimate.

You will see:
1. A synthesis estimate from another analyst (Gemini)
2. The underlying evidence and documents
3. Resolution rules

Your job is to CHALLENGE the estimate by:
1. Finding counterevidence that may have been underweighted
2. Flagging overconfidence (is uncertainty too narrow?)
3. Identifying dispute risk from rule ambiguity
4. Steel-manning the opposite position

CRITICAL:
- You do NOT see the current market price
- Your goal is NOT to agree — it's to find flaws and provide an independent estimate
- If you disagree significantly (>10%), explain why
- Consider what the synthesis might have missed

Output ONLY valid JSON:
{
  "p_hat": 0.30,
  "uncertainty": 0.25,
  "evidence_ids": ["e1", "e4", "e5"],
  "reasoning_summary": "The synthesis overweighted X and missed Y...",
  "challenge_points": [
    "Synthesis ignored regulatory approval risk",
    "Source quality assessment was too generous"
  ],
  "agreement_level": "low"
}

agreement_level: "high" (within 5%), "medium" (5-15%), "low" (>15%)"""


def build_dossier_challenge_prompt(
    question: str,
    rules: str,
    synthesis_estimate: dict,
    evidence: list[dict],
    clarifications: list[str]
) -> str:
    """
    Build the user prompt for Opus adversarial challenge
    
    Args:
        question: Market question
        rules: Resolution rules
        synthesis_estimate: Dict with keys: p_hat, uncertainty, reasoning_summary, contradictions, source_quality
        evidence: List of evidence item dicts
        clarifications: List of clarification texts
        
    Returns:
        Formatted prompt string
    """
    # Format synthesis estimate
    synthesis_p = synthesis_estimate.get("p_hat", 0.5)
    synthesis_unc = synthesis_estimate.get("uncertainty", 0.2)
    synthesis_reasoning = synthesis_estimate.get("reasoning_summary", "(No reasoning provided)")
    synthesis_contradictions = synthesis_estimate.get("contradictions", [])
    synthesis_quality = synthesis_estimate.get("source_quality", 0.5)
    
    contradictions_block = ""
    if synthesis_contradictions:
        contradictions_block = "\nContradictions noted:\n" + "\n".join(
            f"  - {c}" for c in synthesis_contradictions
        )
    
    synthesis_block = f"""SYNTHESIS ESTIMATE:
Probability: {synthesis_p:.2f} ± {synthesis_unc:.2f}
Source Quality: {synthesis_quality:.2f}
Reasoning: {synthesis_reasoning}{contradictions_block}"""
    
    # Format evidence items
    evidence_lines = []
    for i, item in enumerate(evidence, 1):
        eid = item.get("evidence_id", f"e{i}")
        claim = item.get("claim", "")
        polarity = item.get("polarity", "NEUTRAL")
        reliability = item.get("reliability", 0.5)
        doc_id = item.get("doc_id", "unknown")
        
        evidence_lines.append(
            f"[{eid}] (from {doc_id}, {polarity}, reliability={reliability:.2f}):\n  {claim}"
        )
    
    evidence_block = "\n".join(evidence_lines) if evidence_lines else "(No evidence available)"
    
    # Format clarifications
    clarifications_block = ""
    if clarifications:
        clarifications_block = "\n\nCLARIFICATIONS:\n" + "\n".join(
            f"- {c}" for c in clarifications
        )
    
    prompt = f"""MARKET QUESTION:
{question}

RESOLUTION RULES:
{rules}{clarifications_block}

{synthesis_block}

EVIDENCE:
{evidence_block}

TASK:
Challenge the synthesis estimate above. Provide an INDEPENDENT probability estimate.

Consider:
1. Did the synthesis underweight important counterevidence?
2. Is the uncertainty band too narrow (overconfidence)?
3. Are there rule ambiguities that increase dispute risk?
4. What's the strongest case for the OPPOSITE outcome?
5. Did source quality assessment miss red flags?

Your estimate should reflect genuine disagreement if warranted.

Output ONLY the JSON object, nothing else."""
    
    return prompt

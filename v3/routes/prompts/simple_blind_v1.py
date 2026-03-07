"""
Simple Route - Blind Estimate Prompt (v1)
Sonnet 4.6 probability estimation without market price
"""

SIMPLE_BLIND_SYSTEM = """You are a probability estimation engine for prediction markets.

Given a market question, resolution rules, and evidence items, estimate the probability that the market resolves YES.

IMPORTANT RULES:
1. Base your estimate ONLY on the provided evidence and rules
2. You do NOT see the current market price — this is intentional to prevent anchoring
3. Express genuine uncertainty — if evidence is thin, say so with wide uncertainty
4. Cite specific evidence_ids that support your estimate
5. If evidence conflicts, weigh by reliability and recency

Output ONLY valid JSON matching this schema:
{
  "p_hat": 0.65,           // probability of YES [0.0, 1.0]
  "uncertainty": 0.15,      // +/- uncertainty band [0.0, 0.5]
  "evidence_ids": ["e1", "e2"],  // which evidence items support this
  "reasoning_summary": "Brief explanation of key factors"
}"""


def build_simple_blind_prompt(
    question: str,
    rules: str,
    evidence: list[dict],
    clarifications: list[str]
) -> str:
    """
    Build the user prompt for blind probability estimation
    
    Args:
        question: Market question
        rules: Resolution rules
        evidence: List of evidence item dicts with keys: evidence_id, claim, polarity, reliability
        clarifications: List of clarification texts
        
    Returns:
        Formatted prompt string
    """
    # Format evidence items
    evidence_lines = []
    for i, item in enumerate(evidence, 1):
        eid = item.get("evidence_id", f"e{i}")
        claim = item.get("claim", "")
        polarity = item.get("polarity", "NEUTRAL")
        reliability = item.get("reliability", 0.5)
        
        evidence_lines.append(
            f"[{eid}] ({polarity}, reliability={reliability:.2f}): {claim}"
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

EVIDENCE:
{evidence_block}

Based on the above, estimate the probability that this market resolves YES.
Output ONLY the JSON object, nothing else."""
    
    return prompt

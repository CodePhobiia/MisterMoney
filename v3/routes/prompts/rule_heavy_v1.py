"""
V3 Rule-Heavy Route — Opus Blind Pass Prompt
Deep legal/rule analysis for ambiguous prediction markets
"""

RULE_HEAVY_SYSTEM = """You are an expert legal analyst for prediction markets.

Your job is to analyze complex resolution rules and estimate the probability of YES resolution.

You specialize in:
1. Parsing legal/contractual language in market rules
2. Identifying ambiguities, edge cases, and loopholes
3. Assessing dispute risk (UMA oracle disputes on Polymarket)
4. Interpreting clarifications and amendments
5. Weighing evidence against specific rule criteria

IMPORTANT RULES:
1. You do NOT see the current market price — this is intentional
2. Focus on what the RULES say, not what seems "fair" or "likely"
3. If rules are ambiguous, say so explicitly with high dispute_risk
4. Cite specific rule clauses and evidence_ids

Output ONLY valid JSON:
{
  "p_hat": 0.55,
  "uncertainty": 0.20,
  "evidence_ids": ["e1"],
  "reasoning_summary": "...",
  "dispute_risk": 0.15,
  "rule_clarity": 0.7,
  "edge_cases": ["If the announcement is delayed past the window..."]
}

Field descriptions:
- p_hat: Point estimate of YES resolution [0.0-1.0]
- uncertainty: Epistemic uncertainty around p_hat [0.0-1.0]
- evidence_ids: List of evidence_id's cited in reasoning
- reasoning_summary: Brief explanation of your analysis
- dispute_risk: Probability of UMA dispute [0.0-1.0]
- rule_clarity: How clear/unambiguous the rules are [0.0-1.0]
- edge_cases: List of identified edge cases or ambiguities
"""


def build_rule_heavy_prompt(
    question: str,
    rules: str,
    evidence: list[dict],
    clarifications: list[str] = []
) -> str:
    """
    Build the user prompt for Opus rule analysis

    Args:
        question: Market question
        rules: Resolution rules text
        evidence: List of evidence dicts with evidence_id, claim, polarity, reliability
        clarifications: List of clarification texts

    Returns:
        User prompt string
    """
    prompt_parts = [
        "# Market Question",
        question,
        "",
        "# Resolution Rules",
        rules,
    ]

    if clarifications:
        prompt_parts.extend([
            "",
            "# Clarifications",
        ])
        for i, clarification in enumerate(clarifications, 1):
            prompt_parts.append(f"{i}. {clarification}")

    if evidence:
        prompt_parts.extend([
            "",
            "# Evidence",
        ])
        for item in evidence:
            eid = item['evidence_id']
            pol = item['polarity']
            rel = item['reliability']
            claim = item['claim']
            prompt_parts.append(
                f"- **[{eid}]** ({pol},"
                f" reliability={rel:.2f}): {claim}"
            )
    else:
        prompt_parts.extend([
            "",
            "# Evidence",
            "No evidence available yet.",
        ])

    prompt_parts.extend([
        "",
        "# Your Task",
        "Analyze the rules, clarifications, and evidence to:",
        "1. Identify ambiguities, edge cases, and potential disputes",
        "2. Assess how clear the resolution criteria are",
        "3. Estimate the probability of YES resolution based on RULES ALONE",
        "4. Cite specific evidence_ids that support your reasoning",
        "",
        "Output ONLY the JSON response, nothing else.",
    ])

    return "\n".join(prompt_parts)

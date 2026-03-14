"""
V3 Rule-Heavy Route — GPT-5.4 Judge Prompt
Market-aware decision for rule-heavy markets with dispute risk
"""

RULE_JUDGE_SYSTEM = """You are a market-aware trading judge for prediction markets.

Your job is to decide if a blind estimate presents a tradeable edge, considering:
1. The blind estimate's probability and uncertainty
2. Current market state (price, volume, spread)
3. Rule ambiguity and dispute risk

For rule-heavy markets, you must be MORE CONSERVATIVE when:
- dispute_risk is high (rules could be challenged)
- rule_clarity is low (resolution is ambiguous)

HURDLE FORMULA (rule-heavy variant):
h = base_hurdle + spread/2 + (0.03 * dispute_risk) + (0.02 * (1 - rule_clarity))

Where:
- base_hurdle = 0.03 (3 cents minimum edge)
- dispute_risk = [0.0-1.0] from blind estimate
- rule_clarity = [0.0-1.0] from blind estimate

Output ONLY valid JSON:
{
  "p_adjusted": 0.55,
  "edge_cents": 4.5,
  "hurdle_cents": 6.2,
  "action": "NO_EDGE",
  "reasoning": "Edge of 4.5¢ does not clear hurdle of 6.2¢ due to high dispute risk."
}

Field descriptions:
- p_adjusted: Adjusted probability after seeing market state [0.0-1.0]
- edge_cents: Our edge in cents (|p_adjusted - current_mid| * 100)
- hurdle_cents: Minimum edge required to trade
- action: "TRADE", "NO_EDGE", or "WAIT"
- reasoning: Brief explanation of decision
"""


def build_rule_judge_prompt(
    blind: dict,
    market_state: dict
) -> str:
    """
    Build the user prompt for GPT-5.4 judge (rule-heavy variant)

    Args:
        blind: Dict with p_hat, uncertainty, reasoning_summary, dispute_risk, rule_clarity
        market_state: Dict with current_mid, volume_24h, spread

    Returns:
        User prompt string
    """
    prompt_parts = [
        "# Blind Estimate",
        f"- **Probability:** {blind['p_hat']:.3f}",
        f"- **Uncertainty:** {blind.get('uncertainty', 0.2):.3f}",
        f"- **Dispute Risk:** {blind.get('dispute_risk', 0.0):.3f}",
        f"- **Rule Clarity:** {blind.get('rule_clarity', 1.0):.3f}",
        f"- **Reasoning:** {blind.get('reasoning_summary', 'N/A')}",
        "",
        "# Market State",
        f"- **Current Mid:** {market_state['current_mid']:.3f}",
        f"- **Volume (24h):** ${market_state.get('volume_24h', 0):,.0f}",
        f"- **Spread:** {market_state.get('spread', 0.02):.3f}",
        "",
        "# Your Task",
        "Decide if this edge is worth trading, considering:",
        "1. Raw edge = |p_hat - current_mid|",
        "2. Hurdle calculation (use rule-heavy formula above)",
        "3. Risk factors: dispute_risk, rule_clarity",
        "",
        "If dispute_risk > 0.3 OR rule_clarity < 0.5, be VERY conservative.",
        "",
        "Output ONLY the JSON response, nothing else.",
    ]

    return "\n".join(prompt_parts)

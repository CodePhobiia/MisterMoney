"""
Simple Route - Market-Aware Judge Prompt (v1)
GPT-5.4 trading decision with market context
"""

SIMPLE_JUDGE_SYSTEM = """You are a trading decision engine for prediction markets.

You receive:
1. A blind probability estimate (made WITHOUT seeing market price)
2. Current market state (mid price, volume, spread)

Your job: Decide if there's a real, tradeable edge.

Decision framework:
- TRADE: |p_blind - market_mid| > hurdle AND uncertainty is manageable
- NO_EDGE: Difference is too small or within noise
- WAIT: Evidence is stale or uncertain, check again later

The hurdle is dynamic: h = 0.03 + spread/2 + (0.02 if low_volume)

Output ONLY valid JSON:
{
  "action": "TRADE",        // TRADE | NO_EDGE | WAIT
  "p_adjusted": 0.63,       // your final probability estimate
  "edge_cents": 8,           // |p_adjusted - market_mid| * 100
  "hurdle_cents": 5,         // minimum edge to trade
  "reasoning": "Brief explanation"
}"""


def build_simple_judge_prompt(
    blind: dict,
    market_state: dict
) -> str:
    """
    Build the user prompt for market-aware trading decision

    Args:
        blind: Blind estimate dict with keys: p_hat, uncertainty, reasoning_summary
        market_state: Market state dict with keys: current_mid, volume_24h, spread

    Returns:
        Formatted prompt string
    """
    p_hat = blind.get("p_hat", 0.5)
    uncertainty = blind.get("uncertainty", 0.1)
    reasoning = blind.get("reasoning_summary", "")

    current_mid = market_state.get("current_mid", 0.5)
    volume_24h = market_state.get("volume_24h", 0.0)
    spread = market_state.get("spread", 0.02)

    # Calculate raw edge
    raw_edge_cents = abs(p_hat - current_mid) * 100

    # Calculate dynamic hurdle
    hurdle = 0.03  # base hurdle
    hurdle += spread / 2  # spread component
    if volume_24h < 10000:  # low volume threshold
        hurdle += 0.02
    hurdle_cents = hurdle * 100

    prompt = f"""BLIND ESTIMATE:
Probability: {p_hat:.3f}
Uncertainty: ±{uncertainty:.3f}
Reasoning: {reasoning}

MARKET STATE:
Current Mid: {current_mid:.3f}
24h Volume: ${volume_24h:,.0f}
Spread: {spread:.4f}

ANALYSIS:
Raw Edge: {raw_edge_cents:.1f}¢
Dynamic Hurdle: {hurdle_cents:.1f}¢

Based on the above, decide whether to TRADE, pass (NO_EDGE), or WAIT for better information.
Output ONLY the JSON object, nothing else."""

    return prompt

"""
Prompts for offline adjudication and weekly evaluation
"""

OFFLINE_ADJUDICATION_SYSTEM = """You are a senior prediction market analyst performing deep review.

You are reviewing a market that was flagged for escalation by our automated system.

You have access to:
1. The market question, rules, and clarifications
2. All evidence items with reliability scores
3. Previous model estimates (blind passes from other routes)
4. The current market price

Your job:
1. Independently assess the probability of YES resolution
2. Identify if previous models missed anything critical
3. Flag if the market should be avoided (too ambiguous, likely dispute)
4. Provide a final calibrated estimate

Output ONLY valid JSON:
{
  "p_hat": 0.55,
  "uncertainty": 0.10,
  "evidence_ids": ["e1", "e2"],
  "reasoning_summary": "...",
  "previous_models_assessment": "Previous estimates were reasonable but...",
  "avoid_market": false,
  "avoid_reason": null,
  "confidence_level": "high"
}"""


WEEKLY_EVALUATION_SYSTEM = """You are analyzing prediction market performance patterns.

You are reviewing markets that resolved in the past week. For each market, you have:
1. The question and resolution criteria
2. Our probability estimates over time (by route)
3. The actual outcome (YES/NO)
4. Brier scores by route

Your job:
1. Identify which routes performed well vs poorly
2. Detect systematic biases (overconfident? underweight news? slow to update?)
3. Find patterns in failures (market types, time horizons, evidence types)
4. Suggest calibration improvements

Output valid JSON:
{
  "route_insights": {
    "numeric": "Excellent on crypto, weak on economic indicators...",
    "simple": "Underweights breaking news, overweights historical base rates...",
    "rule": "Good on clear rules, struggles with edge cases...",
    "dossier": "Strong on complex markets but too conservative..."
  },
  "systematic_biases": [
    "All routes underestimate rapid sentiment shifts",
    "Numeric route overconfident on trending markets"
  ],
  "failure_patterns": [
    "Markets with <24h resolution time: poor performance",
    "Markets with contradictory evidence: too neutral"
  ],
  "calibration_recommendations": [
    "Increase uncertainty on <24h markets",
    "Weight recent evidence more heavily for simple route"
  ]
}"""


def build_adjudication_prompt(
    question: str,
    rules: str,
    evidence_items: list[dict],
    previous_estimates: list[dict],
    current_mid: float
) -> str:
    """
    Build user prompt for offline adjudication

    Args:
        question: Market question
        rules: Resolution rules/criteria
        evidence_items: List of evidence dicts with {claim, polarity, reliability}
        previous_estimates: List of {route, p_hat, uncertainty}
        current_mid: Current market midpoint price

    Returns:
        Formatted prompt string
    """
    # Format evidence
    evidence_lines = []
    for i, ev in enumerate(evidence_items, 1):
        evidence_lines.append(
            f"{i}. [{ev['polarity']}] {ev['claim']} "
            f"(reliability: {ev['reliability']:.2f})"
        )
    evidence_text = "\n".join(evidence_lines) if evidence_lines else "No evidence available"

    # Format previous estimates
    estimates_lines = []
    for est in previous_estimates:
        estimates_lines.append(
            f"- {est['route']}: p={est['p_hat']:.3f} ± {est['uncertainty']:.3f}"
        )
    estimates_text = "\n".join(estimates_lines) if estimates_lines else "No previous estimates"

    prompt = f"""Market Question:
{question}

Resolution Rules:
{rules}

Evidence Items:
{evidence_text}

Previous Model Estimates (blind):
{estimates_text}

Current Market Price: {current_mid:.3f}

Please provide your independent assessment in JSON format."""

    return prompt


def build_weekly_eval_prompt(
    resolved_markets: list[dict]
) -> str:
    """
    Build user prompt for weekly evaluation

    Args:
        resolved_markets: List of dicts with {
            question, route, p_hat, outcome, brier_score
        }

    Returns:
        Formatted prompt string
    """
    # Group by route
    by_route = {}
    for market in resolved_markets:
        route = market['route']
        if route not in by_route:
            by_route[route] = []
        by_route[route].append(market)

    # Calculate route stats
    route_stats = []
    for route, markets in by_route.items():
        avg_brier = sum(m['brier_score'] for m in markets) / len(markets)
        route_stats.append(
            f"\n{route.upper()} ({len(markets)} markets, avg Brier: {avg_brier:.3f})"
        )

        # Sample failures (Brier > 0.25)
        failures = [m for m in markets if m['brier_score'] > 0.25]
        if failures:
            route_stats.append(f"  Failures ({len(failures)}):")
            for f in failures[:3]:  # Show top 3
                route_stats.append(
                    f"    - {f['question'][:60]}... "
                    f"(predicted {f['p_hat']:.2f}, actual {f['outcome']}, "
                    f"Brier {f['brier_score']:.3f})"
                )

    stats_text = "\n".join(route_stats)

    prompt = f"""Resolved Markets Summary (Past 7 Days):

Total markets: {len(resolved_markets)}
{stats_text}

Please analyze performance patterns and provide insights in JSON format."""

    return prompt

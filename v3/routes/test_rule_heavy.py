"""
V3 Rule-Heavy Route Integration Test
Tests Opus rule analysis + GPT-5.4 judge with LIVE API calls
"""

import asyncio
import json
from datetime import datetime, timedelta
import structlog

from v3.providers.registry import ProviderRegistry
from v3.evidence.graph import EvidenceGraph
from v3.evidence.db import Database
from v3.evidence.entities import EvidenceItem
from v3.intake.schemas import MarketMeta
from v3.routes.rule_heavy import RuleHeavyRoute

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer()
    ]
)

log = structlog.get_logger()


async def test_government_shutdown_scenario():
    """
    Test rule-heavy route with government shutdown scenario.
    
    This is a REAL ambiguous market with complex resolution rules.
    """
    print("\n" + "="*80)
    print("V3 Rule-Heavy Route Integration Test")
    print("Scenario: US Government Shutdown Market")
    print("="*80 + "\n")
    
    # Initialize provider registry
    print("Initializing provider registry...")
    registry = ProviderRegistry()
    await registry.initialize()
    print("✓ Registry initialized\n")
    
    # Initialize evidence graph (mock for now)
    evidence_graph = EvidenceGraph(db=None)  # Pass None for mock
    
    # Initialize rule-heavy route
    route = RuleHeavyRoute(registry, evidence_graph)
    print("✓ RuleHeavyRoute initialized\n")
    
    # Define the market scenario
    condition_id = "test-gov-shutdown-2026"
    
    market = MarketMeta(
        condition_id=condition_id,
        question="Will the US government shut down before April 1, 2026?",
        description="This market resolves YES if the US federal government enters a shutdown state before April 1, 2026.",
        resolution_source="Official government announcements",
        end_date=datetime(2026, 4, 1),
        rules="""
This market resolves YES if the United States federal government enters a shutdown state before April 1, 2026, 12:00 PM ET.

**Shutdown Definition:**
A "shutdown" occurs when federal funding lapses for at least one federal agency due to Congress failing to pass appropriations bills or continuing resolutions. The shutdown must be officially acknowledged by the Office of Management and Budget (OMB) or the White House.

**Qualifying Events:**
- Full government shutdown (all non-essential federal operations cease)
- Partial shutdown (at least one major department/agency shuts down)

**Non-Qualifying Events:**
- Furloughs without a funding lapse
- Shutdowns affecting ONLY legislative branch operations
- Agency closures due to weather, security, or other non-funding reasons
- Continuing resolutions passed before the deadline (no lapse occurs)

**Edge Cases:**
- If a shutdown begins at 11:59 PM on March 31, 2026, and funding is restored at 12:01 AM on April 1, 2026, does this count? (Ambiguous — depends on official OMB announcement)
- If shutdown is announced but funding is retroactively restored within 1 hour, does this count? (Ambiguous)
- If only the legislative branch shuts down, does this count? (Explicitly NO)

**Resolution Source:**
Official statements from OMB, the White House, or major news outlets (NYT, WSJ, WaPo, AP, Reuters).
        """.strip(),
        clarifications=[
            "Clarification 1: A shutdown lasting less than 1 hour does NOT qualify unless officially announced by OMB.",
            "Clarification 2: If a shutdown is announced but funding is restored retroactively before any furloughs occur, this does NOT count.",
        ],
        volume_24h=125000.0,  # $125k volume
        current_mid=0.42,  # Market thinks 42% chance
    )
    
    # Create mock evidence items
    evidence = [
        EvidenceItem(
            evidence_id="e1",
            condition_id=condition_id,
            ts_event=datetime(2026, 3, 5),
            ts_observed=datetime.utcnow(),
            polarity="YES",
            claim="House Speaker announces budget negotiations have stalled, with no resolution expected before March 15 deadline.",
            reliability=0.85,
        ),
        EvidenceItem(
            evidence_id="e2",
            condition_id=condition_id,
            ts_event=datetime(2026, 3, 6),
            ts_observed=datetime.utcnow(),
            polarity="NO",
            claim="Senate Majority Leader says bipartisan continuing resolution is 'very likely' to pass by month-end.",
            reliability=0.75,
        ),
        EvidenceItem(
            evidence_id="e3",
            condition_id=condition_id,
            ts_event=datetime(2026, 3, 7),
            ts_observed=datetime.utcnow(),
            polarity="MIXED",
            claim="White House budget office warns of potential 'brief lapse' in funding if deal is not reached by March 20.",
            reliability=0.80,
        ),
        EvidenceItem(
            evidence_id="e4",
            condition_id=condition_id,
            ts_event=datetime(2026, 3, 8),
            ts_observed=datetime.utcnow(),
            polarity="NEUTRAL",
            claim="Historical data: US government has shut down 21 times since 1976, with 4 shutdowns occurring in the past 10 years.",
            reliability=0.95,
        ),
    ]
    
    print(f"Market Question: {market.question}")
    print(f"Current Market Price: {market.current_mid:.1%}")
    print(f"Volume (24h): ${market.volume_24h:,.0f}")
    print(f"Evidence Items: {len(evidence)}\n")
    
    # TEST 1: Opus Blind Rule Pass
    print("-" * 80)
    print("TEST 1: Opus Blind Rule Analysis (LIVE API CALL)")
    print("-" * 80 + "\n")
    
    blind = await route.opus_rule_pass(
        condition_id=condition_id,
        rule_text=market.rules,
        clarifications=market.clarifications,
        evidence=evidence
    )
    
    # Extract extended fields
    extended_data = json.loads(blind.reasoning_summary)
    
    print("✓ Opus Analysis Complete\n")
    print(f"Probability (p_hat):     {blind.p_hat:.1%}")
    print(f"Uncertainty:             {blind.uncertainty:.1%}")
    print(f"Dispute Risk:            {extended_data['dispute_risk']:.1%}")
    print(f"Rule Clarity:            {extended_data['rule_clarity']:.1%}")
    print(f"Evidence IDs:            {blind.evidence_ids}")
    print(f"Edge Cases ({len(extended_data['edge_cases'])}):")
    for i, edge_case in enumerate(extended_data['edge_cases'], 1):
        print(f"  {i}. {edge_case}")
    print(f"\nReasoning Summary:")
    print(f"  {extended_data['base_reasoning'][:300]}...")
    print()
    
    # TEST 2: GPT-5.4 Judge Pass
    print("-" * 80)
    print("TEST 2: GPT-5.4 Market-Aware Judge (LIVE API CALL)")
    print("-" * 80 + "\n")
    
    decision = await route.judge_pass(
        condition_id=condition_id,
        blind=blind,
        current_mid=market.current_mid,
        volume_24h=market.volume_24h,
        spread=0.02,
    )
    
    print("✓ Judge Decision Complete\n")
    print(f"Current Market Mid:      {market.current_mid:.1%}")
    print(f"Blind Estimate:          {blind.p_hat:.1%}")
    print(f"Edge (cents):            {decision.edge_cents:.2f}¢")
    print(f"Hurdle (cents):          {decision.hurdle_cents:.2f}¢")
    print(f"Action:                  {decision.action}")
    print()
    
    # TEST 3: Escalation Logic
    print("-" * 80)
    print("TEST 3: Escalation Logic Tests")
    print("-" * 80 + "\n")
    
    # Scenario A: Current market (should escalate if high dispute risk or low clarity)
    escalate_a = route.should_escalate_async(blind, market_notional=market.volume_24h)
    print(f"Scenario A (Current Market):")
    print(f"  Dispute Risk: {extended_data['dispute_risk']:.1%}, Rule Clarity: {extended_data['rule_clarity']:.1%}")
    print(f"  Notional: ${market.volume_24h:,.0f}, Uncertainty: {blind.uncertainty:.1%}")
    print(f"  → Escalate: {'YES ⚠️' if escalate_a else 'NO ✓'}\n")
    
    # Scenario B: High-value market (should escalate)
    escalate_b = route.should_escalate_async(blind, market_notional=100000.0)
    print(f"Scenario B (High-Value Market - $100k notional):")
    print(f"  Dispute Risk: {extended_data['dispute_risk']:.1%}, Rule Clarity: {extended_data['rule_clarity']:.1%}")
    print(f"  Notional: $100,000, Uncertainty: {blind.uncertainty:.1%}")
    print(f"  → Escalate: {'YES ⚠️' if escalate_b else 'NO ✓'}\n")
    
    # Scenario C: Clear rules, low dispute (should NOT escalate)
    # Mock a clearer blind estimate
    from v3.evidence.entities import BlindEstimate
    clear_blind = BlindEstimate(
        p_hat=0.55,
        uncertainty=0.15,
        evidence_ids=["e1"],
        model="claude-opus-4-6",
        reasoning_summary=json.dumps({
            "dispute_risk": 0.1,
            "rule_clarity": 0.9,
            "edge_cases": [],
            "base_reasoning": "Clear rules, low ambiguity"
        })
    )
    escalate_c = route.should_escalate_async(clear_blind, market_notional=10000.0)
    print(f"Scenario C (Clear Rules, Low Dispute):")
    print(f"  Dispute Risk: 10.0%, Rule Clarity: 90.0%")
    print(f"  Notional: $10,000, Uncertainty: 15.0%")
    print(f"  → Escalate: {'YES ⚠️' if escalate_c else 'NO ✓'}\n")
    
    # TEST 4: End-to-End Execute
    print("-" * 80)
    print("TEST 4: End-to-End Route Execution (LIVE API CALLS)")
    print("-" * 80 + "\n")
    
    signal = await route.execute(
        condition_id=condition_id,
        market=market,
        evidence_bundle=evidence,
        rule_text=market.rules,
        clarifications=market.clarifications
    )
    
    print("✓ Full Route Execution Complete\n")
    print(f"Final Signal:")
    print(f"  Calibrated Probability:  {signal.p_calibrated:.1%}")
    print(f"  Confidence Range:        [{signal.p_low:.1%} - {signal.p_high:.1%}]")
    print(f"  Uncertainty:             {signal.uncertainty:.1%}")
    print(f"  Hurdle (cents):          {signal.hurdle_cents:.2f}¢")
    print(f"  Hurdle Met:              {'YES ✓' if signal.hurdle_met else 'NO'}")
    print(f"  Route:                   {signal.route}")
    print(f"  Models Used:             {', '.join(signal.models_used)}")
    print(f"  Expires At:              {signal.expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print()
    
    # Final Summary
    print("=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    print(f"✓ Opus blind pass completed")
    print(f"✓ GPT-5.4 judge pass completed")
    print(f"✓ Escalation logic tested (3 scenarios)")
    print(f"✓ End-to-end execution completed")
    print()
    print(f"Sample Opus Analysis:")
    print(f"  - Identified {len(extended_data['edge_cases'])} edge cases")
    print(f"  - Dispute risk: {extended_data['dispute_risk']:.1%}")
    print(f"  - Rule clarity: {extended_data['rule_clarity']:.1%}")
    print(f"  - Probability: {blind.p_hat:.1%} (market: {market.current_mid:.1%})")
    print(f"  - Edge: {abs(blind.p_hat - market.current_mid) * 100:.2f}¢")
    print()
    print("=" * 80)
    print()


if __name__ == "__main__":
    asyncio.run(test_government_shutdown_scenario())

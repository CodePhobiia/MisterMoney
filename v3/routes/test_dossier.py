"""
V3 Dossier Route Integration Test
Tests end-to-end dossier route with live provider calls
"""

import asyncio
import hashlib
from datetime import datetime, timedelta
import structlog

from v3.providers.registry import ProviderRegistry
from v3.evidence.entities import (
    SourceDocument,
    EvidenceItem,
    FairValueSignal,
)
from v3.intake.schemas import MarketMeta
from v3.routes.dossier import DossierRoute

log = structlog.get_logger()


async def test_dossier_route():
    """
    Integration test for dossier route
    
    Tests:
    1. Gemini synthesis (or Sonnet fallback if rate-limited)
    2. Opus adversarial challenge
    3. GPT-5.4 disagreement resolution
    4. Full pipeline end-to-end
    """
    print("\n" + "=" * 80)
    print("V3 DOSSIER ROUTE INTEGRATION TEST")
    print("=" * 80 + "\n")
    
    # Initialize provider registry
    print("Initializing provider registry...")
    registry = ProviderRegistry()
    await registry.initialize()
    print("✓ Registry initialized\n")
    
    # Check provider availability (unhealthy providers are removed during initialization)
    print("Provider Availability:")
    gemini = await registry.get("gemini")
    opus = await registry.get("opus")
    gpt54 = await registry.get("gpt54")
    sonnet = await registry.get("sonnet")
    
    print(f"  Gemini: {'✓ Available' if gemini else '✗ Unavailable (will use Sonnet fallback)'}")
    print(f"  Opus:   {'✓ Available' if opus else '✗ Unavailable'}")
    print(f"  GPT-5.4: {'✓ Available' if gpt54 else '✗ Unavailable'}")
    print(f"  Sonnet: {'✓ Available' if sonnet else '✗ Unavailable'}")
    print()
    
    # Create mock market scenario
    print("Creating mock market scenario...")
    print("Question: 'Will Company X complete its acquisition of Company Y by Q2 2026?'")
    print()
    
    market = MarketMeta(
        condition_id="cond_test_merger_001",
        question="Will Company X complete its acquisition of Company Y by Q2 2026?",
        description="This market resolves YES if the merger closes by June 30, 2026.",
        resolution_source="SEC filings and official press releases",
        end_date=datetime(2026, 6, 30),
        rules="""This market resolves YES if:
1. Company X completes the acquisition of Company Y
2. The deal closes on or before June 30, 2026
3. All regulatory approvals are obtained
4. Shareholder votes approve the transaction
5. Financing is secured

The market resolves NO if:
- The deal is terminated or abandoned
- The deadline passes without closing
- Regulatory approval is denied""",
        clarifications=[
            "A merger extension beyond Q2 2026 counts as NO",
            "Partial acquisitions (< 50% stake) count as NO",
        ],
        volume_24h=125000.0,
        current_mid=0.52,
    )
    
    # Create mock source documents
    print("Creating mock source documents...")
    
    doc1 = SourceDocument(
        doc_id="doc_sec_filing_001",
        url="https://sec.gov/filing/merger-company-x-y",
        source_type="filing",
        publisher="SEC",
        fetched_at=datetime.utcnow() - timedelta(days=2),
        content_hash=hashlib.sha256(b"sec filing content").hexdigest(),
        title="Company X Acquisition Filing - Form S-4",
        text_path="/data/docs/sec_filing_001.txt",
        metadata={"filing_type": "S-4", "date_filed": "2025-12-15"},
    )
    
    doc2 = SourceDocument(
        doc_id="doc_wsj_article_001",
        url="https://wsj.com/company-x-merger-concerns",
        source_type="article",
        publisher="Wall Street Journal",
        fetched_at=datetime.utcnow() - timedelta(days=1),
        content_hash=hashlib.sha256(b"wsj article content").hexdigest(),
        title="Regulatory Hurdles Loom for Company X Merger",
        text_path="/data/docs/wsj_001.txt",
        metadata={"author": "Jane Reporter", "paywall": False},
    )
    
    doc3 = SourceDocument(
        doc_id="doc_analyst_report_001",
        url="https://goldmansachs.com/research/merger-analysis",
        source_type="article",
        publisher="Goldman Sachs Research",
        fetched_at=datetime.utcnow() - timedelta(hours=12),
        content_hash=hashlib.sha256(b"analyst report").hexdigest(),
        title="Merger Probability Analysis: Company X/Y Deal",
        text_path="/data/docs/analyst_001.txt",
        metadata={"analyst": "John Smith", "rating": "buy"},
    )
    
    doc4 = SourceDocument(
        doc_id="doc_twitter_001",
        url="https://twitter.com/dealtracker/status/123456",
        source_type="social",
        publisher="Twitter",
        fetched_at=datetime.utcnow() - timedelta(hours=3),
        content_hash=hashlib.sha256(b"twitter post").hexdigest(),
        title="Tweet about merger concerns",
        text_path=None,
        metadata={"author": "@dealtracker", "likes": 450},
    )
    
    doc5 = SourceDocument(
        doc_id="doc_press_release_001",
        url="https://companyx.com/press/merger-announcement",
        source_type="article",
        publisher="Company X",
        fetched_at=datetime.utcnow() - timedelta(days=30),
        content_hash=hashlib.sha256(b"press release").hexdigest(),
        title="Company X Announces Acquisition of Company Y",
        text_path="/data/docs/press_001.txt",
        metadata={"release_date": "2025-11-01"},
    )
    
    documents = [doc1, doc2, doc3, doc4, doc5]
    print(f"✓ Created {len(documents)} mock documents\n")
    
    # Create mock evidence items with contradictory evidence
    print("Creating mock evidence items...")
    
    evidence = [
        EvidenceItem(
            evidence_id="ev_001",
            condition_id="cond_test_merger_001",
            doc_id="doc_sec_filing_001",
            ts_event=datetime(2025, 12, 15),
            ts_observed=datetime.utcnow() - timedelta(days=2),
            polarity="YES",
            claim="SEC filing shows all required documents submitted for merger approval",
            reliability=0.95,
            freshness_hours=48.0,
        ),
        EvidenceItem(
            evidence_id="ev_002",
            condition_id="cond_test_merger_001",
            doc_id="doc_sec_filing_001",
            ts_event=datetime(2025, 12, 15),
            ts_observed=datetime.utcnow() - timedelta(days=2),
            polarity="YES",
            claim="Shareholder vote scheduled for March 2026, expected to pass with 78% approval",
            reliability=0.90,
            freshness_hours=48.0,
        ),
        EvidenceItem(
            evidence_id="ev_003",
            condition_id="cond_test_merger_001",
            doc_id="doc_wsj_article_001",
            ts_event=datetime.utcnow() - timedelta(days=1),
            ts_observed=datetime.utcnow() - timedelta(days=1),
            polarity="NO",
            claim="FTC chair expressed concerns about anticompetitive effects in public statement",
            reliability=0.85,
            freshness_hours=24.0,
        ),
        EvidenceItem(
            evidence_id="ev_004",
            condition_id="cond_test_merger_001",
            doc_id="doc_wsj_article_001",
            ts_event=datetime.utcnow() - timedelta(days=1),
            ts_observed=datetime.utcnow() - timedelta(days=1),
            polarity="MIXED",
            claim="Industry experts split 50/50 on whether regulatory approval will be granted",
            reliability=0.70,
            freshness_hours=24.0,
        ),
        EvidenceItem(
            evidence_id="ev_005",
            condition_id="cond_test_merger_001",
            doc_id="doc_analyst_report_001",
            ts_event=datetime.utcnow() - timedelta(hours=12),
            ts_observed=datetime.utcnow() - timedelta(hours=12),
            polarity="YES",
            claim="Goldman Sachs analysts estimate 65% probability of deal closing by Q2 2026",
            reliability=0.80,
            freshness_hours=12.0,
        ),
        EvidenceItem(
            evidence_id="ev_006",
            condition_id="cond_test_merger_001",
            doc_id="doc_twitter_001",
            ts_event=datetime.utcnow() - timedelta(hours=3),
            ts_observed=datetime.utcnow() - timedelta(hours=3),
            polarity="NO",
            claim="Unverified rumors on social media suggest financing issues",
            reliability=0.30,
            freshness_hours=3.0,
        ),
        EvidenceItem(
            evidence_id="ev_007",
            condition_id="cond_test_merger_001",
            doc_id="doc_press_release_001",
            ts_event=datetime(2025, 11, 1),
            ts_observed=datetime.utcnow() - timedelta(days=30),
            polarity="YES",
            claim="Company X secured $10B bridge loan to finance acquisition",
            reliability=0.95,
            freshness_hours=720.0,
        ),
    ]
    
    print(f"✓ Created {len(evidence)} evidence items")
    print(f"  - YES polarity: {sum(1 for e in evidence if e.polarity == 'YES')}")
    print(f"  - NO polarity: {sum(1 for e in evidence if e.polarity == 'NO')}")
    print(f"  - MIXED polarity: {sum(1 for e in evidence if e.polarity == 'MIXED')}")
    print(f"  - Reliability range: {min(e.reliability for e in evidence):.2f} - {max(e.reliability for e in evidence):.2f}")
    print()
    
    # Create DossierRoute instance (no EvidenceGraph needed for this test)
    print("Initializing DossierRoute...")
    route = DossierRoute(registry=registry, evidence_graph=None)
    print("✓ DossierRoute initialized\n")
    
    # Test 1: Gemini synthesis (or Sonnet fallback)
    print("=" * 80)
    print("TEST 1: Gemini Dossier Synthesis")
    print("=" * 80)
    
    try:
        synthesis_start = datetime.utcnow()
        synthesis_estimate = await route.gemini_synthesis(
            condition_id=market.condition_id,
            documents=documents,
            evidence=evidence,
            rule_text=market.rules,
            clarifications=market.clarifications,
        )
        synthesis_latency = (datetime.utcnow() - synthesis_start).total_seconds()
        
        print(f"\n✓ Synthesis complete ({synthesis_latency:.2f}s)")
        print(f"  Model: {synthesis_estimate.model}")
        print(f"  Probability: {synthesis_estimate.p_hat:.3f} ± {synthesis_estimate.uncertainty:.3f}")
        print(f"  Evidence IDs: {synthesis_estimate.evidence_ids}")
        print(f"  Reasoning: {synthesis_estimate.reasoning_summary[:300]}...")
        print()
        
    except Exception as e:
        print(f"\n✗ Synthesis failed: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Test 2: Opus adversarial challenge
    print("=" * 80)
    print("TEST 2: Opus Adversarial Challenge")
    print("=" * 80)
    
    try:
        challenge_start = datetime.utcnow()
        challenge_estimate = await route.opus_challenge(
            condition_id=market.condition_id,
            synthesis_estimate=synthesis_estimate,
            evidence=evidence,
            rule_text=market.rules,
        )
        challenge_latency = (datetime.utcnow() - challenge_start).total_seconds()
        
        print(f"\n✓ Challenge complete ({challenge_latency:.2f}s)")
        print(f"  Model: {challenge_estimate.model}")
        print(f"  Probability: {challenge_estimate.p_hat:.3f} ± {challenge_estimate.uncertainty:.3f}")
        print(f"  Evidence IDs: {challenge_estimate.evidence_ids}")
        print(f"  Reasoning: {challenge_estimate.reasoning_summary[:300]}...")
        
        diff = abs(synthesis_estimate.p_hat - challenge_estimate.p_hat)
        print(f"\n  Disagreement: {diff:.3f} ({diff*100:.1f}%)")
        
        print()
        
    except Exception as e:
        print(f"\n✗ Challenge failed: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Test 3: Disagreement resolution
    print("=" * 80)
    print("TEST 3: GPT-5.4 Disagreement Resolution")
    print("=" * 80)
    
    try:
        resolve_start = datetime.utcnow()
        decision = await route.resolve_disagreement(
            condition_id=market.condition_id,
            synthesis=synthesis_estimate,
            challenge=challenge_estimate,
            current_mid=market.current_mid,
            volume_24h=market.volume_24h,
            spread=0.02,
        )
        resolve_latency = (datetime.utcnow() - resolve_start).total_seconds()
        
        print(f"\n✓ Resolution complete ({resolve_latency:.2f}s)")
        print(f"  Final Model: {decision.blind_estimate.model}")
        print(f"  Final Probability: {decision.blind_estimate.p_hat:.3f} ± {decision.blind_estimate.uncertainty:.3f}")
        print(f"  Reasoning: {decision.blind_estimate.reasoning_summary[:200]}...")
        print(f"\n  Market Mid: {decision.current_mid:.3f}")
        print(f"  Edge: {decision.edge_cents:+.1f} cents")
        print(f"  Hurdle: {decision.hurdle_cents:.1f} cents")
        print(f"  Action: {decision.action}")
        
        print()
        
    except Exception as e:
        print(f"\n✗ Resolution failed: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Test 4: Full pipeline end-to-end
    print("=" * 80)
    print("TEST 4: Full Dossier Route Pipeline")
    print("=" * 80)
    
    try:
        pipeline_start = datetime.utcnow()
        signal = await route.execute(
            condition_id=market.condition_id,
            market=market,
            documents=documents,
            evidence=evidence,
            rule_text=market.rules,
            clarifications=market.clarifications,
        )
        pipeline_latency = (datetime.utcnow() - pipeline_start).total_seconds()
        
        print(f"\n✓ Pipeline complete ({pipeline_latency:.2f}s)")
        print(f"\n  Signal Details:")
        print(f"    Route: {signal.route}")
        print(f"    Calibrated P: {signal.p_calibrated:.3f}")
        print(f"    Range: [{signal.p_low:.3f}, {signal.p_high:.3f}]")
        print(f"    Uncertainty: {signal.uncertainty:.3f}")
        print(f"    Skew: {signal.skew_cents:+.1f} cents")
        print(f"    Hurdle Met: {signal.hurdle_met}")
        print(f"    Models Used: {', '.join(signal.models_used)}")
        print(f"    Evidence IDs: {len(signal.evidence_ids)}")
        print(f"    Expires: {signal.expires_at}")
        
        print()
        
    except Exception as e:
        print(f"\n✗ Pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Summary
    print("=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    
    total_latency = (datetime.utcnow() - synthesis_start).total_seconds()
    
    print(f"\n✓ All tests passed!")
    print(f"\n  Latency Breakdown:")
    print(f"    Gemini Synthesis:  {synthesis_latency:6.2f}s")
    print(f"    Opus Challenge:    {challenge_latency:6.2f}s")
    print(f"    GPT-5.4 Resolution:{resolve_latency:6.2f}s")
    print(f"    Full Pipeline:     {pipeline_latency:6.2f}s")
    print(f"    Total:             {total_latency:6.2f}s")
    
    print(f"\n  Estimate Comparison:")
    print(f"    Synthesis:  {synthesis_estimate.p_hat:.3f} ± {synthesis_estimate.uncertainty:.3f} ({synthesis_estimate.model})")
    print(f"    Challenge:  {challenge_estimate.p_hat:.3f} ± {challenge_estimate.uncertainty:.3f} ({challenge_estimate.model})")
    print(f"    Final:      {decision.blind_estimate.p_hat:.3f} ± {decision.blind_estimate.uncertainty:.3f} ({decision.blind_estimate.model})")
    print(f"    Market Mid: {market.current_mid:.3f}")
    print(f"    Edge:       {decision.edge_cents:+.1f} cents")
    
    print(f"\n  Action: {decision.action}")
    print()
    
    # Clean up provider sessions
    await registry.close_all()


if __name__ == "__main__":
    # Configure structlog for clean output
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ]
    )
    
    asyncio.run(test_dossier_route())

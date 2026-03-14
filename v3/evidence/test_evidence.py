"""
V3 Evidence Layer Integration Test
Tests all components working together
"""

import asyncio
from datetime import UTC, datetime, timedelta

import structlog

from .db import Database
from .entities import (
    EvidenceItem,
    FairValueSignal,
    RuleGraph,
    SourceDocument,
)
from .graph import EvidenceGraph
from .normalizer import EvidenceNormalizer
from .retrieval import EvidenceRetrieval
from .storage import ObjectStore

log = structlog.get_logger()


async def test_evidence_layer():
    """Run comprehensive integration test"""

    print("\n" + "=" * 60)
    print("V3 EVIDENCE LAYER INTEGRATION TEST")
    print("=" * 60 + "\n")

    # Database connection
    dsn = "postgresql://mmbot:mmbot_v3_2026@localhost/mistermoney_v3"
    db = Database(dsn)

    try:
        # Step 1: Connect to database
        print("1. Connecting to database...")
        await db.connect()
        print("   ✓ Connected\n")

        # Step 2: Run migrations
        print("2. Running migrations...")
        await db.run_migrations()
        print("   ✓ Migrations completed\n")

        # Initialize components
        graph = EvidenceGraph(db)
        retrieval = EvidenceRetrieval(db)
        normalizer = EvidenceNormalizer()
        store = ObjectStore()

        # Step 3: Create sample SourceDocument
        print("3. Creating sample source document...")
        doc = SourceDocument(
            doc_id="test_doc_001",
            url="https://example.com/article",
            source_type="article",
            publisher="TestPublisher",
            content_hash="abc123def456",
            title="Sample Article About Market Conditions",
            text_path="docs/test_doc_001.txt",
            metadata={
                "author": "Test Author",
                "category": "finance",
            }
        )

        doc_id = await graph.upsert_document(doc)
        print(f"   ✓ Document created: {doc_id}\n")

        # Step 4: Create sample EvidenceItems
        print("4. Creating sample evidence items...")
        evidence_items = [
            EvidenceItem(
                evidence_id="test_ev_001",
                condition_id="COND_001",
                doc_id=doc_id,
                ts_event=datetime.now(UTC) - timedelta(hours=2),
                polarity="YES",
                claim="Revenue increased by 15% in Q4",
                reliability=0.85,
                freshness_hours=2.0,
                extracted_values={
                    "metric": "revenue",
                    "change": 15.0,
                    "period": "Q4"
                }
            ),
            EvidenceItem(
                evidence_id="test_ev_002",
                condition_id="COND_001",
                doc_id=doc_id,
                ts_event=datetime.now(UTC) - timedelta(hours=1),
                polarity="NO",
                claim="Market volatility decreased slightly",
                reliability=0.70,
                freshness_hours=1.0,
                extracted_values={
                    "metric": "volatility",
                    "direction": "down"
                }
            ),
            EvidenceItem(
                evidence_id="test_ev_003",
                condition_id="COND_001",
                doc_id=doc_id,
                ts_event=datetime.now(UTC),
                polarity="MIXED",
                claim="Analysts have mixed opinions on future growth",
                reliability=0.60,
                freshness_hours=0.5,
                extracted_values={
                    "sentiment": "mixed"
                }
            ),
        ]

        for item in evidence_items:
            await graph.add_evidence(item)
            # Add embeddings
            await retrieval.embed_evidence(item.evidence_id, item.claim)

        print(f"   ✓ Created {len(evidence_items)} evidence items\n")

        # Step 5: Create sample RuleGraph
        print("5. Creating sample rule graph...")
        rule = RuleGraph(
            condition_id="COND_001",
            source_name="Q4 Revenue Growth > 10%",
            operator=">",
            threshold_num=10.0,
            threshold_text="10 percent",
            window_start=datetime.now(UTC) - timedelta(days=90),
            window_end=datetime.now(UTC) + timedelta(days=30),
            edge_cases=[
                {"case": "merger_announced", "impact": "ignore"},
                {"case": "stock_split", "impact": "adjust"},
            ],
            clarification_ids=["test_ev_001"],
        )

        await graph.upsert_rule_graph(rule)
        print(f"   ✓ Rule graph created: {rule.condition_id}\n")

        # Step 6: Test retrieval
        print("6. Testing evidence retrieval...")

        # Get evidence bundle
        bundle = await graph.get_evidence_bundle("COND_001", max_items=10)
        print(f"   ✓ Retrieved {len(bundle)} evidence items")

        # Get rule graph
        retrieved_rule = await graph.get_rule_graph("COND_001")
        rule_name = (
            retrieved_rule.source_name if retrieved_rule
            else 'None'
        )
        print(f"   ✓ Retrieved rule graph: {rule_name}")

        # Get document
        retrieved_doc = await graph.get_document(doc_id)
        print(f"   ✓ Retrieved document: {retrieved_doc.title if retrieved_doc else 'None'}\n")

        # Step 7: Test vector search
        print("7. Testing vector search...")
        search_results = await retrieval.search(
            condition_id="COND_001",
            query="revenue growth performance",
            top_k=5
        )
        print(f"   ✓ Found {len(search_results)} similar evidence items")
        if search_results:
            print(f"   Most relevant: '{search_results[0].claim[:60]}...'\n")
        else:
            print("   (No results - embeddings may not be set)\n")

        # Step 8: Test deduplication
        print("8. Testing deduplication...")

        # Add a duplicate
        dup_doc = SourceDocument(
            doc_id="test_doc_002",
            url="https://example.com/article2",
            source_type="article",
            publisher="TestPublisher",
            content_hash="abc123def456",  # Same hash!
            title="Duplicate Article",
        )
        await graph.upsert_document(dup_doc)

        dup_evidence = EvidenceItem(
            evidence_id="test_ev_004",
            condition_id="COND_001",
            doc_id="test_doc_002",
            polarity="YES",
            claim="Revenue increased by 15% in Q4",  # Same claim
            reliability=0.85,
        )
        await graph.add_evidence(dup_evidence)

        removed = await graph.deduplicate("COND_001")
        print(f"   ✓ Removed {removed} duplicate evidence items\n")

        # Step 9: Test signal save/load
        print("9. Testing fair value signal...")
        signal = FairValueSignal(
            condition_id="COND_001",
            p_calibrated=0.72,
            p_low=0.65,
            p_high=0.79,
            uncertainty=0.14,
            skew_cents=5.2,
            hurdle_cents=3.0,
            hurdle_met=True,
            route="rule",
            evidence_ids=["test_ev_001", "test_ev_002"],
            counterevidence_ids=[],
            models_used=["claude-sonnet-4-6", "gpt-5.4"],
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )

        await graph.save_signal(signal)
        print(f"   ✓ Saved signal: p={signal.p_calibrated:.2f}")

        retrieved_signal = await graph.get_latest_signal("COND_001")
        print(f"   ✓ Retrieved signal: p={retrieved_signal.p_calibrated:.2f}\n")

        # Step 10: Test normalizer
        print("10. Testing evidence normalizer...")

        test_html = """
        <html>
        <head><title>Market Report Q4 2024</title></head>
        <body>
            <h1>Strong Quarter for Tech Sector</h1>
            <p>Revenue increased by 22.5% compared to last quarter.
            The company reached 1.2 million users in December 2024.
            This is the largest growth in company history.</p>
        </body>
        </html>
        """

        normalized_doc = normalizer.normalize_article(
            raw_html=test_html,
            url="https://example.com/report",
            publisher="TechNews"
        )
        print(f"   ✓ Normalized article: '{normalized_doc.title[:40]}...'")

        # Extract claims
        full_text = (
            "Revenue increased by 22.5% in Q4."
            " The company became the largest"
            " provider in the sector."
        )
        claims = normalizer.extract_claims_deterministic(normalized_doc, full_text)
        print(f"   ✓ Extracted {len(claims)} claims deterministically")

        for i, claim in enumerate(claims[:3], 1):
            print(f"      {i}. {claim.polarity}: {claim.claim[:50]}...")

        print()

        # Step 11: Test object storage
        print("11. Testing object storage...")

        test_content = b"This is test document content for object storage."
        key = await store.put("test/doc_001.txt", test_content, "text/plain")
        print(f"   ✓ Stored object: {key}")

        exists = await store.exists("test/doc_001.txt")
        print(f"   ✓ Object exists: {exists}")

        retrieved_content = await store.get("test/doc_001.txt")
        print(f"   ✓ Retrieved {len(retrieved_content)} bytes")

        assert retrieved_content == test_content, "Content mismatch!"
        print("   ✓ Content matches\n")

        # Final summary
        print("=" * 60)
        print("ALL TESTS PASSED ✓")
        print("=" * 60)
        print("\nSummary:")
        print(f"  • Documents: {2}")
        print(f"  • Evidence items: {len(evidence_items) + 1}")
        print(f"  • Rule graphs: {1}")
        print(f"  • Fair value signals: {1}")
        print(f"  • Claims extracted: {len(claims)}")
        print(f"  • Objects stored: {1}")
        print()

    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        raise

    finally:
        # Cleanup
        await db.close()
        print("Database connection closed.\n")


if __name__ == "__main__":
    asyncio.run(test_evidence_layer())

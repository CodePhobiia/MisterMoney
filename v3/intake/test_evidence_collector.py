"""
Test Evidence Collector
"""

import asyncio
from datetime import datetime, timezone
import pytest

from v3.intake.evidence_collector import EvidenceCollector
from v3.intake.schemas import MarketMeta
from v3.evidence.graph import EvidenceGraph
from v3.evidence.normalizer import EvidenceNormalizer
from v3.evidence.db import Database


@pytest.fixture
async def db():
    """Database fixture"""
    db = Database(
        dsn="postgresql://mmbot:mmbot_v3_2026@localhost/mistermoney_v3"
    )
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
async def evidence_graph(db):
    """Evidence graph fixture"""
    return EvidenceGraph(db)


@pytest.fixture
async def collector(evidence_graph):
    """Evidence collector fixture"""
    return EvidenceCollector(
        evidence_graph=evidence_graph,
        normalizer=EvidenceNormalizer()
    )


def test_estimate_publisher_reliability():
    """Test publisher reliability estimation"""
    collector = EvidenceCollector(
        evidence_graph=None,
        normalizer=None
    )
    
    # Tier 1: Highly reliable
    assert collector._estimate_publisher_reliability("https://www.reuters.com/article") == 0.9
    assert collector._estimate_publisher_reliability("https://www.bbc.com/news") == 0.9
    assert collector._estimate_publisher_reliability("https://www.bloomberg.com/news") == 0.9
    
    # Tier 2: Reliable
    assert collector._estimate_publisher_reliability("https://www.nytimes.com/article") == 0.75
    assert collector._estimate_publisher_reliability("https://www.cnbc.com/news") == 0.75
    
    # Tier 3: Somewhat reliable
    assert collector._estimate_publisher_reliability("https://techcrunch.com/article") == 0.55
    
    # Tier 4: Low reliability
    assert collector._estimate_publisher_reliability("https://twitter.com/user/status") == 0.25
    assert collector._estimate_publisher_reliability("https://www.reddit.com/r/news") == 0.25
    
    # Unknown
    assert collector._estimate_publisher_reliability("https://example.com/article") == 0.5
    
    print("✓ Publisher reliability estimation works")


def test_extract_urls():
    """Test URL extraction from text"""
    collector = EvidenceCollector(
        evidence_graph=None,
        normalizer=None
    )
    
    text1 = "This market resolves based on https://www.reuters.com/article123 and https://www.bbc.com/news456"
    urls1 = collector._extract_urls(text1)
    assert len(urls1) == 2
    assert "https://www.reuters.com/article123" in urls1
    assert "https://www.bbc.com/news456" in urls1
    
    text2 = "No URLs here"
    urls2 = collector._extract_urls(text2)
    assert len(urls2) == 0
    
    text3 = "Check out https://example.com/page."
    urls3 = collector._extract_urls(text3)
    assert len(urls3) == 1
    assert urls3[0] == "https://example.com/page"  # Should strip trailing period
    
    print("✓ URL extraction works")


@pytest.mark.asyncio
async def test_web_search():
    """Test web search functionality"""
    collector = EvidenceCollector(
        evidence_graph=None,
        normalizer=None
    )
    
    query = "GTA 6 release date 2026"
    results = await collector._web_search(query, num_results=3)
    
    print(f"\nWeb search results for '{query}':")
    for i, result in enumerate(results, 1):
        print(f"{i}. {result.get('title', 'No title')}")
        print(f"   URL: {result.get('url', 'No URL')}")
        print(f"   Snippet: {result.get('snippet', 'No snippet')[:100]}...")
    
    # Note: DuckDuckGo HTML search may not return results due to redirect handling
    # This test is more of a smoke test
    print(f"\n✓ Web search completed (found {len(results)} results)")


@pytest.mark.asyncio
async def test_fetch_url():
    """Test URL fetching"""
    collector = EvidenceCollector(
        evidence_graph=None,
        normalizer=None
    )
    
    # Test with a reliable URL
    url = "https://www.example.com"
    content = await collector._fetch_url(url)
    
    if content:
        assert len(content) > 0
        assert "Example Domain" in content or "example" in content.lower()
        print(f"✓ Successfully fetched {url} ({len(content)} bytes)")
    else:
        print(f"✗ Failed to fetch {url}")


@pytest.mark.asyncio
async def test_collect_for_market(collector, evidence_graph):
    """Test evidence collection for a market"""
    
    # Create a mock market
    market = MarketMeta(
        condition_id="test_gta6_2026",
        question="Will GTA VI be released before June 2026?",
        description="This market resolves YES if Rockstar Games officially releases GTA VI before June 1, 2026.",
        resolution_source="https://www.rockstargames.com Official Rockstar announcements",
        end_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
        rules="Resolves based on official Rockstar announcements",
        clarifications=[],
        volume_24h=100000.0,
        current_mid=0.35,
    )
    
    # Collect evidence
    print(f"\nCollecting evidence for: {market.question}")
    evidence_items = await collector.collect_for_market(market)
    
    print(f"✓ Collected {len(evidence_items)} evidence items")
    
    for i, item in enumerate(evidence_items, 1):
        print(f"\n{i}. Evidence ID: {item.evidence_id}")
        print(f"   Claim: {item.claim[:100]}...")
        print(f"   Reliability: {item.reliability}")
        print(f"   Polarity: {item.polarity}")
        print(f"   Values: {item.extracted_values}")
    
    # Verify evidence was stored in DB
    stored_evidence = await evidence_graph.get_evidence_bundle(
        condition_id=market.condition_id,
        max_items=50
    )
    
    print(f"\n✓ Verified {len(stored_evidence)} items stored in DB")
    
    assert len(stored_evidence) >= len(evidence_items)


@pytest.mark.asyncio
async def test_has_fresh_evidence(collector, evidence_graph):
    """Test fresh evidence check"""
    
    condition_id = "test_fresh_evidence"
    
    # Should be False for non-existent condition
    has_fresh = await collector._has_fresh_evidence(condition_id)
    assert has_fresh is False
    
    print("✓ Fresh evidence check works")


def run_sync_test(test_func):
    """Run a synchronous test function"""
    try:
        test_func()
        return True
    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False


async def run_async_test(test_func, *args):
    """Run an async test function"""
    try:
        await test_func(*args)
        return True
    except Exception as e:
        print(f"✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all tests"""
    print("=" * 60)
    print("Testing Evidence Collector")
    print("=" * 60)
    
    # Synchronous tests
    print("\n--- Synchronous Tests ---")
    run_sync_test(test_estimate_publisher_reliability)
    run_sync_test(test_extract_urls)
    
    # Async tests without DB
    print("\n--- Async Tests (No DB) ---")
    await run_async_test(test_web_search)
    await run_async_test(test_fetch_url)
    
    # Async tests with DB
    print("\n--- Async Tests (With DB) ---")
    try:
        db = Database(
            dsn="postgresql://mmbot:mmbot_v3_2026@localhost/mistermoney_v3"
        )
        await db.connect()
        
        evidence_graph = EvidenceGraph(db)
        collector = EvidenceCollector(
            evidence_graph=evidence_graph,
            normalizer=EvidenceNormalizer()
        )
        
        await run_async_test(test_has_fresh_evidence, collector, evidence_graph)
        await run_async_test(test_collect_for_market, collector, evidence_graph)
        
        await db.close()
        
    except Exception as e:
        print(f"✗ DB tests skipped: {e}")
    
    print("\n" + "=" * 60)
    print("Tests Complete")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
